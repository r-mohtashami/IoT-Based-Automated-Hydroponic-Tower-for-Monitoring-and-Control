import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import ActionLog, ActuatorState, Alert, Base, PlantProfile, Planting, Recommendation, Tower
from app.recommendation_decisions import approve_recommendation, reject_recommendation
from app.seed_builtin_profiles import seed_builtin_profiles
from app.telemetry_store import process_telemetry_message


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def payload(tower_key: str, recorded_at: datetime, *, ph: float | None) -> str:
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
    with Session(engine) as db_session:
        yield db_session


def start_basil(session: Session, tower_key: str = "sim-decision") -> tuple[Tower, Planting]:
    seed_builtin_profiles(session)
    basil = session.scalar(select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic"))
    assert basil is not None
    tower = Tower(name="Decision Tower", tower_key=tower_key)
    session.add(tower)
    session.commit()
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=basil)
    session.add(planting)
    session.commit()
    return tower, planting


def process(session: Session, tower: Tower, recorded_at: datetime, *, ph: float | None) -> None:
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        payload(tower.tower_key, recorded_at, ph=ph),
    )


def first_recommendation(session: Session) -> Recommendation:
    recommendation = session.scalar(select(Recommendation).order_by(Recommendation.id))
    assert recommendation is not None
    return recommendation


def test_approval_revalidates_against_latest_telemetry_and_creates_no_execution(session: Session) -> None:
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=20), ph=5.2)
    process(session, tower, now - timedelta(seconds=5), ph=5.1)
    recommendation = first_recommendation(session)

    result = approve_recommendation(session, recommendation, now=now)

    assert result.valid is True
    assert recommendation.status == "approved"
    assert recommendation.reviewed_at is not None
    assert recommendation.revalidated_at is not None
    assert "Revalidation passed" in recommendation.decision_note
    log = session.scalar(select(ActionLog))
    assert log is not None
    assert log.event_type == "recommendation_approved"
    assert log.details["revalidation"] == "passed"
    assert log.details["current_evaluation"] == "below"
    assert log.details["execution_created"] is False
    assert session.scalar(select(func.count()).select_from(ActuatorState)) == 0


def test_pending_recommendation_can_be_rejected_with_reason(session: Session) -> None:
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=5), ph=5.2)
    recommendation = first_recommendation(session)

    reject_recommendation(session, recommendation, reason="Owner chose to keep monitoring.", now=now)

    assert recommendation.status == "rejected"
    assert recommendation.decision_note == "Owner chose to keep monitoring."
    assert recommendation.reviewed_at is not None
    assert recommendation.revalidated_at is None
    log = session.scalar(select(ActionLog))
    assert log is not None
    assert log.event_type == "recommendation_rejected"
    assert log.details["reason"] == "Owner chose to keep monitoring."


def test_recovery_before_approval_marks_recommendation_stale(session: Session) -> None:
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=10), ph=5.2)
    recommendation = first_recommendation(session)
    process(session, tower, now - timedelta(seconds=2), ph=5.8)

    session.refresh(recommendation)
    # Monitoring recovery requires consecutive readings, but approval still
    # revalidates against the latest telemetry and fails closed immediately.
    assert recommendation.status == "pending"
    result = approve_recommendation(session, recommendation, now=now)
    session.refresh(recommendation)
    assert result.valid is False
    assert result.details["current_evaluation"] == "within"
    assert recommendation.status == "stale"
    assert "returned within" in (recommendation.decision_note or "")


def test_direction_change_before_approval_marks_old_recommendation_stale(session: Session) -> None:
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=10), ph=5.2)
    old_recommendation = first_recommendation(session)
    process(session, tower, now - timedelta(seconds=2), ph=6.2)

    session.refresh(old_recommendation)
    assert old_recommendation.status == "stale"
    assert "different direction" in (old_recommendation.decision_note or "")
    with pytest.raises(ValueError, match="Only pending Recommendations"):
        approve_recommendation(session, old_recommendation, now=now)


def test_ended_planting_before_approval_marks_recommendation_stale(session: Session) -> None:
    tower, planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=5), ph=5.2)
    recommendation = first_recommendation(session)
    planting.status = "completed"
    planting.ended_at = now
    session.commit()

    result = approve_recommendation(session, recommendation, now=now)

    assert result.valid is False
    assert recommendation.status == "stale"
    assert "active planting" in result.reason


def test_missing_factor_in_latest_telemetry_blocks_approval(session: Session) -> None:
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=10), ph=5.2)
    recommendation = first_recommendation(session)
    process(session, tower, now - timedelta(seconds=2), ph=None)

    result = approve_recommendation(session, recommendation, now=now)

    assert result.valid is False
    assert recommendation.status == "stale"
    assert "does not contain the factor" in result.reason


