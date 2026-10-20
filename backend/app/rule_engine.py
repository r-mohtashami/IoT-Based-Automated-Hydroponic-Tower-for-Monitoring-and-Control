from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from typing import Literal
from datetime import timezone
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionLog, Alert, Planting, Recommendation, TelemetryReading

EvaluationStatus = Literal["below", "above", "within", "not_monitored", "not_evaluated"]
SUPPORTED_FACTORS = {
    "ph": "pH",
    "ec_ms_cm": "EC",
    "water_temp_c": "Water temperature",
    "air_temp_c": "Air temperature",
    "humidity_pct": "Humidity",
    "ppfd_umol_m2_s": "PPFD",
}
UNITS = {
    "ph": "pH",
    "ec_ms_cm": "mS/cm",
    "water_temp_c": "°C",
    "air_temp_c": "°C",
    "humidity_pct": "%",
    "ppfd_umol_m2_s": "µmol/m²/s",
}
# Deterministic application policy. No autonomous repeat execution.
OUTCOME_OBSERVATION_SECONDS = 60.0
# Monitoring debounce only; these do not change any crop target. A pending
# condition must be stably back in range before it is resolved, and after a
# genuine recovery it must be stably out of range before it is reopened. This
# prevents sensor noise near a target boundary from churning lifecycle records.
ALERT_RECOVERY_CONFIRMATION_READINGS = max(1, int(os.getenv("ALERT_RECOVERY_CONFIRMATION_READINGS", "3")))
ALERT_REOPEN_CONFIRMATION_READINGS = max(1, int(os.getenv("ALERT_REOPEN_CONFIRMATION_READINGS", "3")))


@dataclass(frozen=True)
class FactorEvaluation:
    factor: str
    status: EvaluationStatus
    observed_value: float | None = None
    target_min: float | None = None
    target_max: float | None = None
    unit: str | None = None
    source_reference_id: str | None = None
    provenance: str | None = None



def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    return float(value)


def _target_range(targets: object, factor: str) -> tuple[float, float, str | None, str | None, str | None] | None:
    if not isinstance(targets, dict) or not isinstance(targets.get(factor), dict):
        return None
    target = targets[factor]
    minimum, maximum = _number(target.get("min")), _number(target.get("max"))
    if minimum is None or maximum is None or minimum > maximum:
        return None
    unit = target.get("unit") if isinstance(target.get("unit"), str) else UNITS.get(factor)
    reference_id = target.get("source_reference_id") if isinstance(target.get("source_reference_id"), str) else None
    provenance = target.get("provenance") if isinstance(target.get("provenance"), str) else None
    return minimum, maximum, unit, reference_id, provenance


def _active_planting(session: Session, tower_id: int) -> Planting | None:
    return session.scalar(select(Planting).where(Planting.tower_id == tower_id, Planting.status == "active"))


def _open_factor_alerts(session: Session, *, tower_id: int, planting_id: int, factor: str) -> list[Alert]:
    return list(session.scalars(select(Alert).where(
        Alert.tower_id == tower_id, Alert.planting_id == planting_id,
        Alert.factor == factor, Alert.status == "open")))


def _direction(alert: Alert) -> str | None:
    details = alert.details if isinstance(alert.details, dict) else {}
    value = details.get("direction")
    return value if value in {"below", "above"} else None


def _resolve_alerts(
    alerts: list[Alert],
    reading: TelemetryReading,
    *,
    recovered: bool = True,
    reason: str | None = None,
) -> None:
    for alert in alerts:
        observed = getattr(reading, alert.factor, None)
        details = dict(alert.details) if isinstance(alert.details, dict) else {}
        details.update({
            "resolved_telemetry_reading_id": reading.id,
            "resolved_recorded_at": reading.recorded_at.isoformat(),
            "resolved_observed_value": observed,
            "resolved_without_recovery": not recovered,
        })
        if reason:
            details["resolution_reason"] = reason
        alert.details = details
        alert.observed_value = observed
        alert.status = "resolved"
        alert.resolved_at = reading.recorded_at


def _invalidate_unexecuted_recommendations(
    session: Session,
    alerts: list[Alert],
    reading: TelemetryReading,
    *,
    reason: str,
    recovered_before_execution: bool,
) -> None:
    alert_ids = [alert.id for alert in alerts if alert.id is not None]
    if not alert_ids:
        return
    recommendations = list(
        session.scalars(
            select(Recommendation).where(
                Recommendation.alert_id.in_(alert_ids),
                Recommendation.status.in_(["pending", "approved"]),
            )
        )
    )
    for recommendation in recommendations:
        recommendation.status = "stale"
        recommendation.revalidated_at = reading.recorded_at
        recommendation.decision_note = reason
        session.add(
            ActionLog(
                tower_id=recommendation.tower_id,
                recommendation_id=recommendation.id,
                event_type="recommendation_stale",
                actuator_type=None,
                status="stale",
                details={
                    "reason": reason,
                    "telemetry_reading_id": reading.id,
                    "recorded_at": reading.recorded_at.isoformat(),
                    "condition_recovered_before_execution": recovered_before_execution,
                },
                occurred_at=reading.recorded_at,
            )
        )


