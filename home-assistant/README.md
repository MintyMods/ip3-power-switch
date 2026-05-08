# Home Assistant integration

The bridge publishes an [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) payload, so the entity appears automatically once HA's MQTT integration is configured against the same broker.

## Add the MQTT integration

`Settings → Devices & Services → Add Integration → MQTT`

| Field | Value |
|---|---|
| Broker | `127.0.0.1` (or your broker's host) |
| Port | `1883` |
| Username | the `MQTT_USER` from `/etc/ip3-power-bridge.env` |
| Password | the `MQTT_PASS` from same |
| Discovery | leave enabled (default) |

Within a second or two of the bridge connecting, you'll see a new device named **Minty-AI Workstation** (rename in `bridge.py` if you like) with a single entity:

```
select.minty_ai_workstation_power_profile
```

Options: **Quiet · Balanced · Performance · Mode 4**

## Dashboard card

A horizontal pill toggle is the most natural UI for a 3- or 4-way select. Drop this onto any view:

```yaml
type: tile
entity: select.minty_ai_workstation_power_profile
name: Power profile
icon: mdi:speedometer
features_position: bottom
vertical: false
features:
  - type: select-options
```

## Mosquitto setup (if you don't already have a broker)

The bridge needs a broker. Quickest path is a Docker container alongside HA:

```yaml
# docker-compose.yml fragment
mosquitto:
  container_name: mosquitto
  image: eclipse-mosquitto:2
  restart: unless-stopped
  network_mode: host
  volumes:
    - ./mosquitto/config:/mosquitto/config
    - ./mosquitto/data:/mosquitto/data
    - ./mosquitto/log:/mosquitto/log
```

```conf
# mosquitto/config/mosquitto.conf
listener 1883 0.0.0.0
allow_anonymous false
password_file /mosquitto/config/passwd

persistence true
persistence_location /mosquitto/data/
log_dest stdout
```

Generate the password file with:

```bash
docker run --rm -i \
  -v $(pwd)/mosquitto/config:/mosquitto/config \
  --entrypoint sh eclipse-mosquitto:2 \
  -c 'mosquitto_passwd -c -b /mosquitto/config/passwd corsair-bridge "your-password"'
```

## Caveats

- The bridge does **not** poll. It publishes the current mode once on MQTT connect, and republishes whenever it processes a `set` command. If you press the front-panel button physically, the dashboard will not auto-refresh — it will show stale state until the bridge restarts or you tap a pill (which forces a re-publish).
- If you want live updates from physical button presses, see the `watchdog` branch (TODO) which polls `EC[0x70]` (the press counter) every 250 ms.
