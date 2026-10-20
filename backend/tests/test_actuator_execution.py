import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actuator_execution import claim_execution, execute_recommendation
from app.actuator_mqtt import ACK_TOPIC_TEMPLATE, ActuatorAck, MqttExchangeResult, parse_ack
from app.models import ActionLog, ActuatorState, Base, PlantProfile, Planting, Recommendation, Tower
from app.recommendation_decisions import approve_recommendation
from app.seed_builtin_profiles import seed_builtin_profiles
from app.telemetry_store import process_telemetry_message


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def telemetry_payload(tower_key: str, recorded_at: datetime, *, ph: float | None) -> str:
    data = {
        "simulated": True,
        "tower_key": tower_key,
        "timestamp": timestamp(recorded_at),
        "ec_ms_cm": 1.4,
        "circulation_on": True,
    }
    if ph is not None:
        data["ph"] = ph
    return json.dumps(data)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session


def approved_basil_recommendation(
    session: Session,
    *,
    tower_key: str = "sim-execution",
    now: datetime | None = None,
) -> tuple[Tower, Planting, Recommendation, datetime]:
    checked_at = now or datetime.now(timezone.utc).replace(microsecond=0)
    seed_builtin_profiles(session)
    basil = session.scalar(select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic"))
    assert basil is not None
    tower = Tower(name="Execution Tower", tower_key=tower_key)
    session.add(tower)
    session.commit()
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=basil)
    session.add(planting)
    session.commit()
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower_key}/telemetry",
        telemetry_payload(tower_key, checked_at - timedelta(seconds=2), ph=5.2),
    )
    recommendation = session.scalar(select(Recommendation).order_by(Recommendation.id))
    assert recommendation is not None
    approval = approve_recommendation(session, recommendation, now=checked_at)
    assert approval.valid is True
    assert recommendation.status == "approved"
    return tower, planting, recommendation, checked_at


def successful_transport(claim, at: datetime) -> MqttExchangeResult:
    assert claim.command_id is not None
    assert claim.tower_key is not None
    assert claim.action is not None
    return MqttExchangeResult(
        command_sent=True,
        sent_at=at,
        ack=ActuatorAck(
            command_id=claim.command_id,
            tower_key=claim.tower_key,
            actuator_type=claim.action.actuator_type,
            state=claim.action.state,
            timestamp=at + timedelta(milliseconds=20),
        ),
    )


def test_approved_recommendation_revalidates_then_records_verified_simulated_execution(session: Session) -> None:
    tower, _planting, recommendation, now = approved_basil_recommendation(session)

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now,
        transport=lambda claim: successful_transport(claim, now),
    )

    assert result.recommendation_status == "executed"
    assert result.execution_status == "verified"
    assert result.command_sent is True
    assert result.execution_verified is True
    assert result.actuator_type == "ph_up_pump"
    assert result.desired_state == "completed"

    session.refresh(recommendation)
    assert recommendation.status == "executed"
    assert recommendation.executed_at is not None

    actuator_state = session.scalar(
        select(ActuatorState).where(
            ActuatorState.tower_id == tower.id,
            ActuatorState.actuator_type == "ph_up_pump",
        )
    )
    assert actuator_state is not None
    assert actuator_state.state == "completed"
    assert actuator_state.state_data["command_id"] == result.command_id
    assert actuator_state.state_data["verification_source"] == "simulator_ack"

    event_types = list(
        session.scalars(
            select(ActionLog.event_type)
            .where(ActionLog.recommendation_id == recommendation.id)
            .order_by(ActionLog.id)
        )
    )
    assert "execution_requested" in event_types
    assert "actuator_command_sent" in event_types
    assert "execution_verified" in event_types
    assert "awaiting_physical_outcome" in event_types


def test_command_sent_without_correlated_ack_is_not_verified(session: Session) -> None:
    _tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-unverified",
    )

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now,
        transport=lambda claim: MqttExchangeResult(
            command_sent=True,
            sent_at=now,
            ack=None,
            error="Simulator acknowledgement timed out.",
        ),
    )

    assert result.command_sent is True
    assert result.execution_verified is False
    assert result.execution_status == "failed"
    assert session.scalar(select(func.count()).select_from(ActuatorState)) == 0
    events = list(
        session.scalars(
            select(ActionLog.event_type).where(ActionLog.recommendation_id == recommendation.id)
        )
    )
    assert "actuator_command_sent" in events
    assert "execution_failed" in events
    assert "execution_verified" not in events


