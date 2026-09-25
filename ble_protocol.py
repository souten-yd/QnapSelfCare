# SPDX-License-Identifier: GPL-3.0-or-later
# HBF record layout adapted from openScale OmronLib.kt, Copyright (C) 2026
# openScale contributors. HEM layout based on hass-omron (MIT, eigger 2025).
# See THIRD_PARTY.md and licenses/. This module never writes measurement EEPROM.
from datetime import datetime, timedelta, timezone
from functools import reduce
from operator import xor

SERVICE = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b"
UNLOCK = "b305b680-aee7-11e1-a730-0002a5d5c51b"
TX = [f"{prefix}-aee7-11e1-{tail}-0002a5d5c51b" for prefix, tail in
      [("db5b55e0", "965e"), ("e0b8a060", "92f4")]] + [
      "0ae12b00-aee8-11e1-a192-0002a5d5c51b", "10e1ba60-aee8-11e1-89e5-0002a5d5c51b"]
RX = [f"{prefix}-aee8-11e1-{tail}-0002a5d5c51b" for prefix, tail in
      [("49123040", "a74d"), ("4d0bf320", "a0d9"), ("5128ce60", "b84b"), ("560f1420", "8184")]]
PROFILES = {"HEM-6232T": {"bases": [0x2e8, 0x860], "count": 100, "size": 14},
            "HBF-228T": {"bases": [0x2c0, 0x6a0, 0xa80, 0xe60], "count": 30, "size": 32}}


def checksum(data):
    return reduce(xor, data, 0)


def command(opcode, address=0, length=0):
    if opcode not in (0, 1, 15) or not 0 <= address <= 0x3fff or not 0 <= length <= 32:
        raise ValueError("Unsupported read command")
    data = bytes([8, opcode, 0, address >> 8, address & 255, length, 0])
    return data + bytes([checksum(data)])


def response(data, opcode, address, length):
    if len(data) < 8 or len(data) > 64 or data[0] != len(data) or checksum(data):
        raise ValueError("Bluetooth応答の長さまたはチェックサムが不正です")
    if opcode != 15 and data[1:3] == b'\x8f\x00' and len(data) == 8:
        raise ValueError(f"機器がコマンドを拒否しました（コード 0x{data[6]:02x}）")
    if data[1:3] != bytes([opcode | 0x80, 0]) or int.from_bytes(data[3:5], "big") != address:
        raise ValueError("Bluetooth応答が要求したコマンドと一致しません")
    if data[-2] != 0:
        raise ValueError(f"機器が通信エラーを返しました（{data[-2]}）")
    # Session start/end acknowledge with an eight-byte control frame. The
    # start command's 0x10 field is not a promise of 16 response payload bytes.
    if opcode in (0, 15):
        if len(data) != 8:
            raise ValueError("通信開始・終了応答の長さが不正です")
        return b''
    if data[5] != length or len(data) != length + 8:
        raise ValueError("Bluetooth応答のデータが不足しています")
    return data[6:-2]


def decode(model, data, offset_minutes=540):
    profile = PROFILES[model]
    if len(data) != profile["size"]:
        raise ValueError("測定レコードの長さが不正です")
    if all(b == 0 for b in data) or all(b == 255 for b in data):
        return None
    tz = timezone(timedelta(minutes=offset_minutes))
    if model == "HEM-6232T":
        value = int.from_bytes(data, "big")
        def bits(first, last):
            return (value >> (len(data) * 8 - last - 1)) & ((1 << (last - first + 1)) - 1)
        date = datetime(2000 + bits(18, 23), bits(34, 37), bits(38, 42), bits(43, 47),
                        bits(52, 57), min(bits(58, 63), 59), tzinfo=tz)
        values = {"systolic": bits(8, 15) + 25, "diastolic": bits(0, 7), "pulse": bits(24, 31)}
        kind = "blood_pressure"
    else:
        def field(offset, size, shift, width):
            return (int.from_bytes(data[offset:offset + size], "big") >> shift) & ((1 << width) - 1)
        weight = field(26, 2, 4, 12)
        if not weight:
            return None
        date = datetime(2000 + field(7, 1, 0, 6), field(11, 1, 0, 4), field(12, 1, 3, 5),
                        field(12, 2, 6, 5), field(9, 1, 0, 6), field(13, 1, 0, 6), tzinfo=tz)
        values = {"weight": round(weight * 0.05, 2)}
        fields = {"body_fat": (2, 2, 6, 10, 0.1), "visceral_fat": (2, 2, 0, 6, 1),
                  "bmr": (4, 2, 4, 12, 1), "muscle": (6, 2, 6, 10, 0.1),
                  "bmi": (8, 2, 6, 10, 0.1), "body_age": (10, 1, 0, 7, 1)}
        for name, (offset, size, shift, width, scale) in fields.items():
            raw = field(offset, size, shift, width)
            if raw:
                values[name] = round(raw * scale, 2)
        kind = "body_composition"
    return {"measured_at": date.isoformat(), "kind": kind, "values": values, "raw": data.hex()}
