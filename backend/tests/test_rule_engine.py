import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import ActionLog, Alert, Base, PlantProfile, Planting, Recommendation, Tower
from app.rule_engine import evaluate_telemetry_reading
from app.seed_builtin_profiles import seed_builtin_profiles
from app.telemetry_store import process_telemetry_message


def telemetry_payload(
    tower_key: str,
    timestamp: str,
    *,
    ph: float | None = 5.8,
    ec_ms_cm: float | None = 1.4,
) -> str:
    data = {
        "simulated": True,
        "tower_key": tower_key,
        "timestamp": timestamp,
        "water_temp_c": 21.0,
        "air_temp_c": 23.0,
        "humidity_pct": 60.0,
        "water_level_pct": 82.0,
        "ppfd_umol_m2_s": 320.0,
        "circulation_on": True,
    }
    if ph is not None:
        data["ph"] = ph
    if ec_ms_cm is not None:
        data["ec_ms_cm"] = ec_ms_cm
    return json.dumps(data)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def start_builtin_planting(session: Session, tower_key: str = "sim-1") -> tuple[Tower, Planting, PlantProfile]:
    seed_builtin_profiles(session)
    basil = session.scalar(
        select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
    )
    assert basil is not None

    tower = Tower(name="Rule Test Tower", tower_key=tower_key)
    session.add(tower)
    session.commit()

    planting = Planting.from_profile(tower_id=tower.id, plant_profile=basil)
    session.add(planting)
    session.commit()
    return tower, planting, basil


def process(
    session: Session,
    tower_key: str,
    timestamp: str,
    *,
    ph: float | None = 5.8,
    ec_ms_cm: float | None = 1.4,
):
    return process_telemetry_message(
        session,
        f"hydroponics/towers/{tower_key}/telemetry",
        telemetry_payload(
            tower_key,
            timestamp,
            ph=ph,
            ec_ms_cm=ec_ms_cm,
        ),
    )


def test_below_ph_creates_traceable_alert_and_review_only_recommendation(session: Session) -> None:
    tower, planting, _profile = start_builtin_planting(session)

    status, reading = process(
        session,
        tower.tower_key,
        "2026-08-26T12:00:00Z",
        ph=5.2,
        ec_ms_cm=1.4,
    )

    assert status == "stored"
    assert reading is not None
    alert = session.scalar(select(Alert))
    recommendation = session.scalar(select(Recommendation))
    assert alert is not None
    assert alert.tower_id == tower.id
    assert alert.planting_id == planting.id
    assert alert.factor == "ph"
    assert alert.status == "open"
    assert alert.observed_value == 5.2
    assert alert.details["direction"] == "below"
    assert alert.details["target_min"] == 5.5
    assert alert.details["target_max"] == 6.0
    assert alert.details["unit"] == "pH"
    assert alert.details["telemetry_reading_id"] == reading.id
    assert alert.details["target_source_reference_id"] == "osu-hla-6722-table-2"
    assert alert.details["target_provenance"] == "system_evidence_backed"
    assert "below the configured target range" in alert.message

    assert recommendation is not None
    assert recommendation.alert_id == alert.id
    assert recommendation.action_type == "increase_ph"
    assert recommendation.action_parameters["executable"] is True
    assert "outside the configured target range" in recommendation.reason
    lowered = recommendation.reason.lower()
    assert "dose" not in lowered
    assert "pump" not in lowered
    assert "duration" not in lowered
    assert "emergency" not in lowered
    assert "safe" not in lowered


