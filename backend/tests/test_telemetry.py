from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from app.telemetry import validate_telemetry_message


def valid_payload(**overrides):
    payload = {
        "simulated": True,
        "tower_key": "sim-1",
        "timestamp": "2026-08-20T12:00:00Z",
        "ph": 6.1,
        "ec_ms_cm": 1.5,
        "water_temp_c": 21.0,
        "air_temp_c": 23.0,
        "humidity_pct": 60.0,
        "water_level_pct": 82.0,
        "ppfd_umol_m2_s": 320.0,
        "circulation_on": True,
    }
    payload.update(overrides)
    return payload


def test_valid_telemetry_is_accepted():
    result = validate_telemetry_message(
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(valid_payload()),
    )
    assert result.tower_key == "sim-1"
    assert result.simulated is True


def test_missing_sensor_remains_missing_without_default():
    payload = valid_payload()
    del payload["ppfd_umol_m2_s"]

    result = validate_telemetry_message(
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(payload),
    )

    assert result.ppfd_umol_m2_s is None
    assert "ppfd_umol_m2_s" not in result.model_dump(exclude_none=True)


def test_malformed_payload_is_rejected():
    with pytest.raises(ValidationError):
        validate_telemetry_message(
            "hydroponics/towers/sim-1/telemetry",
            b'{"simulated": true, "tower_key": "sim-1", "ph": "bad"}',
        )


def test_topic_and_payload_tower_must_match():
    with pytest.raises(ValueError, match="does not match"):
        validate_telemetry_message(
            "hydroponics/towers/sim-2/telemetry",
            json.dumps(valid_payload(tower_key="sim-1")),
        )


def test_timezone_aware_timestamp_is_normalized_to_utc():
    result = validate_telemetry_message(
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(valid_payload(timestamp="2026-08-20T14:00:00+02:00")),
    )

    assert result.timestamp == datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert result.timestamp.utcoffset().total_seconds() == 0


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValidationError, match="explicit timezone offset or Z"):
        validate_telemetry_message(
            "hydroponics/towers/sim-1/telemetry",
            json.dumps(valid_payload(timestamp="2026-08-20T12:00:00")),
        )
