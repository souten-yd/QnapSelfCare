# SPDX-License-Identifier: GPL-3.0-or-later
"""One bounded USB/BlueZ BLE operation. JSON stdin/stdout; no arbitrary commands."""
import asyncio
import json
import fcntl
import os
import secrets
import sys

import ble_protocol as protocol


class Session:
    def __init__(self, client, trace=None):
        self.client = client
        self.trace = trace
        self.frames = asyncio.Queue()
        self.unlocks = asyncio.Queue()
        self.channels = {}

    async def subscribe(self):
        await self.client.start_notify(protocol.UNLOCK, lambda _, data: self.unlocks.put_nowait(bytes(data)))
        for index, char in enumerate(protocol.RX):
            await self.client.start_notify(char, lambda _, data, i=index: self.notify(i, bytes(data)))

    def notify(self, channel, data):
        self.channels[channel] = data
        first = self.channels.get(0)
        if not first:
            return
        size = first[0]
        if not 8 <= size <= 64:
            self.channels.clear()
            self.frames.put_nowait(ValueError("Bluetooth応答の長さが不正です"))
            return
        count = (size + 15) // 16
        if all(i in self.channels for i in range(count)):
            frame = b"".join(self.channels[i] for i in range(count))
            self.channels.clear()
            self.frames.put_nowait(frame[:size])

    async def unlock(self, opcode, key, expected):
        if self.trace is not None:
            self.trace['stage'] = {1: 'unlock', 2: 'pairing_mode', 0: 'key_programming'}.get(opcode, 'unlock')
            self.trace['expected_unlock_status'] = bytes([expected, 0]).hex()
        await self.client.write_gatt_char(protocol.UNLOCK, bytes([opcode]) + key, response=True)
        data = await asyncio.wait_for(self.unlocks.get(), 10)
        if self.trace is not None:
            # Only the two-byte status. Never log the application key or bonding material.
            self.trace['unlock_status'] = data[:2].hex()
        if data[:2] != bytes([expected, 0]):
            raise ValueError("ペアリング応答が一致しません。機器を-P-表示にして再実行してください" if opcode != 1 else
                             "保存したペアリングキーが一致しません。機器の再ペアリングが必要です")

    async def request(self, opcode, address=0, length=0):
        if self.trace is not None:
            self.trace['stage'] = 'read' if opcode == 1 else 'session'
            self.trace['last_command'] = {'opcode': opcode, 'address': address, 'length': length}
        self.channels.clear()
        while not self.frames.empty():
            self.frames.get_nowait()
        packet = protocol.command(opcode, address, length)
        await self.client.write_gatt_char(protocol.TX[0], packet, response=True)
        data = await asyncio.wait_for(self.frames.get(), 10)
        if isinstance(data, Exception):
            raise data
        try:
            return protocol.response(data, opcode, address, length)
        except ValueError:
            if self.trace is not None and opcode == 1:
                self.trace['invalid_response_hex'] = data[:64].hex()
            raise

    async def records(self, device):
        profile = protocol.PROFILES[device["model"]]
        records, invalid = [], 0
        for slot, user_id in device["bindings"].items():
            base = profile["bases"][int(slot) - 1]
            size = profile["size"] * profile["count"]
            payload = bytearray()
            detail = {'slot': int(slot), 'bytes_read': 0, 'valid': 0, 'empty': 0, 'invalid': 0,
                      'valid_samples': [], 'invalid_samples': []}
            if self.trace is not None:
                self.trace['slots'].append(detail)
            for cursor in range(0, size, 32):
                payload.extend(await self.request(1, base + cursor, min(32, size - cursor)))
                detail['bytes_read'] = len(payload)
            for cursor in range(0, size, profile["size"]):
                raw = bytes(payload[cursor:cursor + profile["size"]])
                try:
                    record = protocol.decode(device["model"], raw, device["utc_offset_minutes"])
                except ValueError as error:
                    invalid += 1
                    detail['invalid'] += 1
                    if self.trace is not None and len(detail['invalid_samples']) < 2:
                        detail['invalid_samples'].append({'index': cursor // profile['size'],
                                                          'reason': str(error)[:120], 'raw_hex': raw.hex()})
                    continue
                if record:
                    detail['valid'] += 1
                    if self.trace is not None and len(detail['valid_samples']) < 2:
                        detail['valid_samples'].append({'index': cursor // profile['size'],
                                                        'measured_at': record['measured_at'], 'values': record['values']})
                    records.append(dict(record, user_id=user_id, device_id=device["id"], slot=int(slot)))
                else:
                    detail['empty'] += 1
        return {"records": records, "invalid_records": invalid}