def _invalidate_orphaned_recommendations(
    session: Session,
    reading: TelemetryReading,
    planting: Planting | None,
) -> None:
    """Defensively close old actionable records that no longer have live planting/Alert context."""
    candidates = list(
        session.scalars(
            select(Recommendation).where(
                Recommendation.tower_id == reading.tower_id,
                Recommendation.status.in_(["pending", "approved"]),
            )
        )
    )
    for recommendation in candidates:
        alert = session.get(Alert, recommendation.alert_id)
        if planting is None or recommendation.planting_id != planting.id:
            reason = "The planting associated with this recommendation is no longer active."
        elif alert is None or alert.status != "open":
            reason = "The related Alert is no longer open, so this recommendation is no longer applicable."
        else:
            continue
        recommendation.status = "stale"
        recommendation.revalidated_at = reading.recorded_at
        recommendation.decision_note = reason
        session.add(
            ActionLog(
                tower_id=recommendation.tower_id,
                recommendation_id=recommendation.id,
                event_type="recommendation_stale",
                actuator_type=None,
                status="stale",
                details={
                    "reason": reason,
                    "telemetry_reading_id": reading.id,
                    "recorded_at": reading.recorded_at.isoformat(),
                    "orphaned_operational_state": True,
                },
                occurred_at=reading.recorded_at,
            )
        )


def _resolve_orphaned_alerts(
    session: Session,
    reading: TelemetryReading,
    planting: Planting | None,
) -> None:
    alerts = list(
        session.scalars(
            select(Alert).where(
                Alert.tower_id == reading.tower_id,
                Alert.status == "open",
            )
        )
    )
    for alert in alerts:
        if planting is not None and alert.planting_id == planting.id:
            continue
        details = dict(alert.details) if isinstance(alert.details, dict) else {}
        details.update(
            {
                "resolution_reason": "The planting associated with this Alert is no longer active.",
                "resolved_without_recovery": True,
                "resolved_recorded_at": reading.recorded_at.isoformat(),
            }
        )
        alert.details = details
        alert.status = "resolved"
        alert.resolved_at = reading.recorded_at


def _recommendation_action(factor: str, direction: str) -> tuple[str, bool]:
    if factor == "ph":
        return ("increase_ph" if direction == "below" else "decrease_ph", True)
    if factor == "ec_ms_cm" and direction == "below":
        return "increase_ec", True
    if factor == "ec_ms_cm" and direction == "above":
        return "manual_ec_high_intervention", False
    if factor in {"water_temp_c", "air_temp_c", "humidity_pct"}:
        return "manual_environment_review", False
    if factor == "ppfd_umol_m2_s":
        return ("increase_light" if direction == "below" else "decrease_light", True)
    return "review_required", False


def _structured_context(*, factor: str, direction: str, observed_value: float,
                        target_min: float, target_max: float, unit: str | None,
                        reading: TelemetryReading, provenance: str | None,
                        source_reference_id: str | None) -> dict[str, object]:
    return {
        "factor": factor, "direction": direction, "observed_value_at_creation": observed_value,
        "target_min": target_min, "target_max": target_max, "unit": unit,
        "telemetry_reading_id": reading.id, "recorded_at": reading.recorded_at.isoformat(),
        "target_provenance": provenance, "target_source_reference_id": source_reference_id,
    }


