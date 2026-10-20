from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


SENSOR_SPECS: dict[str, tuple[str, str]] = {
    "ph": ("pH", ""),
    "ec_ms_cm": ("EC", "mS/cm"),
    "water_temp_c": ("Water temperature", "°C"),
    "air_temp_c": ("Air temperature", "°C"),
    "humidity_pct": ("Humidity", "%"),
    "water_level_pct": ("Reservoir level", "%"),
    "ppfd_umol_m2_s": ("Light (PPFD)", "µmol/m²/s"),
    "circulation_on": ("Circulation", ""),
}

PROFILE_TYPE_LABELS = {
    "system_builtin": "Evidence-backed",
    "user_customized": "Customized plant",
    "user_custom": "Custom plant",
}

ACTION_EVENT_LABELS = {
    "recommendation_approved": "Recommendation approved",
    "recommendation_rejected": "Recommendation rejected",
    "execution_requested": "Execution requested",
    "actuator_command_sent": "Command sent",
    "execution_verified": "Actuator acknowledged",
    "awaiting_physical_outcome": "Awaiting physical outcome",
    "physical_outcome_observed": "Post-action telemetry observed",
    "physical_outcome_recovered": "Recovered",
    "physical_outcome_follow_up_required": "Follow-up required",
    "execution_failed": "Execution failed / unverified",
    "recommendation_stale": "Recommendation no longer needed",
}

DEFAULT_TELEMETRY_STALE_SECONDS = 120
DEFAULT_MAX_FUTURE_CLOCK_SKEW_SECONDS = 5


def sensor_text(value: Any, unit: str = "") -> str:
    if value is None:
        return "Not monitored"
    if isinstance(value, bool):
        return "On" if value else "Off"
    if isinstance(value, float):
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return f"{rendered} {unit}".strip()


def sensor_number_and_unit(value: Any, unit: str = "") -> tuple[str, str]:
    """Return number and unit separately so responsive cards never ellipsize the unit."""
    if value is None:
        return "—", ""
    if isinstance(value, bool):
        return ("On" if value else "Off"), ""
    if isinstance(value, float):
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return rendered, unit


def profile_type_label(profile_type: str) -> str:
    return PROFILE_TYPE_LABELS.get(profile_type, profile_type.replace("_", " ").title())


def profile_origin_label(profile: dict[str, Any], profiles: list[dict[str, Any]] | None = None) -> str:
    profile_type = profile.get("profile_type")
    if profile_type == "system_builtin":
        return "Evidence-backed"
    if profile_type == "user_custom":
        return "Custom · Settings set by you"
    if profile_type == "user_customized":
        source_id = profile.get("source_profile_id")
        source_name = None
        if profiles and source_id is not None:
            source = next((item for item in profiles if item.get("id") == source_id), None)
            if source:
                source_name = str(source.get("name") or "").split(" — ", 1)[0]
        return f"Based on {source_name} · Settings changed by you" if source_name else "Customized · Settings changed by you"
    return "Plant"


def factor_label(factor: str) -> str:
    return SENSOR_SPECS.get(factor, (factor.replace("_", " ").title(), ""))[0]


def target_for_factor(planting: dict[str, Any] | None, factor: str) -> dict[str, Any] | None:
    if not planting:
        return None
    targets = planting.get("profile_snapshot", {}).get("targets", {})
    value = targets.get(factor)
    return value if isinstance(value, dict) else None


def target_text(target: dict[str, Any] | None) -> str:
    if not target:
        return "No plant target"
    unit = target.get("unit", "")
    minimum = target.get("min")
    maximum = target.get("max")
    if minimum is not None and maximum is not None:
        return f"{minimum}–{maximum} {unit}".strip()
    if "value" in target:
        return sensor_text(target.get("value"), unit)
    return "Target saved for this planting"


def alert_target_text(alert: dict[str, Any]) -> str:
    details = alert.get("details") or {}
    minimum = details.get("target_min")
    maximum = details.get("target_max")
    unit = details.get("unit", "")
    if minimum is None or maximum is None:
        return "No plant target"
    return f"{minimum}–{maximum} {unit}".strip()


def chart_points(history: list[dict[str, Any]], factor: str) -> list[dict[str, Any]]:
    points = []
    for row in history:
        value = row.get(factor)
        if value is None:
            continue
        points.append({"timestamp": row["timestamp"], "value": value})
    return points


def execution_evidence(history: list[dict[str, Any]], recommendation_id: int) -> dict[str, Any]:
    events = [row for row in history if row.get("recommendation_id") == recommendation_id]
    command_sent = any(row.get("event_type") == "actuator_command_sent" for row in events)
    verified = any(row.get("event_type") == "execution_verified" for row in events)
    recovered = any(row.get("event_type") == "physical_outcome_recovered" for row in events)
    follow_up = any(row.get("event_type") == "physical_outcome_follow_up_required" for row in events)
    failed = any(row.get("event_type") == "execution_failed" for row in events)
    simulated = any(bool((row.get("details") or {}).get("simulated")) for row in events)

    if recovered:
        label = "Recovered — newer telemetry verified the physical outcome"
    elif follow_up:
        label = "Actuator acknowledged — follow-up is required after newer telemetry"
    elif verified:
        label = "Actuator acknowledged — awaiting newer telemetry"
    elif command_sent and failed:
        label = "Command sent — execution failed or remains unverified"
    elif command_sent:
        label = "Command sent — verification not confirmed"
    elif failed:
        label = "Execution failed / unverified"
    else:
        label = "No execution evidence"
    return {
        "command_sent": command_sent,
        "execution_verified": verified,
        "execution_failed": failed,
        "physical_recovery_verified": recovered,
        "follow_up_required": follow_up,
        "simulated": simulated,
        "label": label,
    }


