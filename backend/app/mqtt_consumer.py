import logging
import os

import paho.mqtt.client as mqtt
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.telemetry import present_sensor_fields, validate_telemetry_message
from app.telemetry_store import persist_telemetry

logger = logging.getLogger("hydroponics.mqtt")
TELEMETRY_TOPIC = "hydroponics/towers/+/telemetry"


class TelemetryConsumer:
    def __init__(self) -> None:
        self.host = os.getenv("MQTT_HOST", "mosquitto")
        self.port = int(os.getenv("MQTT_PORT", "1883"))
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="hydroponics-backend",
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self) -> None:
        logger.info("Starting MQTT telemetry consumer host=%s port=%s", self.host, self.port)
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.warning("MQTT connection failed reason=%s", reason_code)
            return
        client.subscribe(TELEMETRY_TOPIC)
        logger.info("MQTT connected; subscribed topic=%s", TELEMETRY_TOPIC)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.warning(
                "MQTT disconnected unexpectedly reason=%s; reconnect will be retried",
                reason_code,
            )

    def _on_message(self, client, userdata, message) -> None:
        try:
            telemetry = validate_telemetry_message(message.topic, message.payload)
        except (ValidationError, ValueError, UnicodeDecodeError) as exc:
            logger.warning(
                "Rejected MQTT telemetry topic=%s error=%s",
                message.topic,
                exc,
            )
            return

        try:
            with SessionLocal() as session:
                persistence_status, _reading = persist_telemetry(session, telemetry)
        except SQLAlchemyError:
            logger.exception(
                "Failed to persist MQTT telemetry tower=%s timestamp=%s",
                telemetry.tower_key,
                telemetry.timestamp.isoformat(),
            )
            return

        if persistence_status == "unknown_tower":
            logger.warning(
                "Rejected MQTT telemetry for unknown tower_key=%s",
                telemetry.tower_key,
            )
            return
        if persistence_status == "duplicate":
            logger.info(
                "Ignored replayed telemetry tower=%s timestamp=%s",
                telemetry.tower_key,
                telemetry.timestamp.isoformat(),
            )
            return

        logger.info(
            "Persisted simulated telemetry tower=%s timestamp=%s sensors=%s circulation_on=%s",
            telemetry.tower_key,
            telemetry.timestamp.isoformat(),
            ",".join(present_sensor_fields(telemetry)) or "none",
            telemetry.circulation_on,
        )
