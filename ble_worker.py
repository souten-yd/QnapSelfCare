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
    def __init__(self, client):
        self.client = client
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
        await self.client.write_gatt_char(protocol.UNLOCK, bytes([opcode]) + key, response=True)
        data = await asyncio.wait_for(self.unlocks.get(), 10)
        if data[:2] != bytes([expected, 0]):
            raise ValueError("ペアリング応答が一致しません。機器を-P-表示にして再実行してください" if opcode != 1 else
                             "保存したペアリングキーが一致しません。機器の再ペアリングが必要です")

    async def request(self, opcode, address=0, length=0):
        self.channels.clear()
        while not self.frames.empty():
            self.frames.get_nowait()
        packet = protocol.command(opcode, address, length)
        await self.client.write_gatt_char(protocol.TX[0], packet, response=True)
        data = await asyncio.wait_for(self.frames.get(), 10)
        if isinstance(data, Exception):
            raise data
        return protocol.response(data, opcode, address, length)

    async def records(self, device):
        profile = protocol.PROFILES[device["model"]]
        records, invalid = [], 0
        for slot, user_id in device["bindings"].items():
            base = profile["bases"][int(slot) - 1]
            size = profile["size"] * profile["count"]
            payload = bytearray()
            for cursor in range(0, size, 32):
                payload.extend(await self.request(1, base + cursor, min(32, size - cursor)))
            for cursor in range(0, size, profile["size"]):
                try:
                    record = protocol.decode(device["model"], payload[cursor:cursor + profile["size"]], device["utc_offset_minutes"])
                except ValueError:
                    invalid += 1
                    continue
                if record:
                    records.append(dict(record, user_id=user_id, device_id=device["id"], slot=int(slot)))
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


async def operate(request):
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        raise ValueError("USB Bluetooth用のbleakが未導入です。管理画面のセットアップ手順を確認してください") from None
    adapter = request.get("adapter", "hci0")
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
    found = await BleakScanner.find_device_by_address(address, timeout=20, adapter=adapter)
    if found is None:
        raise ValueError("機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください")
    agent = None
    try:
        agent = await register_agent(address)
        async with BleakClient(found, adapter=adapter, timeout=20) as client:
            if not client.services.get_service(protocol.SERVICE):
                raise ValueError("対応するOmronサービスがありません。機種とアドレスを確認してください")
            session = Session(client)
            await session.subscribe()
            if request["action"] == "pair":
                await session.unlock(2, bytes(16), 0x82)
                # The parent persists the proposed key before programming it so a
                # later disconnect cannot lose the newly programmed credential.
                key = bytes.fromhex(request["key"])
                await session.unlock(0, key, 0x80)
                await session.request(0, length=16)
                await session.request(15)
                return {"paired": True}
            key = request.get("key")
            if not key:
                raise ValueError("先にペアリングしてください")
            await session.unlock(1, bytes.fromhex(key), 0x81)
            await session.request(0, length=16)
            result = await session.records(device)
            await session.request(15)
            return result
    finally:
        if agent:
            bus, manager, path = agent
            try:
                await manager.call_unregister_agent(path)
            finally:
                bus.disconnect()


def main():
    try:
        lock_path = os.environ.get("SELFCARE_BLE_LOCK", "/tmp/qnapselfcare-ble.lock")
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = json.loads(sys.stdin.read(65536))
        result = asyncio.run(asyncio.wait_for(operate(request), 180))
        print(json.dumps(result))
    except Exception as error:
        message = str(error) or type(error).__name__
        print(json.dumps({"error": message}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
