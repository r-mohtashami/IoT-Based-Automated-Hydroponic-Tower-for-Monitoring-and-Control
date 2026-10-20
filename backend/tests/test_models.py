from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ActionLog,
    ActuatorState,
    Alert,
    Base,
    Planting,
    PlantProfile,
    Recommendation,
    TelemetryReading,
    Tower,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def add_tower_and_profile(session: Session) -> tuple[Tower, PlantProfile]:
    tower = Tower(name="Tower A")
    profile = PlantProfile(
        name="Test Profile",
        profile_type="user_custom",
        targets={},
        source_references=[],
    )
    session.add_all([tower, profile])
    session.commit()
    return tower, profile


def test_all_core_domain_tables_exist() -> None:
    assert set(Base.metadata.tables) == {
        "towers",
        "plant_profiles",
        "plantings",
        "telemetry_readings",
        "alerts",
        "recommendations",
        "actuator_states",
        "action_logs",
    }


def test_tower_cannot_have_two_active_plantings(session: Session) -> None:
    tower, profile = add_tower_and_profile(session)
    first = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add(first)
    session.commit()

    second = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add(second)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_completed_planting_does_not_block_new_active_planting(session: Session) -> None:
    tower, profile = add_tower_and_profile(session)
    old = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    old.status = "completed"
    old.ended_at = datetime.now(timezone.utc)
    current = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add_all([old, current])
    session.commit()

    assert session.query(Planting).filter_by(tower_id=tower.id).count() == 2


def test_planting_snapshot_stays_independent_from_profile(session: Session) -> None:
    tower = Tower(name="Tower Snapshot")
    profile = PlantProfile(
        name="Snapshot Profile",
        profile_type="user_custom",
        targets={"example_factor": {"unit": "example_unit", "value": 1}},
        source_references=[{"label": "example-source"}],
    )
    session.add_all([tower, profile])
    session.commit()

    planting = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add(planting)
    session.commit()
    planting_id = planting.id

    profile.targets = {"example_factor": {"unit": "example_unit", "value": 2}}
    profile.source_references = [{"label": "changed-source"}]
    session.commit()
    session.expire_all()

    stored_planting = session.get(Planting, planting_id)
    assert stored_planting is not None
    assert stored_planting.profile_snapshot["targets"]["example_factor"]["value"] == 1
    assert stored_planting.profile_snapshot["source_references"] == [
        {"label": "example-source"}
    ]


def test_telemetry_sensor_values_may_all_be_missing(session: Session) -> None:
    tower = Tower(name="Tower Missing Sensors")
    session.add(tower)
    session.commit()

    reading = TelemetryReading(
        tower_id=tower.id,
        recorded_at=datetime.now(timezone.utc),
        ph=None,
        ec_ms_cm=None,
        water_temp_c=None,
        air_temp_c=None,
        humidity_pct=None,
        water_level_pct=None,
        ppfd_umol_m2_s=None,
    )
    session.add(reading)
    session.commit()
    session.refresh(reading)

    assert reading.ph is None
    assert reading.ec_ms_cm is None
    assert reading.water_temp_c is None
    assert reading.air_temp_c is None
    assert reading.humidity_pct is None
    assert reading.water_level_pct is None
    assert reading.ppfd_umol_m2_s is None


def test_builtin_profile_is_distinct_and_immutable(session: Session) -> None:
    built_in = PlantProfile(
        name="Built-in Placeholder",
        profile_type="system_builtin",
        targets={},
        source_references=[],
    )
    session.add(built_in)
    session.commit()

    customized = PlantProfile(
        name="My Copy",
        profile_type="user_customized",
        source_profile_id=built_in.id,
        targets={},
        source_references=[],
    )
    session.add(customized)
    session.commit()

    assert built_in.is_builtin is True
    assert customized.is_builtin is False
    assert customized.source_profile_id == built_in.id

    built_in.name = "Attempted overwrite"
    with pytest.raises(ValueError, match="immutable"):
        session.commit()
    session.rollback()


def test_core_relationship_records_can_be_persisted(session: Session) -> None:
    tower, profile = add_tower_and_profile(session)
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add(planting)
    session.commit()

    alert = Alert(
        tower_id=tower.id,
        planting_id=planting.id,
        factor="example_factor",
        status="open",
        message="Example alert without agronomic target data.",
        detected_at=datetime.now(timezone.utc),
    )
    session.add(alert)
    session.commit()

    recommendation = Recommendation(
        alert_id=alert.id,
        tower_id=tower.id,
        planting_id=planting.id,
        status="pending",
        reason="Example recommendation lifecycle record.",
    )
    actuator_state = ActuatorState(
        tower_id=tower.id,
        actuator_type="example_actuator",
        state="off",
        observed_at=datetime.now(timezone.utc),
    )
    session.add_all([recommendation, actuator_state])
    session.commit()

    action_log = ActionLog(
        tower_id=tower.id,
        recommendation_id=recommendation.id,
        event_type="example_event",
        status="recorded",
        occurred_at=datetime.now(timezone.utc),
    )
    session.add(action_log)
    session.commit()

    assert recommendation.alert.id == alert.id
    assert recommendation.planting.id == planting.id
    assert action_log.recommendation.id == recommendation.id
    assert actuator_state.tower.id == tower.id


def test_recorded_at_column_is_timezone_aware() -> None:
    assert TelemetryReading.__table__.c.recorded_at.type.timezone is True
