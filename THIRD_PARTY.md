# Bluetooth protocol attribution

`ble_protocol.py`, `ble_worker.py`, and `ble_bridge.py` are distributed under GPL-3.0-or-later. Their complete Python source is included in the QPKG and container. The license is in `licenses/GPL-3.0.txt`.

- HBF-228T record geometry and WLP framing are adapted from openScale `OmronLib.kt` and `OmronWlcHandler.kt`, Copyright (C) 2026 openScale contributors (GPL-3.0-or-later): https://github.com/oliexdev/openScale/tree/master/android_app/app/src/main/java/com/health/openscale/core/bluetooth
- HEM-6232T memory layout is based on hass-omron `omron_ble/device_catalog.py` and `record_parsers.py`, Copyright (c) 2025 eigger (MIT): https://github.com/eigger/hass-omron/tree/master/custom_components/omron/omron_ble . The MIT notice is included in `licenses/hass-omron-MIT.txt`.
- The optional BLE runtime uses Bleak 0.22.3 and its dependencies, installed from PyPI. Their distribution notices remain in the installed packages.

Only session start/end, bounded memory reads, and explicit pairing-key programming are implemented. Measurement EEPROM writes, calibration changes, unread-counter clearing and clock changes are not implemented.

QnapHomeHub's optional radio container vendors the same two BLE worker/protocol files as standalone subprocess programs; its notices are included alongside them. UI and storage code communicate using a JSON request/response interface. The Bluetooth code has not yet been verified on the user's NAS and devices.