def test_duplicate_execution_is_rejected(session: Session) -> None:
    _tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-duplicate-execution",
    )
    execute_recommendation(
        session,
        recommendation.id,
        now=now,
        transport=lambda claim: successful_transport(claim, now),
    )

    with pytest.raises(ValueError, match="Only approved Recommendations"):
        execute_recommendation(
            session,
            recommendation.id,
            now=now,
            transport=lambda claim: successful_transport(claim, now),
        )

    assert session.scalar(
        select(func.count()).select_from(ActionLog).where(
            ActionLog.recommendation_id == recommendation.id,
            ActionLog.event_type == "execution_requested",
        )
    ) == 1


def no_transport_calls(counter: dict[str, int]):
    def transport(_claim):
        counter["calls"] += 1
        raise AssertionError("MQTT transport must not run when execution revalidation fails.")

    return transport


def test_recovery_before_execution_blocks_command(session: Session) -> None:
    tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-exec-recovery",
    )
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        telemetry_payload(tower.tower_key, now + timedelta(seconds=1), ph=5.8),
    )
    counter = {"calls": 0}

    session.refresh(recommendation)
    # Alert lifecycle recovery is debounced, but execution always revalidates
    # against the latest reading and must block immediately.
    assert recommendation.status == "approved"
    result = execute_recommendation(
        session,
        recommendation.id,
        now=now + timedelta(seconds=2),
        transport=no_transport_calls(counter),
    )
    session.refresh(recommendation)
    assert result.execution_status == "blocked"
    assert result.command_sent is False
    assert result.revalidation["current_evaluation"] == "within"
    assert recommendation.status == "stale"
    assert counter["calls"] == 0


def test_direction_change_before_execution_blocks_command(session: Session) -> None:
    tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-exec-direction",
    )
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        telemetry_payload(tower.tower_key, now + timedelta(seconds=1), ph=6.2),
    )
    counter = {"calls": 0}

    session.refresh(recommendation)
    assert recommendation.status == "stale"
    with pytest.raises(ValueError, match="Only approved Recommendations"):
        execute_recommendation(
            session,
            recommendation.id,
            now=now + timedelta(seconds=2),
            transport=no_transport_calls(counter),
        )
    assert counter["calls"] == 0


def test_ended_planting_before_execution_blocks_command(session: Session) -> None:
    _tower, planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-exec-ended",
    )
    planting.status = "completed"
    planting.ended_at = now + timedelta(seconds=1)
    session.commit()
    counter = {"calls": 0}

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now + timedelta(seconds=2),
        transport=no_transport_calls(counter),
    )

    assert result.execution_status == "blocked"
    assert "active planting" in result.reason
    assert counter["calls"] == 0


def test_missing_latest_factor_before_execution_blocks_command(session: Session) -> None:
    tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-exec-missing",
    )
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        telemetry_payload(tower.tower_key, now + timedelta(seconds=1), ph=None),
    )
    counter = {"calls": 0}

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now + timedelta(seconds=2),
        transport=no_transport_calls(counter),
    )

    assert result.execution_status == "blocked"
    assert "does not contain the factor" in result.reason
    assert counter["calls"] == 0


def test_stale_telemetry_before_execution_blocks_command(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECOMMENDATION_MAX_TELEMETRY_AGE_SECONDS", "30")
    _tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-exec-stale",
    )
    counter = {"calls": 0}

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now + timedelta(seconds=31),
        transport=no_transport_calls(counter),
    )

    assert result.execution_status == "blocked"
    assert "freshness limit" in result.reason
    assert counter["calls"] == 0


def test_future_dated_latest_telemetry_before_execution_blocks_command(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECOMMENDATION_MAX_FUTURE_CLOCK_SKEW_SECONDS", "5")
    tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-exec-future",
    )
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        telemetry_payload(tower.tower_key, now + timedelta(seconds=10), ph=5.1),
    )
    counter = {"calls": 0}

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now,
        transport=no_transport_calls(counter),
    )

    assert result.execution_status == "blocked"
    assert "too far in the future" in result.reason
    assert counter["calls"] == 0


def test_execution_revalidation_uses_planting_snapshot_after_profile_change(session: Session) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    tower = Tower(name="Execution Snapshot Tower", tower_key="sim-exec-snapshot")
    profile = PlantProfile(
        name="Execution Snapshot Profile",
        profile_type="user_custom",
        targets={
            "ph": {"min": 5.5, "max": 6.0, "unit": "pH"},
            "ec_ms_cm": {"min": 1.0, "max": 1.6, "unit": "mS/cm"},
        },
        source_references=[],
    )
    session.add_all([tower, profile])
    session.commit()
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add(planting)
    session.commit()
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        telemetry_payload(tower.tower_key, now - timedelta(seconds=2), ph=5.2),
    )
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None
    assert approve_recommendation(session, recommendation, now=now).valid is True

    profile.targets = {
        "ph": {"min": 5.0, "max": 6.0, "unit": "pH"},
        "ec_ms_cm": {"min": 1.0, "max": 1.6, "unit": "mS/cm"},
    }
    session.commit()

    result = execute_recommendation(
        session,
        recommendation.id,
        now=now,
        transport=lambda claim: successful_transport(claim, now),
    )

    assert result.execution_verified is True
    assert result.revalidation["target_min"] == 5.5
    assert profile.targets["ph"]["min"] == 5.0
    assert planting.profile_snapshot["targets"]["ph"]["min"] == 5.5


