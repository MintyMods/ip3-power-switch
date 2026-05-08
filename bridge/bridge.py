#!/usr/bin/env python3
"""IP3POWERSWITCH <-> Home Assistant MQTT bridge.

Reads the EC's FCMO byte to publish current mode; writes the EC's FCMI byte
(plus calls powerprofilesctl) to change modes. Event-driven: no polling.

Hardware: AMD Strix Halo systems with an IP3 Tech mainboard
(Corsair AI Workstation 300, Beelink GTR series, GMK EVO-X1/X2, etc.)
that expose ACPI WMI device with _UID "IP3POWERSWITCH".
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

# -- config from env -----------------------------------------------------------
BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
USER = os.environ["MQTT_USER"]
PASS = os.environ["MQTT_PASS"]

# -- hardware specifics --------------------------------------------------------
EC_IO = "/sys/kernel/debug/ec/ec0/io"
EC_FCMO_OFFSET = 0x31  # read: current mode (0..3)
EC_FCMI_OFFSET = 0x32  # write: 0x80 | mode to set new mode

# -- HA entity layout (override via env) ---------------------------------------
DEVICE_ID = os.environ.get("DEVICE_ID", "ip3_power_switch")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "IP3 Power Switch")
DEVICE_MANUFACTURER = os.environ.get("DEVICE_MANUFACTURER", "IP3 Tech")
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "AI Mainboard")
ENTITY_ID = f"{DEVICE_ID}_power_profile"

TOPIC_BASE = os.environ.get("TOPIC_BASE", f"ip3/{DEVICE_ID}")
DISCOVERY_TOPIC = f"homeassistant/select/{ENTITY_ID}/config"
STATE_TOPIC = f"{TOPIC_BASE}/profile/state"
COMMAND_TOPIC = f"{TOPIC_BASE}/profile/set"
AVAIL_TOPIC = f"{TOPIC_BASE}/availability"

# -- mode mapping --------------------------------------------------------------
# Friendly label -> (FCMI byte, PPD profile name)
MODES = {
    "Quiet":         (0x80, "power-saver"),
    "Balanced":      (0x81, "balanced"),
    "Performance":   (0x82, "performance"),
    "Mode 4":        (0x83, "performance"),  # undocumented — keep PPD on perf
}
MODE_LABELS = list(MODES.keys())
LABEL_BY_FCMO = {i: label for i, label in enumerate(MODE_LABELS)}

# -- logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ip3-power-bridge")


# -- EC helpers ----------------------------------------------------------------
def read_ec_byte(offset: int) -> int | None:
    try:
        with open(EC_IO, "rb") as fh:
            fh.seek(offset)
            return fh.read(1)[0]
    except OSError as exc:
        log.error("EC read at 0x%02x failed: %s", offset, exc)
        return None


def write_ec_byte(offset: int, value: int) -> bool:
    try:
        with open(EC_IO, "r+b") as fh:
            fh.seek(offset)
            fh.write(bytes([value & 0xFF]))
        return True
    except OSError as exc:
        log.error("EC write 0x%02x=0x%02x failed: %s", offset, value, exc)
        return False


def read_current_label() -> str | None:
    fcmo = read_ec_byte(EC_FCMO_OFFSET)
    if fcmo is None:
        return None
    return LABEL_BY_FCMO.get(fcmo & 0x0F)  # mask high bit if MCU echoed it


# -- powerprofilesctl ----------------------------------------------------------
def set_ppd(profile: str) -> bool:
    try:
        subprocess.run(
            ["powerprofilesctl", "set", profile],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        log.error("powerprofilesctl set %s failed: %s", profile, exc.stderr.strip())
        return False
    except FileNotFoundError:
        log.warning("powerprofilesctl not present; skipping PPD update")
        return True  # don't fail the whole set if PPD just isn't installed


# -- MQTT plumbing -------------------------------------------------------------
def publish_discovery(client: mqtt.Client) -> None:
    payload = {
        "name": "Power profile",
        "unique_id": ENTITY_ID,
        "object_id": ENTITY_ID,
        "icon": "mdi:speedometer",
        "options": MODE_LABELS,
        "state_topic": STATE_TOPIC,
        "command_topic": COMMAND_TOPIC,
        "availability_topic": AVAIL_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [DEVICE_ID],
            "name": DEVICE_NAME,
            "manufacturer": DEVICE_MANUFACTURER,
            "model": DEVICE_MODEL,
        },
    }
    client.publish(DISCOVERY_TOPIC, json.dumps(payload), qos=1, retain=True)


def publish_state(client: mqtt.Client) -> None:
    label = read_current_label()
    if label is None:
        return
    client.publish(STATE_TOPIC, label, qos=1, retain=True)
    log.info("state -> %s", label)


def apply_mode(label: str) -> bool:
    if label not in MODES:
        log.warning("unknown mode label: %s", label)
        return False
    fcmi_byte, ppd_profile = MODES[label]
    if not write_ec_byte(EC_FCMI_OFFSET, fcmi_byte):
        return False
    set_ppd(ppd_profile)
    return True


def on_connect(client, userdata, flags, rc):
    if rc != 0:
        log.error("MQTT connect failed rc=%s", rc)
        return
    log.info("MQTT connected")
    client.subscribe(COMMAND_TOPIC, qos=1)
    publish_discovery(client)
    client.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
    publish_state(client)


def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    log.info("cmd: %s", payload)
    if apply_mode(payload):
        publish_state(client)


# -- main loop -----------------------------------------------------------------
def main():
    client = mqtt.Client(client_id="ip3-power-bridge", clean_session=True)
    client.username_pw_set(USER, PASS)
    client.will_set(AVAIL_TOPIC, "offline", qos=1, retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()

    stop_event = threading.Event()

    def handle_signal(*_):
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    stop_event.wait()
    log.info("shutting down")
    client.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
