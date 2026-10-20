from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TelemetryReading, Tower
from app.rule_engine import evaluate_telemetry_reading
from app.telemetry import TelemetryPayload, validate_telemetry_message

PersistenceStatus = Literal["stored", "duplicate", "unknown_tower"]


def persist_telemetry(
    session: Session,
    telemetry: TelemetryPayload,
) -> tuple[PersistenceStatus, TelemetryReading | None]:
    """Map a validated tower_key to one Tower and persist one simulated reading."""
    tower = session.scalar(select(Tower).where(Tower.tower_key == telemetry.tower_key))
    if tower is None:
        return "unknown_tower", None

    existing = session.scalar(
        select(TelemetryReading).where(
            TelemetryReading.tower_id == tower.id,
            TelemetryReading.recorded_at == telemetry.timestamp,
        )
    )
    if existing is not None:
        return "duplicate", existing

    reading = TelemetryReading(
        tower_id=tower.id,
        recorded_at=telemetry.timestamp,
        source="simulator",
        ph=telemetry.ph,
        ec_ms_cm=telemetry.ec_ms_cm,
        water_temp_c=telemetry.water_temp_c,
        air_temp_c=telemetry.air_temp_c,
        humidity_pct=telemetry.humidity_pct,
        water_level_pct=telemetry.water_level_pct,
        ppfd_umol_m2_s=telemetry.ppfd_umol_m2_s,
        circulation_on=telemetry.circulation_on,
    )
    session.add(reading)
    session.flush()
    evaluate_telemetry_reading(session, reading)
    session.commit()
    session.refresh(reading)
    return "stored", reading


def process_telemetry_message(
    session: Session,
    topic: str,
    payload: bytes | str,
) -> tuple[PersistenceStatus, TelemetryReading | None]:
    telemetry = validate_telemetry_message(topic, payload)
    return persist_telemetry(session, telemetry)