async def register_agent(address):
    from dbus_fast import BusType, DBusError
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method
    expected = address.upper().replace(":", "_")

    class Agent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        def check(self, device):
            if not device.upper().endswith("DEV_" + expected):
                raise DBusError("org.bluez.Error.Rejected", "Unrequested device")

        @method()
        def Release(self):
            pass

        @method()
        def Cancel(self):
            pass

        @method()
        def RequestConfirmation(self, device: 'o', passkey: 'u'):
            self.check(device)

        @method()
        def RequestAuthorization(self, device: 'o'):
            self.check(device)

        @method()
        def AuthorizeService(self, device: 'o', uuid: 's'):
            self.check(device)

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    path = "/org/qnapselfcare/agent"
    bus.export(path, Agent())
    intro = await bus.introspect("org.bluez", "/org/bluez")
    manager = bus.get_proxy_object("org.bluez", "/org/bluez", intro).get_interface("org.bluez.AgentManager1")
    await manager.call_register_agent(path, "NoInputNoOutput")
    await manager.call_request_default_agent(path)
    return bus, manager, path


async def operate(request, trace=None):
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        raise ValueError("USB Bluetooth用のbleakが未導入です。管理画面のセットアップ手順を確認してください") from None
    adapter = request.get("adapter", "hci0")
    if trace is not None:
        trace['stage'] = 'adapter'
    # Only run inside an exclusive radio operation. Enable the selected adapter;
    # do not reset controllers or change unrelated adapters.
    from dbus_fast import BusType, Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(Message(destination="org.bluez", path="/org/bluez/" + adapter,
            interface="org.freedesktop.DBus.Properties", member="Set", signature="ssv",
            body=["org.bluez.Adapter1", "Powered", Variant('b', True)]))
        if reply.message_type == MessageType.ERROR:
            raise ValueError("USB Bluetoothを有効化できません: " + str(reply.body))
    finally:
        bus.disconnect()
    if request["action"] == "scan":
        devices = await BleakScanner.discover(timeout=10, return_adv=True, adapter=adapter)
        return {"devices": [{"address": d.address, "name": adv.local_name or d.name or "名前なし", "rssi": adv.rssi}
                            for d, adv in devices.values()]}
    device = request["device"]
    address = device["address"]
    if trace is not None:
        trace['stage'] = 'discovery'
    found = await BleakScanner.find_device_by_address(address, timeout=20, adapter=adapter)
    if found is None:
        raise ValueError("機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください")
    agent = None
    try:
        if trace is not None:
            trace['stage'] = 'connection'
        agent = await register_agent(address)
        async with BleakClient(found, adapter=adapter, timeout=20) as client:
            if trace is not None:
                trace['stage'] = 'service'
            if not client.services.get_service(protocol.SERVICE):
                raise ValueError("対応するOmronサービスがありません。機種とアドレスを確認してください")
            session = Session(client, trace)
            if trace is not None:
                trace['stage'] = 'subscribe'
            await session.subscribe()
            if request["action"] == "pair":
                await session.unlock(2, bytes(16), 0x82)
                # The parent persists the proposed key before programming it so a
                # later disconnect cannot lose the newly programmed credential.
                key = bytes.fromhex(request["key"])
                await session.unlock(0, key, 0x80)
                await session.request(0, length=16)
                await session.request(15)
                if trace is not None:
                    trace['stage'] = 'completed'
                return {"paired": True, **({'diagnostic': trace} if trace is not None else {})}
            key = request.get("key")
            if not key:
                raise ValueError("先にペアリングしてください")
            await session.unlock(1, bytes.fromhex(key), 0x81)
            await session.request(0, length=16)
            result = await session.records(device)
            await session.request(15)
            if trace is not None:
                trace['stage'] = 'completed'
                result['diagnostic'] = trace
            return result
    finally:
        if agent:
            bus, manager, path = agent
            try:
                await manager.call_unregister_agent(path)
            finally:
                bus.disconnect()


def main():
    trace = None
    try:
        lock_path = os.environ.get("SELFCARE_BLE_LOCK", "/tmp/qnapselfcare-ble.lock")
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = json.loads(sys.stdin.read(65536))
        if request.get('action') in ('pair', 'sync') and request.get('diagnostic') is True:
            trace = {'stage': 'starting', 'slots': []}
        result = asyncio.run(asyncio.wait_for(operate(request, trace), 180))
        print(json.dumps(result))
    except Exception as error:
        message = str(error) or type(error).__name__
        print(json.dumps({"error": message, **({'diagnostic': trace} if trace is not None else {})}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