def test_ack_requires_exact_command_correlation() -> None:
    topic = ACK_TOPIC_TEMPLATE.format(tower_key="sim-1")
    payload = json.dumps(
        {
            "simulated": True,
            "tower_key": "sim-1",
            "command_id": "command-123",
            "actuator_type": "review_indicator",
            "state": "on",
            "status": "executed",
            "timestamp": "2026-08-31T12:00:00Z",
        }
    )

    assert parse_ack(
        topic,
        payload,
        expected_command_id="different-command",
        expected_tower_key="sim-1",
        expected_actuator_type="review_indicator",
        expected_state="on",
    ) is None

    ack = parse_ack(
        topic,
        payload,
        expected_command_id="command-123",
        expected_tower_key="sim-1",
        expected_actuator_type="review_indicator",
        expected_state="on",
    )
    assert ack is not None
    assert ack.command_id == "command-123"


def test_execution_claim_is_acquired_only_once_while_recommendation_stays_approved(session: Session) -> None:
    _tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-single-acquisition",
    )

    first = claim_execution(session, recommendation.id, now=now)
    assert first.allowed is True
    session.refresh(recommendation)
    assert recommendation.status == "approved"

    with pytest.raises(ValueError, match="already been requested"):
        claim_execution(session, recommendation.id, now=now)

    assert session.scalar(
        select(func.count()).select_from(ActionLog).where(
            ActionLog.recommendation_id == recommendation.id,
            ActionLog.event_type == "execution_requested",
        )
    ) == 1


def test_execution_requested_has_database_unique_acquisition_guard(session: Session) -> None:
    tower, _planting, recommendation, now = approved_basil_recommendation(
        session,
        tower_key="sim-unique-execution-request",
    )
    first = claim_execution(session, recommendation.id, now=now)
    assert first.allowed is True

    session.add(
        ActionLog(
            tower_id=tower.id,
            recommendation_id=recommendation.id,
            event_type="execution_requested",
            actuator_type="review_indicator",
            status="requested",
            details={"command_id": "duplicate"},
            occurred_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    assert session.scalar(
        select(func.count()).select_from(ActionLog).where(
            ActionLog.recommendation_id == recommendation.id,
            ActionLog.event_type == "execution_requested",
        )
    ) == 1


def test_manual_ec_high_execution_request_is_blocked_without_transport_or_failure(session: Session) -> None:
    checked_at = datetime.now(timezone.utc).replace(microsecond=0)
    seed_builtin_profiles(session)
    basil = session.scalar(select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic"))
    assert basil is not None
    tower = Tower(name="Manual EC Tower", tower_key="sim-manual-ec")
    session.add(tower)
    session.commit()
    session.add(Planting.from_profile(tower_id=tower.id, plant_profile=basil))
    session.commit()

    payload = json.dumps(
        {
            "simulated": True,
            "tower_key": tower.tower_key,
            "timestamp": timestamp(checked_at - timedelta(seconds=2)),
            "ph": 5.8,
            "ec_ms_cm": 2.1,
            "circulation_on": True,
        }
    )
    process_telemetry_message(session, f"hydroponics/towers/{tower.tower_key}/telemetry", payload)
    recommendation = session.scalar(select(Recommendation).order_by(Recommendation.id))
    assert recommendation is not None
    assert recommendation.action_type == "manual_ec_high_intervention"
    approve_recommendation(session, recommendation, now=checked_at)

    calls = {"count": 0}

    def transport(_claim):
        calls["count"] += 1
        raise AssertionError("Manual EC-high must never call actuator transport.")

    result = execute_recommendation(session, recommendation.id, now=checked_at, transport=transport)

    session.refresh(recommendation)
    assert calls["count"] == 0
    assert result.execution_status == "blocked"
    assert result.command_sent is False
    assert result.recommendation_status == "approved"
    assert recommendation.status == "approved"
    assert recommendation.revalidated_at is not None
    persisted_revalidated_at = recommendation.revalidated_at
    if persisted_revalidated_at.tzinfo is None:
        persisted_revalidated_at = persisted_revalidated_at.replace(tzinfo=timezone.utc)
    assert persisted_revalidated_at == checked_at
    event_types = list(
        session.scalars(select(ActionLog.event_type).where(ActionLog.recommendation_id == recommendation.id))
    )
    assert "execution_blocked" in event_types
    assert "execution_failed" not in event_types
    assert "actuator_command_sent" not in event_types
