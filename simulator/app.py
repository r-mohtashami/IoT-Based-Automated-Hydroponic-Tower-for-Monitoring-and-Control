import json
import logging
import random
import socket
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from simulator import (
    ACTUATOR_EVENT_TOPIC_TEMPLATE,
    SimulatorConfig,
    advance_state,
    apply_actuator_command,
    build_actuator_ack,
    fetch_backend_tower_keys,
    inject_disturbance,
    load_states,
    observe_state,
    parse_actuator_command,
    save_states,
    sync_states_to_tower_keys,
    tower_keys,
)

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s simulator %(message)s")
logger = logging.getLogger("simulator")
TELEMETRY_TOPIC_TEMPLATE = "hydroponics/towers/{tower_key}/telemetry"
ACTUATOR_COMMAND_TOPIC = "hydroponics/towers/+/commands/actuator"


def main() -> None:
    config = SimulatorConfig.from_env()
    fallback_keys = tower_keys(config.tower_count)
    keys = list(fallback_keys)
    if config.discover_towers_from_backend:
        try:
            keys = fetch_backend_tower_keys(config.backend_base_url)
        except RuntimeError as exc:
            logger.warning("Initial backend tower discovery failed; using configured fallback: %s", exc)

    known_towers = set(keys)
    states = load_states(config.state_path, keys)
    deviation_applied: set[str] = set()
    rng = random.Random(config.random_seed)
    connected = threading.Event()
    state_lock = threading.Lock()
    last_tower_refresh = 0.0

    def apply_configured_deviation_once() -> None:
        if config.deviation is None:
            return
        key = config.deviation.tower_key
        if key in states and key not in deviation_applied:
            inject_disturbance(states[key], config.deviation)
            deviation_applied.add(key)

    def refresh_towers(*, force: bool = False) -> None:
        nonlocal keys, last_tower_refresh
        if not config.discover_towers_from_backend:
            return
        now_monotonic = time.monotonic()
        if not force and now_monotonic - last_tower_refresh < config.tower_refresh_seconds:
            return
        last_tower_refresh = now_monotonic
        try:
            discovered = fetch_backend_tower_keys(config.backend_base_url)
        except RuntimeError as exc:
            logger.warning("Backend tower discovery failed; keeping current virtual towers: %s", exc)
            return
        with state_lock:
            added, removed = sync_states_to_tower_keys(states, discovered)
            keys = list(discovered)
            known_towers.clear()
            known_towers.update(keys)
            apply_configured_deviation_once()
            if added or removed:
                save_states(config.state_path, states)
                logger.info(
                    "Synchronized virtual towers active=%s added=%s removed=%s",
                    len(keys),
                    ",".join(added) or "none",
                    ",".join(removed) or "none",
                )

    # Scenario injection is a physical-state event. Apply once per logical virtual tower.
    with state_lock:
        apply_configured_deviation_once()
        save_states(config.state_path, states)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"hydroponics-simulator-{socket.gethostname()}",
    )
    client.reconnect_delay_set(min_delay=1, max_delay=10)

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.warning("MQTT connection failed reason=%s", reason_code)
            return
        client.subscribe(ACTUATOR_COMMAND_TOPIC, qos=1)
        connected.set()
        Path("/tmp/simulator-ready").touch()
        logger.info(
            "MQTT connected towers=%s persistent_state=%s dynamic_discovery=%s",
            len(keys),
            config.state_path,
            config.discover_towers_from_backend,
        )

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties) -> None:
        connected.clear()
        if reason_code.is_failure:
            logger.warning("MQTT disconnected unexpectedly reason=%s", reason_code)

    def on_message(client, userdata, message) -> None:
        try:
            command = parse_actuator_command(message.topic, message.payload, known_towers)
        except ValueError as exc:
            logger.warning("Rejected simulated actuator command topic=%s error=%s", message.topic, exc)
            return
        tower_key = str(command["tower_key"])
        with state_lock:
            # The tower can disappear between parse and lock acquisition after a user removal.
            state = states.get(tower_key)
            if state is None:
                logger.warning("Rejected actuator command for removed tower=%s", tower_key)
                return
            apply_actuator_command(state, command)
            save_states(config.state_path, states)
        ack = build_actuator_ack(command)
        ack_topic = ACTUATOR_EVENT_TOPIC_TEMPLATE.format(tower_key=tower_key)
        info = client.publish(ack_topic, json.dumps(ack, separators=(",", ":")), qos=1)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(
                "Acknowledged simulated actuator tower=%s actuator=%s command_id=%s",
                tower_key,
                command["actuator_type"],
                command["command_id"],
            )

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(config.mqtt_host, config.mqtt_port, keepalive=30)
    client.loop_start()

    logger.info(
        "Starting stateful simulated hardware towers=%s interval_seconds=%s run_once=%s dynamic_discovery=%s",
        len(keys),
        config.interval_seconds,
        config.run_once,
        config.discover_towers_from_backend,
    )
    try:
        while True:
            if not connected.wait(timeout=10):
                logger.warning("Waiting for MQTT broker connection")
                continue
            refresh_towers(force=config.run_once)
            with state_lock:
                active_keys = list(keys)
                for tower_key in active_keys:
                    state = states.get(tower_key)
                    if state is None:
                        continue
                    advance_state(state, config.interval_seconds)
                    payload = observe_state(
                        state,
                        rng,
                        config.missing_sensors.get(tower_key),
                    )
                    topic = TELEMETRY_TOPIC_TEMPLATE.format(tower_key=tower_key)
                    info = client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=0)
                    info.wait_for_publish(timeout=5)
                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.warning("Publish failed tower=%s rc=%s", tower_key, info.rc)
                save_states(config.state_path, states)
            if config.run_once:
                logger.info("SIM_RUN_ONCE complete towers_published=%s", len(active_keys))
                return
            time.sleep(config.interval_seconds)
    finally:
        with state_lock:
            save_states(config.state_path, states)
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