def test_old_latest_telemetry_blocks_approval(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECOMMENDATION_MAX_TELEMETRY_AGE_SECONDS", "30")
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=31), ph=5.2)
    recommendation = first_recommendation(session)

    result = approve_recommendation(session, recommendation, now=now)

    assert result.valid is False
    assert recommendation.status == "stale"
    assert "freshness limit" in result.reason


def test_non_pending_recommendation_cannot_receive_second_decision(session: Session) -> None:
    tower, _planting = start_basil(session)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=5), ph=5.2)
    recommendation = first_recommendation(session)
    reject_recommendation(session, recommendation, now=now)

    with pytest.raises(ValueError, match="Only pending"):
        approve_recommendation(session, recommendation, now=now)
    with pytest.raises(ValueError, match="Only pending"):
        reject_recommendation(session, recommendation, now=now)


def test_approval_revalidation_uses_planting_snapshot_after_profile_change(session: Session) -> None:
    tower = Tower(name="Snapshot Decision Tower", tower_key="sim-decision-snapshot")
    profile = PlantProfile(
        name="Snapshot Decision Profile",
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

    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=5), ph=5.2)
    recommendation = first_recommendation(session)

    profile.targets = {
        "ph": {"min": 5.0, "max": 6.0, "unit": "pH"},
        "ec_ms_cm": {"min": 1.0, "max": 1.6, "unit": "mS/cm"},
    }
    session.commit()

    result = approve_recommendation(session, recommendation, now=now)

    assert result.valid is True
    assert recommendation.status == "approved"
    assert result.details["target_min"] == 5.5
    assert profile.targets["ph"]["min"] == 5.0
    assert planting.profile_snapshot["targets"]["ph"]["min"] == 5.5


def test_future_dated_telemetry_beyond_clock_skew_is_rejected(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECOMMENDATION_MAX_FUTURE_CLOCK_SKEW_SECONDS", "5")
    tower, _planting = start_basil(session, tower_key="sim-future-decision")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now + timedelta(seconds=10), ph=5.2)
    recommendation = first_recommendation(session)

    result = approve_recommendation(session, recommendation, now=now)

    assert result.valid is False
    assert recommendation.status == "stale"
    assert "too far in the future" in result.reason
    assert result.details["telemetry_future_skew_seconds"] == 10.0


def test_stale_loaded_second_decision_cannot_overwrite_first(tmp_path) -> None:
    database_path = tmp_path / "decision_race.db"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)

    with Session(engine) as setup_session:
        tower, _planting = start_basil(setup_session, tower_key="sim-decision-race")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        process(setup_session, tower, now - timedelta(seconds=2), ph=5.2)
        recommendation_id = first_recommendation(setup_session).id

    session_one = Session(engine, expire_on_commit=False)
    session_two = Session(engine, expire_on_commit=False)
    try:
        recommendation_one = session_one.get(Recommendation, recommendation_id)
        recommendation_two = session_two.get(Recommendation, recommendation_id)
        assert recommendation_one is not None and recommendation_two is not None
        assert recommendation_one.status == recommendation_two.status == "pending"

        approve_recommendation(session_one, recommendation_one, now=now)

        with pytest.raises(ValueError, match="Only pending Recommendations"):
            reject_recommendation(session_two, recommendation_two, now=now)

        with Session(engine) as verify_session:
            stored = verify_session.get(Recommendation, recommendation_id)
            assert stored is not None
            assert stored.status == "approved"
            assert verify_session.scalar(
                select(func.count()).select_from(ActionLog).where(
                    ActionLog.recommendation_id == recommendation_id
                )
            ) == 1
    finally:
        session_one.close()
        session_two.close()


def test_decision_and_action_log_roll_back_together_on_log_failure(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.recommendation_decisions as decisions

    tower, _planting = start_basil(session, tower_key="sim-decision-atomic")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    process(session, tower, now - timedelta(seconds=2), ph=5.2)
    recommendation = first_recommendation(session)
    recommendation_id = recommendation.id

    def add_invalid_log(
        db_session: Session,
        recommendation: Recommendation,
        *,
        event_type: str,
        status: str,
        details: dict[str, object],
        occurred_at: datetime,
    ) -> None:
        db_session.add(
            ActionLog(
                tower_id=recommendation.tower_id,
                recommendation_id=recommendation.id,
                event_type=None,  # type: ignore[arg-type]
                status=status,
                details=details,
                occurred_at=occurred_at,
            )
        )

    monkeypatch.setattr(decisions, "_log_decision", add_invalid_log)

    with pytest.raises(Exception):
        approve_recommendation(session, recommendation, now=now)

    session.expire_all()
    stored = session.get(Recommendation, recommendation_id)
    assert stored is not None
    assert stored.status == "pending"
    assert session.scalar(
        select(func.count()).select_from(ActionLog).where(
            ActionLog.recommendation_id == recommendation_id
        )
    ) == 0
