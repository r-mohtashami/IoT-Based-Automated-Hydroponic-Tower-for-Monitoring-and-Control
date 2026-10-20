from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ActionLog, Planting, Recommendation, TelemetryReading
from app.rule_engine import evaluate_snapshot_factor

DEFAULT_MAX_TELEMETRY_AGE_SECONDS = 120
DEFAULT_MAX_FUTURE_CLOCK_SKEW_SECONDS = 5


@dataclass(frozen=True)
class RevalidationResult:
    valid: bool
    reason: str
    details: dict[str, object]


def max_telemetry_age_seconds() -> int:
    value = int(
        os.getenv(
            "RECOMMENDATION_MAX_TELEMETRY_AGE_SECONDS",
            str(DEFAULT_MAX_TELEMETRY_AGE_SECONDS),
        )
    )
    if value <= 0:
        raise ValueError("RECOMMENDATION_MAX_TELEMETRY_AGE_SECONDS must be positive.")
    return value


def max_future_clock_skew_seconds() -> int:
    value = int(
        os.getenv(
            "RECOMMENDATION_MAX_FUTURE_CLOCK_SKEW_SECONDS",
            str(DEFAULT_MAX_FUTURE_CLOCK_SKEW_SECONDS),
        )
    )
    if value < 0:
        raise ValueError("RECOMMENDATION_MAX_FUTURE_CLOCK_SKEW_SECONDS cannot be negative.")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _active_planting(session: Session, tower_id: int) -> Planting | None:
    return session.scalar(
        select(Planting).where(
            Planting.tower_id == tower_id,
            Planting.status == "active",
        )
    )


def _latest_telemetry(session: Session, tower_id: int) -> TelemetryReading | None:
    return session.scalar(
        select(TelemetryReading)
        .where(TelemetryReading.tower_id == tower_id)
        .order_by(TelemetryReading.recorded_at.desc(), TelemetryReading.id.desc())
        .limit(1)
    )


def _expected_direction(recommendation: Recommendation) -> str | None:
    details = recommendation.alert.details
    if not isinstance(details, dict):
        return None
    direction = details.get("direction")
    return direction if direction in {"below", "above"} else None


def revalidate_recommendation(
    session: Session,
    recommendation: Recommendation,
    *,
    now: datetime | None = None,
    required_status: str = "pending",
) -> RevalidationResult:
    """Check current state using the active planting snapshot and latest telemetry."""
    if recommendation.status != required_status:
        return RevalidationResult(
            False,
            f"Recommendation is no longer {required_status}.",
            {"required_status": required_status, "current_status": recommendation.status},
        )

    active_planting = _active_planting(session, recommendation.tower_id)
    if active_planting is None or active_planting.id != recommendation.planting_id:
        return RevalidationResult(
            False,
            "The expected active planting is no longer active.",
            {"expected_planting_id": recommendation.planting_id},
        )

    latest = _latest_telemetry(session, recommendation.tower_id)
    if latest is None:
        return RevalidationResult(False, "No telemetry is available for revalidation.", {})

    current_time = _as_utc(now or datetime.now(timezone.utc))
    recorded_at = _as_utc(latest.recorded_at)
    raw_age_seconds = (current_time - recorded_at).total_seconds()
    future_skew_seconds = max(0.0, -raw_age_seconds)
    age_seconds = max(0.0, raw_age_seconds)
    freshness_limit = max_telemetry_age_seconds()
    future_skew_limit = max_future_clock_skew_seconds()

    base_details: dict[str, object] = {
        "latest_telemetry_reading_id": latest.id,
        "latest_recorded_at": recorded_at.isoformat(),
        "telemetry_age_seconds": round(age_seconds, 3),
        "telemetry_future_skew_seconds": round(future_skew_seconds, 3),
        "max_telemetry_age_seconds": freshness_limit,
        "max_future_clock_skew_seconds": future_skew_limit,
        "factor": recommendation.alert.factor,
    }

    if future_skew_seconds > future_skew_limit:
        return RevalidationResult(
            False,
            "Latest telemetry timestamp is too far in the future for the application clock-skew policy.",
            base_details,
        )

    if age_seconds > freshness_limit:
        return RevalidationResult(
            False,
            "Latest telemetry is older than the application freshness limit.",
            base_details,
        )

    if recorded_at < _as_utc(recommendation.alert.detected_at):
        return RevalidationResult(
            False,
            "Latest telemetry predates the Alert being reviewed.",
            base_details,
        )

    factor = recommendation.alert.factor
    evaluation = evaluate_snapshot_factor(active_planting, latest, factor)
    base_details["current_evaluation"] = evaluation.status
    base_details["observed_value"] = evaluation.observed_value
    base_details["target_min"] = evaluation.target_min
    base_details["target_max"] = evaluation.target_max

    if evaluation.status == "not_monitored":
        return RevalidationResult(
            False,
            "Latest telemetry does not contain the factor required for revalidation.",
            base_details,
        )
    if evaluation.status == "not_evaluated":
        return RevalidationResult(
            False,
            "The active planting snapshot no longer provides an evaluable target for this factor.",
            base_details,
        )
    if evaluation.status == "within":
        return RevalidationResult(
            False,
            "The latest telemetry has returned within the configured target range.",
            base_details,
        )

    expected_direction = _expected_direction(recommendation)
    base_details["expected_direction"] = expected_direction
    if expected_direction is None or evaluation.status != expected_direction:
        return RevalidationResult(
            False,
            "The latest telemetry is out of range in a different direction than this Recommendation.",
            base_details,
        )

    if recommendation.alert.status != "open":
        return RevalidationResult(
            False,
            "The related Alert is no longer open.",
            base_details,
        )

    return RevalidationResult(
        True,
        "Revalidation passed against the active planting snapshot and latest telemetry.",
        base_details,
    )


