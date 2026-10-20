import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Base, TelemetryReading, Tower
from app.telemetry_store import process_telemetry_message


def payload(tower_key: str, timestamp: str = "2026-08-20T12:00:00Z", **overrides):
    data = {
        "simulated": True,
        "tower_key": tower_key,
        "timestamp": timestamp,
        "ph": 6.1,
        "ec_ms_cm": 1.5,
        "water_temp_c": 21.0,
        "air_temp_c": 23.0,
        "humidity_pct": 60.0,
        "water_level_pct": 82.0,
        "ppfd_umol_m2_s": 320.0,
        "circulation_on": True,
    }
    data.update(overrides)
    return data


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def test_known_tower_is_mapped_and_persisted_with_missing_sensor_as_null(session: Session) -> None:
    tower = Tower(name="Simulator Tower 1", tower_key="sim-1")
    session.add(tower)
    session.commit()

    data = payload("sim-1")
    del data["ppfd_umol_m2_s"]
    status, reading = process_telemetry_message(
        session,
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(data),
    )

    assert status == "stored"
    assert reading is not None
    assert reading.tower_id == tower.id
    assert reading.recorded_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 8, 20, 12, 0, tzinfo=timezone.utc
    )
    assert reading.source == "simulator"
    assert reading.ppfd_umol_m2_s is None
    assert reading.circulation_on is True


def test_topic_payload_mismatch_creates_no_row(session: Session) -> None:
    session.add(Tower(name="Simulator Tower 1", tower_key="sim-1"))
    session.commit()

    with pytest.raises(ValueError, match="does not match"):
        process_telemetry_message(
            session,
            "hydroponics/towers/sim-2/telemetry",
            json.dumps(payload("sim-1")),
        )

    assert session.scalar(select(func.count()).select_from(TelemetryReading)) == 0


def test_unknown_tower_is_rejected_without_misattribution(session: Session) -> None:
    known = Tower(name="Known Tower", tower_key="sim-1")
    session.add(known)
    session.commit()

    status, reading = process_telemetry_message(
        session,
        "hydroponics/towers/sim-99/telemetry",
        json.dumps(payload("sim-99")),
    )

    assert status == "unknown_tower"
    assert reading is None
    assert session.scalar(select(func.count()).select_from(TelemetryReading)) == 0
    assert session.scalar(
        select(func.count()).select_from(TelemetryReading).where(
            TelemetryReading.tower_id == known.id
        )
    ) == 0


def test_replayed_same_tower_timestamp_is_not_inserted_twice(session: Session) -> None:
    session.add(Tower(name="Simulator Tower 1", tower_key="sim-1"))
    session.commit()
    message = json.dumps(payload("sim-1"))

    first_status, _ = process_telemetry_message(
        session,
        "hydroponics/towers/sim-1/telemetry",
        message,
    )
    second_status, _ = process_telemetry_message(
        session,
        "hydroponics/towers/sim-1/telemetry",
        message,
    )

    assert first_status == "stored"
    assert second_status == "duplicate"
    assert session.scalar(select(func.count()).select_from(TelemetryReading)) == 1


def test_multiple_towers_remain_separated(session: Session) -> None:
    first = Tower(name="Simulator Tower 1", tower_key="sim-1")
    second = Tower(name="Simulator Tower 2", tower_key="sim-2")
    session.add_all([first, second])
    session.commit()

    process_telemetry_message(
        session,
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(payload("sim-1", ph=6.0)),
    )
    process_telemetry_message(
        session,
        "hydroponics/towers/sim-2/telemetry",
        json.dumps(payload("sim-2", ph=6.4)),
    )

    first_readings = list(
        session.scalars(
            select(TelemetryReading).where(TelemetryReading.tower_id == first.id)
        )
    )
    second_readings = list(
        session.scalars(
            select(TelemetryReading).where(TelemetryReading.tower_id == second.id)
        )
    )
    assert [reading.ph for reading in first_readings] == [6.0]
    assert [reading.ph for reading in second_readings] == [6.4]


def test_equivalent_timezone_offsets_are_treated_as_same_replay(session: Session) -> None:
    session.add(Tower(name="Simulator Tower 1", tower_key="sim-1"))
    session.commit()

    first_status, first_reading = process_telemetry_message(
        session,
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(payload("sim-1", timestamp="2026-08-20T12:00:00Z")),
    )
    second_status, second_reading = process_telemetry_message(
        session,
        "hydroponics/towers/sim-1/telemetry",
        json.dumps(payload("sim-1", timestamp="2026-08-20T14:00:00+02:00")),
    )

    assert first_status == "stored"
    assert second_status == "duplicate"
    assert first_reading is not None
    assert second_reading is not None
    assert first_reading.id == second_reading.id
    assert session.scalar(select(func.count()).select_from(TelemetryReading)) == 1