def _create_recommendation(session: Session, alert: Alert, reading: TelemetryReading,
                           *, factor: str, direction: str, observed_value: float,
                           target_min: float, target_max: float, unit: str | None,
                           provenance: str | None, source_reference_id: str | None) -> Recommendation:
    action_type, executable = _recommendation_action(factor, direction)
    label = SUPPORTED_FACTORS[factor]
    range_text = f"{target_min:g}–{target_max:g}" + (f" {unit}" if unit else "")
    observed_text = f"{observed_value:g}" + (f" {unit}" if unit else "")
    if action_type == "manual_ec_high_intervention":
        action_text = "Manual dilution / solution-maintenance intervention is required; no automatic EC-high actuator exists in the MVP."
    elif action_type == "manual_environment_review":
        action_text = "Manual review is required; this factor has no automatic actuator in the MVP."
    elif executable:
        action_text = "A supervised simulated correction step is available after explicit approval and separate execution."
    else:
        action_text = "Review the condition and planting context."
    recommendation = Recommendation(
        alert_id=alert.id, tower_id=reading.tower_id, planting_id=alert.planting_id,
        status="pending",
        reason=(f"{label} is outside the configured target range ({direction}: observed {observed_text}; "
                f"configured {range_text}). {action_text}"),
        action_type=action_type,
        action_parameters={
            **_structured_context(factor=factor, direction=direction, observed_value=observed_value,
                                  target_min=target_min, target_max=target_max, unit=unit,
                                  reading=reading, provenance=provenance,
                                  source_reference_id=source_reference_id),
            "executable": executable,
        },
    )
    session.add(recommendation)
    return recommendation


def _latest_executed_recommendation(session: Session, alert_id: int) -> Recommendation | None:
    return session.scalar(select(Recommendation).where(
        Recommendation.alert_id == alert_id, Recommendation.status == "executed"
    ).order_by(Recommendation.executed_at.desc(), Recommendation.id.desc()).limit(1))


def _outcome_already_final(session: Session, recommendation_id: int) -> bool:
    return session.scalar(select(ActionLog.id).where(
        ActionLog.recommendation_id == recommendation_id,
        ActionLog.event_type.in_(["physical_outcome_recovered", "physical_outcome_follow_up_required"]),
    ).limit(1)) is not None


def _post_execution_count(session: Session, recommendation: Recommendation, reading: TelemetryReading) -> int:
    if recommendation.executed_at is None:
        return 0
    rows = session.scalars(select(TelemetryReading.id).where(
        TelemetryReading.tower_id == recommendation.tower_id,
        TelemetryReading.recorded_at > recommendation.executed_at,
        TelemetryReading.recorded_at <= reading.recorded_at,
    ))
    return len(list(rows))


def _log_outcome(session: Session, recommendation: Recommendation, event_type: str,
                 status: str, reading: TelemetryReading, **details: object) -> None:
    session.add(ActionLog(
        tower_id=recommendation.tower_id, recommendation_id=recommendation.id,
        event_type=event_type, actuator_type=None, status=status,
        details={"telemetry_reading_id": reading.id, "recorded_at": reading.recorded_at.isoformat(), **details},
        occurred_at=reading.recorded_at,
    ))


def _evaluate_executed_outcome(session: Session, alert: Alert, reading: TelemetryReading,
                               evaluation: FactorEvaluation) -> None:
    recommendation = _latest_executed_recommendation(session, alert.id)
    if recommendation is None or recommendation.executed_at is None or _utc(reading.recorded_at) <= _utc(recommendation.executed_at):
        return
    if _outcome_already_final(session, recommendation.id):
        return
    params = recommendation.action_parameters if isinstance(recommendation.action_parameters, dict) else {}
    original = _number(params.get("observed_value_at_creation"))
    direction = params.get("direction")
    current = evaluation.observed_value
    if current is None or original is None or direction not in {"below", "above"}:
        return

    if evaluation.status == "within":
        _log_outcome(session, recommendation, "physical_outcome_recovered", "recovered", reading,
                     observed_value=current, outcome="recovered")
        return

    improving = (direction == "below" and current > original) or (direction == "above" and current < original)
    wrong_direction = (direction == "below" and current < original) or (direction == "above" and current > original)
    count = _post_execution_count(session, recommendation, reading)
    elapsed_seconds = (_utc(reading.recorded_at) - _utc(recommendation.executed_at)).total_seconds()
    if elapsed_seconds < OUTCOME_OBSERVATION_SECONDS:
        _log_outcome(session, recommendation, "physical_outcome_observed", "improving" if improving else "observed", reading,
                     observed_value=current, outcome="improving" if improving else ("wrong_direction" if wrong_direction else "unchanged"),
                     observation_count=count, elapsed_seconds=elapsed_seconds)
        return

    outcome = "improving" if improving else ("wrong_direction" if wrong_direction else "no_verified_effect")
    _log_outcome(session, recommendation, "physical_outcome_follow_up_required", "follow_up_required", reading,
                 observed_value=current, outcome=outcome, observation_count=count, elapsed_seconds=elapsed_seconds)
    existing_pending = session.scalar(select(Recommendation.id).where(
        Recommendation.alert_id == alert.id, Recommendation.status.in_(["pending", "approved"])
    ).limit(1))
    if existing_pending is None:
        _create_recommendation(session, alert, reading, factor=alert.factor, direction=str(direction),
                               observed_value=current, target_min=float(evaluation.target_min),
                               target_max=float(evaluation.target_max), unit=evaluation.unit,
                               provenance=evaluation.provenance,
                               source_reference_id=evaluation.source_reference_id)