def test_above_ec_creates_above_target_alert(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    process(
        session,
        tower.tower_key,
        "2026-08-26T12:05:00Z",
        ph=5.8,
        ec_ms_cm=1.9,
    )

    alert = session.scalar(select(Alert))
    assert alert is not None
    assert alert.factor == "ec_ms_cm"
    assert alert.observed_value == 1.9
    assert alert.details["direction"] == "above"
    assert alert.details["target_min"] == 1.0
    assert alert.details["target_max"] == 1.6
    assert alert.details["unit"] == "mS/cm"


def test_within_range_creates_no_alert_or_recommendation(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    process(
        session,
        tower.tower_key,
        "2026-08-26T12:10:00Z",
        ph=5.8,
        ec_ms_cm=1.4,
    )

    assert session.scalar(select(func.count()).select_from(Alert)) == 0
    assert session.scalar(select(func.count()).select_from(Recommendation)) == 0


def test_missing_supported_sensor_is_not_monitored_and_creates_no_alert(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    _status, reading = process(
        session,
        tower.tower_key,
        "2026-08-26T12:15:00Z",
        ph=None,
        ec_ms_cm=1.4,
    )
    assert reading is not None

    evaluations = evaluate_telemetry_reading(session, reading)
    ph = next(item for item in evaluations if item.factor == "ph")
    assert ph.status == "not_monitored"
    assert ph.observed_value is None
    assert session.scalar(select(func.count()).select_from(Alert)) == 0


def test_environment_target_creates_manual_review_recommendation(session: Session) -> None:
    tower = Tower(name="Environment Review Tower", tower_key="sim-environment")
    profile = PlantProfile(
        name="User Environment Profile",
        profile_type="user_custom",
        targets={
            "water_temp_c": {
                "min": 18.0,
                "max": 22.0,
                "unit": "°C",
                "provenance": "user_configured",
            }
        },
        source_references=[],
    )
    session.add_all([tower, profile])
    session.commit()
    session.add(Planting.from_profile(tower_id=tower.id, plant_profile=profile))
    session.commit()

    payload = json.loads(telemetry_payload(tower.tower_key, "2026-08-26T12:20:00Z"))
    payload["water_temp_c"] = 24.0
    status, reading = process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        json.dumps(payload),
    )
    assert status == "stored"
    assert reading is not None

    evaluations = evaluate_telemetry_reading(session, reading)
    statuses = {item.factor: item.status for item in evaluations}
    assert statuses["water_temp_c"] == "above"
    assert statuses["ph"] == "not_evaluated"
    assert statuses["ec_ms_cm"] == "not_evaluated"

    alert = session.scalar(select(Alert))
    recommendation = session.scalar(select(Recommendation))
    assert alert is not None and alert.factor == "water_temp_c"
    assert recommendation is not None
    assert recommendation.action_type == "manual_environment_review"
    assert recommendation.action_parameters["executable"] is False
    assert "Manual review is required" in recommendation.reason


def test_tower_without_active_planting_gets_no_crop_target_alert(session: Session) -> None:
    tower = Tower(name="No Planting Tower", tower_key="sim-empty")
    session.add(tower)
    session.commit()

    process(
        session,
        tower.tower_key,
        "2026-08-26T12:25:00Z",
        ph=2.0,
        ec_ms_cm=5.0,
    )

    assert session.scalar(select(func.count()).select_from(Alert)) == 0
    assert session.scalar(select(func.count()).select_from(Recommendation)) == 0


def test_active_planting_uses_snapshot_after_current_profile_changes(session: Session) -> None:
    tower = Tower(name="Snapshot Rule Tower", tower_key="sim-snapshot")
    profile = PlantProfile(
        name="Snapshot Rule Profile",
        profile_type="user_custom",
        targets={
            "ph": {"min": 5.5, "max": 6.0, "unit": "pH"},
            "ec_ms_cm": {"min": 1.0, "max": 1.6, "unit": "mS/cm"},
        },
        source_references=[{"label": "owner-configured-test"}],
    )
    session.add_all([tower, profile])
    session.commit()
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=profile)
    session.add(planting)
    session.commit()

    profile.targets = {
        "ph": {"min": 5.0, "max": 7.0, "unit": "pH"},
        "ec_ms_cm": {"min": 1.0, "max": 1.6, "unit": "mS/cm"},
    }
    session.commit()

    process(
        session,
        tower.tower_key,
        "2026-08-26T12:30:00Z",
        ph=6.5,
        ec_ms_cm=1.4,
    )

    alert = session.scalar(select(Alert))
    assert alert is not None
    assert alert.factor == "ph"
    assert alert.details["direction"] == "above"
    assert alert.details["target_min"] == 5.5
    assert alert.details["target_max"] == 6.0
    assert planting.profile_snapshot["targets"]["ph"]["max"] == 6.0
    assert profile.targets["ph"]["max"] == 7.0


def test_repeated_same_condition_refreshes_one_open_alert_without_recommendation_spam(
    session: Session,
) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    process(session, tower.tower_key, "2026-08-26T12:35:00Z", ph=5.2, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T12:36:00Z", ph=5.1, ec_ms_cm=1.4)

    alerts = list(session.scalars(select(Alert)))
    recommendations = list(session.scalars(select(Recommendation)))
    assert len(alerts) == 1
    assert len(recommendations) == 1
    assert alerts[0].status == "open"
    assert alerts[0].observed_value == 5.1
    assert alerts[0].details["recorded_at"] == "2026-08-26T12:36:00+00:00"


def test_return_to_range_resolves_open_alert_without_creating_new_one(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    process(session, tower.tower_key, "2026-08-26T12:40:00Z", ph=5.2, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T12:40:50Z", ph=5.8, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T12:40:55Z", ph=5.8, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T12:41:00Z", ph=5.8, ec_ms_cm=1.4)

    alerts = list(session.scalars(select(Alert)))
    assert len(alerts) == 1
    assert alerts[0].status == "resolved"
    assert alerts[0].resolved_at is not None
    assert alerts[0].observed_value == 5.8
    assert alerts[0].details["resolved_observed_value"] == 5.8
    recommendations = list(session.scalars(select(Recommendation)))
    assert len(recommendations) == 1
    assert recommendations[0].status == "stale"
    stale_event = session.scalar(
        select(ActionLog).where(
            ActionLog.recommendation_id == recommendations[0].id,
            ActionLog.event_type == "recommendation_stale",
        )
    )
    assert stale_event is not None
    assert stale_event.details["condition_recovered_before_execution"] is True


def test_approved_but_unexecuted_recommendation_becomes_stale_after_recovery(session: Session) -> None:
    from app.recommendation_decisions import approve_recommendation
    from datetime import timedelta

    tower, _planting, _profile = start_builtin_planting(session)
    base = datetime(2026, 8, 26, 12, 42, tzinfo=timezone.utc)
    process(session, tower.tower_key, base.isoformat().replace("+00:00", "Z"), ph=6.2, ec_ms_cm=1.4)
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None
    approve_recommendation(session, recommendation, now=base + timedelta(seconds=1))
    assert recommendation.status == "approved"

    for seconds in (2, 3, 4):
        process(
            session,
            tower.tower_key,
            (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
            ph=5.8,
            ec_ms_cm=1.4,
        )

    session.refresh(recommendation)
    assert recommendation.status == "stale"
    assert "confirm recovery" in (recommendation.decision_note or "")


def test_next_telemetry_repairs_historical_pending_recommendation_whose_alert_is_already_resolved(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)
    process(session, tower.tower_key, "2026-08-26T12:44:00Z", ph=6.2, ec_ms_cm=1.4)
    alert = session.scalar(select(Alert))
    recommendation = session.scalar(select(Recommendation))
    assert alert is not None and recommendation is not None

    # Reproduce legacy persisted data: resolved Alert but still-pending Recommendation.
    alert.status = "resolved"
    alert.resolved_at = datetime(2026, 8, 26, 12, 45, tzinfo=timezone.utc)
    session.commit()
    assert recommendation.status == "pending"

    process(session, tower.tower_key, "2026-08-26T12:46:00Z", ph=5.8, ec_ms_cm=1.4)

    session.refresh(recommendation)
    assert recommendation.status == "stale"
    assert "related Alert is no longer open" in (recommendation.decision_note or "")


def test_next_telemetry_closes_open_alert_from_previous_planting(session: Session) -> None:
    tower, old_planting, profile = start_builtin_planting(session)
    process(session, tower.tower_key, "2026-08-26T12:47:00Z", ph=6.2, ec_ms_cm=1.4)
    old_alert = session.scalar(select(Alert))
    old_recommendation = session.scalar(select(Recommendation))
    assert old_alert is not None and old_recommendation is not None

    old_planting.status = "completed"
    old_planting.ended_at = datetime(2026, 8, 26, 12, 48, tzinfo=timezone.utc)
    new_planting = Planting.from_profile(
        tower_id=tower.id,
        plant_profile=profile,
        started_at=datetime(2026, 8, 26, 12, 49, tzinfo=timezone.utc),
    )
    session.add(new_planting)
    session.commit()

    process(session, tower.tower_key, "2026-08-26T12:50:00Z", ph=5.8, ec_ms_cm=1.4)

    session.refresh(old_alert)
    session.refresh(old_recommendation)
    assert old_alert.status == "resolved"
    assert old_alert.details["resolved_without_recovery"] is True
    assert old_recommendation.status == "stale"
    assert session.scalar(
        select(func.count()).select_from(Alert).where(
            Alert.planting_id == new_planting.id,
            Alert.status == "open",
        )
    ) == 0


def test_target_min_equality_is_within(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    _status, reading = process(
        session,
        tower.tower_key,
        "2026-08-26T12:50:00Z",
        ph=5.5,
        ec_ms_cm=1.4,
    )
    assert reading is not None
    evaluations = evaluate_telemetry_reading(session, reading)
    ph = next(item for item in evaluations if item.factor == "ph")

    assert ph.status == "within"
    assert session.scalar(select(func.count()).select_from(Alert)) == 0


def test_target_max_equality_is_within(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    _status, reading = process(
        session,
        tower.tower_key,
        "2026-08-26T12:51:00Z",
        ph=6.0,
        ec_ms_cm=1.4,
    )
    assert reading is not None
    evaluations = evaluate_telemetry_reading(session, reading)
    ph = next(item for item in evaluations if item.factor == "ph")

    assert ph.status == "within"
    assert session.scalar(select(func.count()).select_from(Alert)) == 0


def test_direct_below_to_above_resolves_old_direction(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    process(session, tower.tower_key, "2026-08-26T12:52:00Z", ph=5.2, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T12:53:00Z", ph=6.2, ec_ms_cm=1.4)

    alerts = list(session.scalars(select(Alert).order_by(Alert.id)))
    assert len(alerts) == 2
    assert alerts[0].details["direction"] == "below"
    assert alerts[0].status == "resolved"
    assert alerts[1].details["direction"] == "above"
    assert alerts[1].status == "open"
    assert sum(alert.status == "open" for alert in alerts) == 1
    recommendations = list(session.scalars(select(Recommendation).order_by(Recommendation.id)))
    assert [item.status for item in recommendations] == ["stale", "pending"]


def test_direct_above_to_below_resolves_old_direction(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)

    process(session, tower.tower_key, "2026-08-26T12:54:00Z", ph=6.2, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T12:55:00Z", ph=5.2, ec_ms_cm=1.4)

    alerts = list(session.scalars(select(Alert).order_by(Alert.id)))
    assert len(alerts) == 2
    assert alerts[0].details["direction"] == "above"
    assert alerts[0].status == "resolved"
    assert alerts[1].details["direction"] == "below"
    assert alerts[1].status == "open"
    assert sum(alert.status == "open" for alert in alerts) == 1


def test_ec_high_is_manual_intervention_and_not_executable(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session)
    process(session, tower.tower_key, "2026-08-26T14:00:00Z", ph=5.8, ec_ms_cm=2.1)
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None
    assert recommendation.action_type == "manual_ec_high_intervention"
    assert recommendation.action_parameters["executable"] is False
    assert "no automatic EC-high actuator" in recommendation.reason


def test_post_action_telemetry_recovery_resolves_alert_and_logs_recovered(session: Session) -> None:
    from app.actuator_execution import execute_recommendation
    from app.actuator_mqtt import ActuatorAck, MqttExchangeResult
    from app.recommendation_decisions import approve_recommendation
    from datetime import datetime, timedelta, timezone

    tower, _planting, _profile = start_builtin_planting(session)
    base = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
    process(session, tower.tower_key, base.isoformat().replace('+00:00','Z'), ph=5.2, ec_ms_cm=1.4)
    rec = session.scalar(select(Recommendation))
    assert rec is not None
    approve_recommendation(session, rec, now=base + timedelta(seconds=1))
    def transport(claim):
        at = base + timedelta(seconds=2)
        return MqttExchangeResult(True, at, ActuatorAck(claim.command_id, claim.tower_key, claim.action.actuator_type, claim.action.state, at))
    execute_recommendation(session, rec.id, now=base + timedelta(seconds=1), transport=transport)
    process(session, tower.tower_key, (base + timedelta(seconds=3)).isoformat().replace('+00:00','Z'), ph=5.7, ec_ms_cm=1.4)
    alert = session.scalar(select(Alert))
    assert alert.status == "resolved"
    event = session.scalar(select(ActionLog).where(ActionLog.recommendation_id == rec.id, ActionLog.event_type == "physical_outcome_recovered"))
    assert event is not None


def test_unresolved_executed_step_creates_one_follow_up_after_observation_window(session: Session) -> None:
    from app.actuator_execution import execute_recommendation
    from app.actuator_mqtt import ActuatorAck, MqttExchangeResult
    from app.recommendation_decisions import approve_recommendation
    from datetime import datetime, timedelta, timezone

    tower, _planting, _profile = start_builtin_planting(session)
    base = datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc)
    process(session, tower.tower_key, base.isoformat().replace('+00:00','Z'), ph=5.0, ec_ms_cm=1.4)
    rec = session.scalar(select(Recommendation))
    approve_recommendation(session, rec, now=base + timedelta(seconds=1))
    def transport(claim):
        at = base + timedelta(seconds=2)
        return MqttExchangeResult(True, at, ActuatorAck(claim.command_id, claim.tower_key, claim.action.actuator_type, claim.action.state, at))
    execute_recommendation(session, rec.id, now=base + timedelta(seconds=1), transport=transport)
    process(session, tower.tower_key, (base + timedelta(seconds=15)).isoformat().replace('+00:00','Z'), ph=5.1, ec_ms_cm=1.4)
    process(session, tower.tower_key, (base + timedelta(seconds=30)).isoformat().replace('+00:00','Z'), ph=5.15, ec_ms_cm=1.4)
    process(session, tower.tower_key, (base + timedelta(seconds=45)).isoformat().replace('+00:00','Z'), ph=5.2, ec_ms_cm=1.4)
    process(session, tower.tower_key, (base + timedelta(seconds=62)).isoformat().replace('+00:00','Z'), ph=5.25, ec_ms_cm=1.4)
    recs = list(session.scalars(select(Recommendation).order_by(Recommendation.id)))
    assert len(recs) == 2
    assert recs[0].status == "executed"
    assert recs[1].status == "pending"
    assert recs[1].action_type == "increase_ph"


def test_delayed_older_telemetry_is_stored_but_cannot_rewrite_current_alert_state(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session, tower_key="sim-delayed")

    process(session, tower.tower_key, "2026-08-26T18:00:00Z", ph=6.2, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T18:01:40Z", ph=5.8, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T18:01:50Z", ph=5.8, ec_ms_cm=1.4)
    process(session, tower.tower_key, "2026-08-26T18:02:00Z", ph=5.8, ec_ms_cm=1.4)

    alert = session.scalar(select(Alert).order_by(Alert.id))
    assert alert is not None
    assert alert.status == "resolved"
    assert alert.observed_value == 5.8
    resolved_at = alert.resolved_at

    status, delayed = process(session, tower.tower_key, "2026-08-26T18:01:00Z", ph=6.3, ec_ms_cm=1.4)
    assert status == "stored"
    assert delayed is not None

    alerts = list(session.scalars(select(Alert).order_by(Alert.id)))
    assert len(alerts) == 1
    assert alerts[0].status == "resolved"
    assert alerts[0].observed_value == 5.8
    assert alerts[0].resolved_at == resolved_at


def test_direction_change_is_not_recorded_as_recovery(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session, tower_key="sim-direction")
    process(session, tower.tower_key, "2026-08-26T19:00:00Z", ph=5.2, ec_ms_cm=1.4)
    first_rec = session.scalar(select(Recommendation).order_by(Recommendation.id))
    assert first_rec is not None

    process(session, tower.tower_key, "2026-08-26T19:01:00Z", ph=6.2, ec_ms_cm=1.4)

    first_alert = session.scalar(select(Alert).where(Alert.id == first_rec.alert_id))
    assert first_alert is not None
    assert first_alert.status == "resolved"
    assert first_alert.details["resolved_without_recovery"] is True
    assert "changed direction" in first_alert.details["resolution_reason"]

    stale_event = session.scalar(
        select(ActionLog).where(
            ActionLog.recommendation_id == first_rec.id,
            ActionLog.event_type == "recommendation_stale",
        )
    )
    assert stale_event is not None
    assert stale_event.details["condition_recovered_before_execution"] is False

def test_boundary_noise_does_not_churn_alerts_or_recommendations(session: Session) -> None:
    tower, _planting, _profile = start_builtin_planting(session, tower_key="sim-boundary")

    # First excursion is reported normally.
    process(session, tower.tower_key, "2026-08-26T20:00:00Z", ph=5.8, ec_ms_cm=1.61)
    first_alert = session.scalar(select(Alert).where(Alert.factor == "ec_ms_cm"))
    assert first_alert is not None
    first_recommendation = session.scalar(
        select(Recommendation).where(Recommendation.alert_id == first_alert.id)
    )
    assert first_recommendation is not None

    # Boundary noise must not resolve the open condition on one in-range sample.
    process(session, tower.tower_key, "2026-08-26T20:00:05Z", ph=5.8, ec_ms_cm=1.599)
    process(session, tower.tower_key, "2026-08-26T20:00:10Z", ph=5.8, ec_ms_cm=1.601)
    process(session, tower.tower_key, "2026-08-26T20:00:15Z", ph=5.8, ec_ms_cm=1.599)
    process(session, tower.tower_key, "2026-08-26T20:00:20Z", ph=5.8, ec_ms_cm=1.601)
    session.refresh(first_alert)
    session.refresh(first_recommendation)
    assert first_alert.status == "open"
    assert first_recommendation.status == "pending"
    assert session.scalar(select(func.count()).select_from(Alert)) == 1
    assert session.scalar(select(func.count()).select_from(Recommendation)) == 1

    # Three consecutive in-range readings confirm recovery exactly once.
    process(session, tower.tower_key, "2026-08-26T20:00:25Z", ph=5.8, ec_ms_cm=1.599)
    process(session, tower.tower_key, "2026-08-26T20:00:30Z", ph=5.8, ec_ms_cm=1.598)
    process(session, tower.tower_key, "2026-08-26T20:00:35Z", ph=5.8, ec_ms_cm=1.597)
    session.refresh(first_alert)
    session.refresh(first_recommendation)
    assert first_alert.status == "resolved"
    assert first_recommendation.status == "stale"
    assert session.scalar(select(func.count()).select_from(Alert)) == 1
    assert session.scalar(select(func.count()).select_from(Recommendation)) == 1

    # One or two noisy samples above the boundary must not reopen it.
    process(session, tower.tower_key, "2026-08-26T20:00:40Z", ph=5.8, ec_ms_cm=1.601)
    process(session, tower.tower_key, "2026-08-26T20:00:45Z", ph=5.8, ec_ms_cm=1.602)
    assert session.scalar(select(func.count()).select_from(Alert)) == 1
    assert session.scalar(select(func.count()).select_from(Recommendation)) == 1

    # A sustained third out-of-range reading reopens exactly once.
    process(session, tower.tower_key, "2026-08-26T20:00:50Z", ph=5.8, ec_ms_cm=1.603)
    alerts = list(session.scalars(select(Alert).order_by(Alert.id)))
    recommendations = list(session.scalars(select(Recommendation).order_by(Recommendation.id)))
    assert len(alerts) == 2
    assert len(recommendations) == 2
    assert alerts[-1].status == "open"
    assert recommendations[-1].status == "pending"