def _log_decision(
    session: Session,
    recommendation: Recommendation,
    *,
    event_type: str,
    status: str,
    details: dict[str, object],
    occurred_at: datetime,
) -> None:
    session.add(
        ActionLog(
            tower_id=recommendation.tower_id,
            recommendation_id=recommendation.id,
            event_type=event_type,
            status=status,
            details=details,
            occurred_at=occurred_at,
        )
    )


def _guarded_transition(
    session: Session,
    recommendation_id: int,
    *,
    new_status: str,
    values: dict[str, object],
) -> None:
    result = session.execute(
        update(Recommendation)
        .where(
            Recommendation.id == recommendation_id,
            Recommendation.status == "pending",
        )
        .values(status=new_status, **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.rollback()
        raise ValueError("Only pending Recommendations can receive a decision.")


def approve_recommendation(
    session: Session,
    recommendation: Recommendation,
    *,
    now: datetime | None = None,
) -> RevalidationResult:
    decided_at = _as_utc(now or datetime.now(timezone.utc))
    result = revalidate_recommendation(session, recommendation, now=decided_at)
    new_status = "approved" if result.valid else "stale"

    _guarded_transition(
        session,
        recommendation.id,
        new_status=new_status,
        values={
            "reviewed_at": decided_at,
            "revalidated_at": decided_at,
            "decision_note": result.reason,
        },
    )

    if result.valid:
        event_type = "recommendation_approved"
        log_details = {"revalidation": "passed", **result.details, "execution_created": False}
    else:
        event_type = "recommendation_stale"
        log_details = {"revalidation": "failed", "reason": result.reason, **result.details}

    _log_decision(
        session,
        recommendation,
        event_type=event_type,
        status=new_status,
        details=log_details,
        occurred_at=decided_at,
    )

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(recommendation)
    return result


def reject_recommendation(
    session: Session,
    recommendation: Recommendation,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    decided_at = _as_utc(now or datetime.now(timezone.utc))
    clean_reason = reason.strip() if reason and reason.strip() else None

    _guarded_transition(
        session,
        recommendation.id,
        new_status="rejected",
        values={
            "reviewed_at": decided_at,
            "decision_note": clean_reason or "Rejected by user.",
        },
    )
    _log_decision(
        session,
        recommendation,
        event_type="recommendation_rejected",
        status="rejected",
        details={"decision": "rejected", "reason": clean_reason},
        occurred_at=decided_at,
    )

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(recommendation)