def _recovery_condition_confirmed(
    session: Session,
    *,
    planting: Planting,
    reading: TelemetryReading,
    evaluation: FactorEvaluation,
    open_alerts: list[Alert],
) -> bool:
    """Confirm recovery for unexecuted conditions without changing crop targets.

    An executed correction is allowed to recover on the first newer in-range
    reading because that reading is also the physical verification signal. For
    pending/approved-but-unexecuted conditions, several consecutive in-range
    readings are required so boundary noise cannot repeatedly resolve/reopen the
    same condition.
    """
    if not open_alerts:
        return False
    if ALERT_RECOVERY_CONFIRMATION_READINGS <= 1:
        return True
    if any(
        alert.id is not None and _latest_executed_recommendation(session, alert.id) is not None
        for alert in open_alerts
    ):
        return True

    recent = list(
        session.scalars(
            select(TelemetryReading)
            .where(
                TelemetryReading.tower_id == reading.tower_id,
                TelemetryReading.recorded_at <= reading.recorded_at,
            )
            .order_by(TelemetryReading.recorded_at.desc(), TelemetryReading.id.desc())
            .limit(ALERT_RECOVERY_CONFIRMATION_READINGS)
        )
    )
    if len(recent) < ALERT_RECOVERY_CONFIRMATION_READINGS:
        return False
    return all(
        evaluate_snapshot_factor(planting, candidate, evaluation.factor).status == "within"
        for candidate in recent
    )


def _reopen_condition_confirmed(
    session: Session,
    *,
    planting: Planting,
    reading: TelemetryReading,
    evaluation: FactorEvaluation,
) -> bool:
    """Debounce reopening after a genuine recovery without changing crop targets."""
    if ALERT_REOPEN_CONFIRMATION_READINGS <= 1:
        return True
    latest_resolved = session.scalar(
        select(Alert)
        .where(
            Alert.tower_id == reading.tower_id,
            Alert.planting_id == planting.id,
            Alert.factor == evaluation.factor,
            Alert.status == "resolved",
        )
        .order_by(Alert.resolved_at.desc(), Alert.id.desc())
        .limit(1)
    )
    if latest_resolved is None or latest_resolved.resolved_at is None:
        return True
    details = latest_resolved.details if isinstance(latest_resolved.details, dict) else {}
    # A direction change is a different current condition, not noisy reopening
    # after recovery, so do not delay it.
    if details.get("resolved_without_recovery") is True:
        return True

    recent = list(
        session.scalars(
            select(TelemetryReading)
            .where(
                TelemetryReading.tower_id == reading.tower_id,
                TelemetryReading.recorded_at > latest_resolved.resolved_at,
                TelemetryReading.recorded_at <= reading.recorded_at,
            )
            .order_by(TelemetryReading.recorded_at.desc(), TelemetryReading.id.desc())
            .limit(ALERT_REOPEN_CONFIRMATION_READINGS)
        )
    )
    if len(recent) < ALERT_REOPEN_CONFIRMATION_READINGS:
        return False
    return all(
        evaluate_snapshot_factor(planting, candidate, evaluation.factor).status == evaluation.status
        for candidate in recent
    )

def _alert_details(*, planting: Planting, reading: TelemetryReading, direction: str,
                   target_min: float, target_max: float, unit: str | None,
                   source_reference_id: str | None, provenance: str | None) -> dict[str, object]:
    snapshot = planting.profile_snapshot if isinstance(planting.profile_snapshot, dict) else {}
    return {
        "direction": direction, "target_min": target_min, "target_max": target_max, "unit": unit,
        "telemetry_reading_id": reading.id, "recorded_at": reading.recorded_at.isoformat(),
        "profile_name": snapshot.get("name"), "target_source_reference_id": source_reference_id,
        "target_provenance": provenance,
    }


def evaluate_snapshot_factor(planting: Planting, reading: TelemetryReading, factor: str) -> FactorEvaluation:
    if factor not in SUPPORTED_FACTORS:
        return FactorEvaluation(factor=factor, status="not_evaluated")
    snapshot = planting.profile_snapshot if isinstance(planting.profile_snapshot, dict) else {}
    target_range = _target_range(snapshot.get("targets", {}), factor)
    if target_range is None:
        return FactorEvaluation(factor=factor, status="not_evaluated")
    target_min, target_max, unit, source_reference_id, provenance = target_range
    observed = getattr(reading, factor)
    if observed is None:
        return FactorEvaluation(factor=factor, status="not_monitored", target_min=target_min,
                                target_max=target_max, unit=unit, source_reference_id=source_reference_id,
                                provenance=provenance)
    status: EvaluationStatus = "below" if observed < target_min else "above" if observed > target_max else "within"
    return FactorEvaluation(factor=factor, status=status, observed_value=observed, target_min=target_min,
                            target_max=target_max, unit=unit, source_reference_id=source_reference_id,
                            provenance=provenance)


