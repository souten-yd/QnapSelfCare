FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bluez dbus && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-ble.txt ./
RUN pip install --no-cache-dir -r requirements-ble.txt
COPY ble_worker.py ble_protocol.py ble_bridge.py ./
COPY licenses ./licenses
COPY THIRD_PARTY.md ./
COPY bluetooth/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
