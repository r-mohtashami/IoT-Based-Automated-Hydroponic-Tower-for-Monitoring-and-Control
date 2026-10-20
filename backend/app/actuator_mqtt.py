from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

COMMAND_TOPIC_TEMPLATE = "hydroponics/towers/{tower_key}/commands/actuator"
ACK_TOPIC_TEMPLATE = "hydroponics/towers/{tower_key}/events/actuator"
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 5.0


class CommandClaim(Protocol):
    command_id: str | None
    tower_key: str | None
    recommendation_id: int
    action: object | None


@dataclass(frozen=True)
class ActuatorAck:
    command_id: str
    tower_key: str
    actuator_type: str
    state: str
    timestamp: datetime


@dataclass(frozen=True)
class MqttExchangeResult:
    command_sent: bool
    sent_at: datetime | None
    ack: ActuatorAck | None
    error: str | None = None


def verification_timeout_seconds() -> float:
    value = float(
        os.getenv(
            "ACTUATOR_VERIFICATION_TIMEOUT_SECONDS",
            str(DEFAULT_VERIFICATION_TIMEOUT_SECONDS),
        )
    )
    if value <= 0:
        raise ValueError("ACTUATOR_VERIFICATION_TIMEOUT_SECONDS must be positive.")
    return value


def command_payload(claim: CommandClaim) -> dict[str, object]:
    if claim.command_id is None or claim.tower_key is None or claim.action is None:
        raise ValueError("Execution claim is not publishable.")
    action = claim.action
    return {
        "simulated": True,
        "command_id": claim.command_id,
        "tower_key": claim.tower_key,
        "recommendation_id": claim.recommendation_id,
        "actuator_type": getattr(action, "actuator_type"),
        "command": getattr(action, "command"),
        "state": getattr(action, "state"),
    }


def parse_ack(
    topic: str,
    payload: bytes | str,
    *,
    expected_command_id: str,
    expected_tower_key: str,
    expected_actuator_type: str,
    expected_state: str,
) -> ActuatorAck | None:
    if topic != ACK_TOPIC_TEMPLATE.format(tower_key=expected_tower_key):
        return None
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("simulated") is not True:
        return None
    if raw.get("command_id") != expected_command_id or raw.get("tower_key") != expected_tower_key:
        return None
    if raw.get("actuator_type") != expected_actuator_type or raw.get("state") != expected_state:
        return None
    if raw.get("status") != "executed":
        return None

    timestamp_raw = raw.get("timestamp")
    if not isinstance(timestamp_raw, str):
        return None
    try:
        timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        timestamp = timestamp.astimezone(timezone.utc)
    except ValueError:
        return None

    return ActuatorAck(
        command_id=expected_command_id,
        tower_key=expected_tower_key,
        actuator_type=expected_actuator_type,
        state=expected_state,
        timestamp=timestamp,
    )


def publish_and_wait_for_ack(claim: CommandClaim) -> MqttExchangeResult:
    import paho.mqtt.client as mqtt

    payload = command_payload(claim)
    assert claim.command_id is not None and claim.tower_key is not None and claim.action is not None
    action = claim.action
    host = os.getenv("MQTT_HOST", "mosquitto")
    port = int(os.getenv("MQTT_PORT", "1883"))
    timeout = verification_timeout_seconds()
    command_topic = COMMAND_TOPIC_TEMPLATE.format(tower_key=claim.tower_key)
    ack_topic = ACK_TOPIC_TEMPLATE.format(tower_key=claim.tower_key)
    connected = threading.Event()
    acknowledged = threading.Event()
    ack_holder: dict[str, ActuatorAck] = {}

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"hydroponics-executor-{socket.gethostname()}-{claim.command_id[:8]}",
    )

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        if not reason_code.is_failure:
            client.subscribe(ack_topic, qos=1)
            connected.set()

    def on_message(client, userdata, message) -> None:
        ack = parse_ack(
            message.topic,
            message.payload,
            expected_command_id=claim.command_id,
            expected_tower_key=claim.tower_key,
            expected_actuator_type=getattr(action, "actuator_type"),
            expected_state=getattr(action, "state"),
        )
        if ack is not None:
            ack_holder["ack"] = ack
            acknowledged.set()

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        if not connected.wait(timeout):
            return MqttExchangeResult(False, None, None, "MQTT connection/subscription timed out.")

        info = client.publish(command_topic, json.dumps(payload, separators=(",", ":")), qos=1)
        info.wait_for_publish(timeout=timeout)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return MqttExchangeResult(False, None, None, f"MQTT publish failed rc={info.rc}.")
        sent_at = datetime.now(timezone.utc)

        if not acknowledged.wait(timeout):
            return MqttExchangeResult(True, sent_at, None, "Simulator acknowledgement timed out.")
        return MqttExchangeResult(True, sent_at, ack_holder["ack"], None)
    except OSError as exc:
        return MqttExchangeResult(False, None, None, f"MQTT error: {exc}")
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()