def execution_feedback(evidence: dict[str, Any]) -> tuple[str, str]:
    if evidence.get("physical_recovery_verified"):
        return "success", evidence["label"]
    if evidence.get("follow_up_required") or evidence.get("execution_verified"):
        return "warning", evidence["label"]
    if evidence.get("command_sent"):
        return "warning", evidence["label"]
    if evidence.get("execution_failed"):
        return "error", evidence["label"]
    return "warning", "No verified execution evidence is recorded by the backend."


def action_event_label(event_type: str) -> str:
    return ACTION_EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())


def friendly_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return value


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def telemetry_age_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (reference.astimezone(timezone.utc) - parsed).total_seconds())


def telemetry_future_skew_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - reference.astimezone(timezone.utc)).total_seconds())


def telemetry_is_stale(
    value: str | None,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_TELEMETRY_STALE_SECONDS,
    max_future_clock_skew_seconds: float = DEFAULT_MAX_FUTURE_CLOCK_SKEW_SECONDS,
) -> bool:
    age = telemetry_age_seconds(value, now=now)
    future_skew = telemetry_future_skew_seconds(value, now=now)
    return (
        age is None
        or future_skew is None
        or age > stale_after_seconds
        or future_skew > max_future_clock_skew_seconds
    )


def chart_domain(
    values: list[float],
    target: dict[str, Any] | None = None,
    *,
    minimum_span: float = 0.4,
) -> tuple[float, float] | None:
    numeric = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if target:
        minimum = target.get("min")
        maximum = target.get("max")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            numeric.append(float(minimum))
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            numeric.append(float(maximum))
    if not numeric:
        return None
    low, high = min(numeric), max(numeric)
    span = high - low
    desired_span = max(span * 1.3, minimum_span)
    midpoint = (low + high) / 2
    lower = midpoint - desired_span / 2
    upper = midpoint + desired_span / 2
    if low >= 0 and lower < 0:
        upper -= lower
        lower = 0.0
    return lower, upper


def parse_target_range(minimum_text: str, maximum_text: str, *, unit: str) -> dict[str, Any]:
    try:
        minimum = float(minimum_text)
        maximum = float(maximum_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter both minimum and maximum as numbers.") from exc
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        raise ValueError("Target values must be finite numbers.")
    if minimum > maximum:
        raise ValueError("Minimum cannot be greater than maximum.")
    return {"min": minimum, "max": maximum, "unit": unit}


def recommendation_label(rec: dict[str, Any], alerts_by_id: dict[int, dict[str, Any]]) -> str:
    alert = alerts_by_id.get(rec.get("alert_id")) or {}
    factor = factor_label(alert.get("factor", "condition"))
    status = rec.get("status", "unknown").replace("_", " ").title()
    created = friendly_time(rec.get("created_at"))
    return f"{factor} — {status} — {created}"


def assistant_action_candidates(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only currently actionable Recommendation controls evidenced by assistant tools.

    The model never grants mutation authority. This helper only converts authoritative
    application tool results into UI controls; the actual click still sends a structured
    action_request that the backend scopes to one action and one Recommendation.
    """
    candidates: dict[int, dict[str, Any]] = {}

    def record(recommendation: Any) -> None:
        if not isinstance(recommendation, dict):
            return
        recommendation_id = recommendation.get("recommendation_id", recommendation.get("id"))
        if not isinstance(recommendation_id, int):
            return
        status = recommendation.get("status")
        parameters = recommendation.get("action_parameters")
        executable = True
        if isinstance(parameters, dict):
            executable = bool(parameters.get("executable", True))

        if status == "pending" and executable:
            candidates[recommendation_id] = {
                "recommendation_id": recommendation_id,
                "status": "pending",
                "actions": ["approve", "reject"],
            }
        elif status == "approved" and executable:
            candidates[recommendation_id] = {
                "recommendation_id": recommendation_id,
                "status": "approved",
                "actions": ["execute"],
            }
        elif status in {"rejected", "stale", "executed", "failed"}:
            candidates.pop(recommendation_id, None)

    for trace_item in tool_trace or []:
        if not isinstance(trace_item, dict):
            continue
        tool = trace_item.get("tool")
        result = trace_item.get("result")
        if not isinstance(result, dict):
            continue

        if tool == "get_recommendations":
            for recommendation in result.get("recommendations") or []:
                record(recommendation)
        elif tool in {"get_recommendation_status", "explain_recommendation"}:
            record(result.get("recommendation"))
        elif tool == "approve_recommendation":
            decision = result.get("decision")
            if isinstance(decision, dict) and decision.get("status") == "approved":
                recommendation_id = decision.get("recommendation_id")
                if isinstance(recommendation_id, int):
                    candidates[recommendation_id] = {
                        "recommendation_id": recommendation_id,
                        "status": "approved",
                        "actions": ["execute"],
                    }
        elif tool in {"reject_recommendation", "execute_recommendation"}:
            recommendation_id = None
            if tool == "execute_recommendation":
                execution = result.get("execution")
                if isinstance(execution, dict):
                    recommendation_id = execution.get("recommendation_id")
            else:
                decision = result.get("decision")
                if isinstance(decision, dict):
                    recommendation_id = decision.get("recommendation_id")
            if isinstance(recommendation_id, int):
                candidates.pop(recommendation_id, None)

    return list(candidates.values())
