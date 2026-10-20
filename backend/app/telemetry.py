from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)


TowerKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
]


class TelemetryPayload(BaseModel):
    """Validated MQTT telemetry. Sensor fields are optional by design."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    simulated: Literal[True]
    tower_key: TowerKey
    timestamp: datetime
    ph: float | None = Field(default=None, ge=0, le=14)
    ec_ms_cm: float | None = Field(default=None, ge=0)
    water_temp_c: float | None = None
    air_temp_c: float | None = None
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    water_level_pct: float | None = Field(default=None, ge=0, le=100)
    ppfd_umol_m2_s: float | None = Field(default=None, ge=0)
    circulation_on: bool

    @field_validator("timestamp")
    @classmethod
    def require_timezone_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Telemetry timestamp must include an explicit timezone offset or Z.")
        return value.astimezone(timezone.utc)


SENSOR_FIELDS = (
    "ph",
    "ec_ms_cm",
    "water_temp_c",
    "air_temp_c",
    "humidity_pct",
    "water_level_pct",
    "ppfd_umol_m2_s",
)


def tower_key_from_topic(topic: str) -> str:
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "hydroponics" or parts[1] != "towers" or parts[3] != "telemetry":
        raise ValueError("Unexpected telemetry topic.")
    if not parts[2]:
        raise ValueError("Telemetry topic is missing tower key.")
    return parts[2]


def validate_telemetry_message(topic: str, payload: bytes | str) -> TelemetryPayload:
    topic_tower_key = tower_key_from_topic(topic)
    telemetry = TelemetryPayload.model_validate_json(payload)
    if telemetry.tower_key != topic_tower_key:
        raise ValueError("Telemetry tower_key does not match MQTT topic.")
    return telemetry


def present_sensor_fields(telemetry: TelemetryPayload) -> list[str]:
    return [field for field in SENSOR_FIELDS if getattr(telemetry, field) is not None]


__all__ = [
    "TelemetryPayload",
    "ValidationError",
    "present_sensor_fields",
    "tower_key_from_topic",
    "validate_telemetry_message",
]