def evaluate_telemetry_reading(session: Session, reading: TelemetryReading) -> list[FactorEvaluation]:
    planting = _active_planting(session, reading.tower_id)
    if planting is None:
        return []

    evaluations = [
        evaluate_snapshot_factor(planting, reading, factor)
        for factor in SUPPORTED_FACTORS
    ]

    # Keep delayed/out-of-order telemetry for history and trends, but never let an
    # older reading rewrite the current Alert/Recommendation lifecycle.
    newer_reading_id = session.scalar(
        select(TelemetryReading.id).where(
            TelemetryReading.tower_id == reading.tower_id,
            TelemetryReading.id != reading.id,
            TelemetryReading.recorded_at > reading.recorded_at,
        ).limit(1)
    )
    if newer_reading_id is not None:
        return evaluations

    _resolve_orphaned_alerts(session, reading, planting)
    _invalidate_orphaned_recommendations(session, reading, planting)
    for evaluation in evaluations:
        factor = evaluation.factor
        if evaluation.status in {"not_monitored", "not_evaluated"}:
            continue
        open_alerts = _open_factor_alerts(session, tower_id=reading.tower_id, planting_id=planting.id, factor=factor)
        if evaluation.status == "within":
            for alert in open_alerts:
                _evaluate_executed_outcome(session, alert, reading, evaluation)
            if not _recovery_condition_confirmed(
                session,
                planting=planting,
                reading=reading,
                evaluation=evaluation,
                open_alerts=open_alerts,
            ):
                continue
            _invalidate_unexecuted_recommendations(
                session,
                open_alerts,
                reading,
                reason="The latest telemetry remained within the configured target range long enough to confirm recovery before execution.",
                recovered_before_execution=True,
            )
            _resolve_alerts(open_alerts, reading, recovered=True)
            continue
        if evaluation.observed_value is None or evaluation.target_min is None or evaluation.target_max is None:
            continue
        same = next((a for a in open_alerts if _direction(a) == evaluation.status), None)
        details = _alert_details(planting=planting, reading=reading, direction=evaluation.status,
                                 target_min=evaluation.target_min, target_max=evaluation.target_max,
                                 unit=evaluation.unit, source_reference_id=evaluation.source_reference_id,
                                 provenance=evaluation.provenance)
        if same is not None:
            same.observed_value = evaluation.observed_value
            same.details = details
            superseded = [a for a in open_alerts if a.id != same.id]
            _invalidate_unexecuted_recommendations(
                session,
                superseded,
                reading,
                reason="A newer reading confirmed the current direction, so older opposite-direction recommendations are no longer applicable.",
                recovered_before_execution=False,
            )
            _resolve_alerts(
                superseded,
                reading,
                recovered=False,
                reason="A newer reading is still outside target but in a different direction.",
            )
            _evaluate_executed_outcome(session, same, reading, evaluation)
            continue
        _invalidate_unexecuted_recommendations(
            session,
            open_alerts,
            reading,
            reason="The latest telemetry moved out of range in a different direction, so the previous recommendation is no longer applicable.",
            recovered_before_execution=False,
        )
        _resolve_alerts(
            open_alerts,
            reading,
            recovered=False,
            reason="The condition changed direction; this previous Alert is no longer the current condition.",
        )
        if not _reopen_condition_confirmed(
            session, planting=planting, reading=reading, evaluation=evaluation
        ):
            continue
        label = SUPPORTED_FACTORS[factor]
        alert = Alert(
            tower_id=reading.tower_id, planting_id=planting.id, factor=factor, status="open",
            message=f"{label} is {evaluation.status} the configured target range for the active planting.",
            observed_value=evaluation.observed_value, details=details, detected_at=reading.recorded_at,
        )
        session.add(alert)
        session.flush()
        _create_recommendation(session, alert, reading, factor=factor, direction=evaluation.status,
                               observed_value=evaluation.observed_value, target_min=evaluation.target_min,
                               target_max=evaluation.target_max, unit=evaluation.unit,
                               provenance=evaluation.provenance, source_reference_id=evaluation.source_reference_id)
    return evaluations
