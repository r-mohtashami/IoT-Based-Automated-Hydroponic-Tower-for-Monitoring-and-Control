from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionLog, ActuatorState, Alert, Planting, Recommendation, TelemetryReading, Tower
from app.rule_engine import evaluate_snapshot_factor


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


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


def _sensor(value: float | bool | None) -> dict[str, Any]:
    if value is None:
        return {"status": "not_monitored", "value": None}
    return {"status": "observed", "value": value}


def _snapshot(planting: Planting | None) -> dict[str, Any] | None:
    if planting is None:
        return None
    snapshot = planting.profile_snapshot if isinstance(planting.profile_snapshot, dict) else {}
    return {
        "planting_id": planting.id,
        "status": planting.status,
        "profile_name": snapshot.get("name"),
        "growth_stage": planting.growth_stage,
        "started_at": _iso(planting.started_at),
        "configured_targets": snapshot.get("targets", {}),
        "source_references": snapshot.get("source_references", []),
        "target_source": "planting_profile_snapshot",
    }


def _tower_display_name(tower: Tower) -> str:
    name = tower.name or "Tower"
    key = tower.tower_key or ""
    if key.startswith("sim-") and name.startswith("Demo "):
        return name[5:]
    return name


def list_towers_tool(session: Session) -> dict[str, Any]:
    towers = list(session.scalars(select(Tower).order_by(Tower.id)))
    return {
        "towers": [
            {
                "tower_id": tower.id,
                "tower_key": tower.tower_key,
                "name": _tower_display_name(tower),
            }
            for tower in towers
        ]
    }


def get_tower_status_tool(session: Session, tower_id: int) -> dict[str, Any]:
    tower = session.get(Tower, tower_id)
    if tower is None:
        return {"error": "Tower not found.", "tower_id": tower_id}
    planting = _active_planting(session, tower_id)
    latest = _latest_telemetry(session, tower_id)
    return {
        "tower": {
            "tower_id": tower.id,
            "tower_key": tower.tower_key,
            "name": _tower_display_name(tower),
            "description": tower.description,
        },
        "active_planting": _snapshot(planting),
        "latest_telemetry": _telemetry_payload(latest) if latest is not None else None,
        "open_alert_count": len(
            list(
                session.scalars(
                    select(Alert).where(Alert.tower_id == tower_id, Alert.status == "open")
                )
            )
        ),
        "simulated_actuator_states": [
            {
                "actuator_type": state.actuator_type,
                "state": state.state,
                "state_data": state.state_data,
                "observed_at": _iso(state.observed_at),
            }
            for state in session.scalars(
                select(ActuatorState).where(ActuatorState.tower_id == tower_id).order_by(ActuatorState.actuator_type)
            )
        ],
    }


def get_active_planting_tool(session: Session, tower_id: int) -> dict[str, Any]:
    tower = session.get(Tower, tower_id)
    if tower is None:
        return {"error": "Tower not found.", "tower_id": tower_id}
    planting = _active_planting(session, tower_id)
    if planting is None:
        return {"tower_id": tower_id, "active_planting": None}
    return {"tower_id": tower_id, "active_planting": _snapshot(planting)}


def _telemetry_payload(reading: TelemetryReading) -> dict[str, Any]:
    return {
        "reading_id": reading.id,
        "tower_id": reading.tower_id,
        "timestamp": _iso(reading.recorded_at),
        "provenance": "simulated" if reading.source == "simulator" else reading.source,
        "simulated": reading.source == "simulator",
        "sensors": {
            "ph": _sensor(reading.ph),
            "ec_ms_cm": _sensor(reading.ec_ms_cm),
            "water_temp_c": _sensor(reading.water_temp_c),
            "air_temp_c": _sensor(reading.air_temp_c),
            "humidity_pct": _sensor(reading.humidity_pct),
            "water_level_pct": _sensor(reading.water_level_pct),
            "ppfd_umol_m2_s": _sensor(reading.ppfd_umol_m2_s),
            "circulation_on": _sensor(reading.circulation_on),
        },
    }


def get_latest_telemetry_tool(session: Session, tower_id: int) -> dict[str, Any]:
    tower = session.get(Tower, tower_id)
    if tower is None:
        return {"error": "Tower not found.", "tower_id": tower_id}
    reading = _latest_telemetry(session, tower_id)
    if reading is None:
        return {"tower_id": tower_id, "latest_telemetry": None}

    planting = _active_planting(session, tower_id)
    return {
        "tower_id": tower_id,
        "latest_telemetry": _telemetry_payload(reading),
        "configured_target_context": _snapshot(planting),
        "note": "Missing sensor values are reported as not_monitored and are not fabricated.",
    }


def _alert_payload(alert: Alert) -> dict[str, Any]:
    return {
        "alert_id": alert.id,
        "tower_id": alert.tower_id,
        "planting_id": alert.planting_id,
        "factor": alert.factor,
        "status": alert.status,
        "message": alert.message,
        "observed_value": alert.observed_value,
        "details": alert.details,
        "detected_at": _iso(alert.detected_at),
        "resolved_at": _iso(alert.resolved_at),
        "semantics": "derived_from_observed_telemetry_and_configured_target",
    }


def get_open_alerts_tool(session: Session, tower_id: int) -> dict[str, Any]:
    if session.get(Tower, tower_id) is None:
        return {"error": "Tower not found.", "tower_id": tower_id}
    alerts = list(
        session.scalars(
            select(Alert)
            .where(Alert.tower_id == tower_id, Alert.status == "open")
            .order_by(Alert.detected_at.desc(), Alert.id.desc())
        )
    )
    return {"tower_id": tower_id, "open_alerts": [_alert_payload(alert) for alert in alerts]}


def _recommendation_payload(recommendation: Recommendation) -> dict[str, Any]:
    return {
        "recommendation_id": recommendation.id,
        "alert_id": recommendation.alert_id,
        "tower_id": recommendation.tower_id,
        "planting_id": recommendation.planting_id,
        "status": recommendation.status,
        "historical_reason": recommendation.reason,
        "action_type": recommendation.action_type,
        "action_parameters": recommendation.action_parameters,
        "created_at": _iso(recommendation.created_at),
        "reviewed_at": _iso(recommendation.reviewed_at),
        "revalidated_at": _iso(recommendation.revalidated_at),
        "executed_at": _iso(recommendation.executed_at),
        "decision_note": recommendation.decision_note,
        "historical_reason_is_current_observation": False,
    }


def get_recommendations_tool(session: Session, tower_id: int) -> dict[str, Any]:
    if session.get(Tower, tower_id) is None:
        return {"error": "Tower not found.", "tower_id": tower_id}
    recommendations = list(
        session.scalars(
            select(Recommendation)
            .where(Recommendation.tower_id == tower_id)
            .order_by(Recommendation.created_at.desc(), Recommendation.id.desc())
            .limit(100)
        )
    )
    return {
        "tower_id": tower_id,
        "recommendations": [_recommendation_payload(item) for item in recommendations],
    }


def _source_reference(planting: Planting | None, reference_id: str | None) -> dict[str, Any] | None:
    if planting is None or not reference_id:
        return None
    snapshot = planting.profile_snapshot if isinstance(planting.profile_snapshot, dict) else {}
    references = snapshot.get("source_references", [])
    if not isinstance(references, list):
        return None
    for reference in references:
        if isinstance(reference, dict) and reference.get("reference_id") == reference_id:
            return reference
    return None


def get_recommendation_status_tool(session: Session, recommendation_id: int) -> dict[str, Any]:
    recommendation = session.get(Recommendation, recommendation_id)
    if recommendation is None:
        return {"error": "Recommendation not found.", "recommendation_id": recommendation_id}

    active = _active_planting(session, recommendation.tower_id)
    latest = _latest_telemetry(session, recommendation.tower_id)
    factor = recommendation.alert.factor
    current_evaluation: dict[str, Any] | None = None
    if active is not None and latest is not None:
        evaluated = evaluate_snapshot_factor(active, latest, factor)
        current_evaluation = {
            "factor": factor,
            "status": evaluated.status,
            "observed_value": evaluated.observed_value,
            "target_min": evaluated.target_min,
            "target_max": evaluated.target_max,
            "unit": evaluated.unit,
            "source_reference_id": evaluated.source_reference_id,
            "latest_telemetry_reading_id": latest.id,
            "latest_recorded_at": _iso(latest.recorded_at),
        }

    return {
        "recommendation": _recommendation_payload(recommendation),
        "linked_alert": _alert_payload(recommendation.alert),
        "current_context": {
            "active_planting": _snapshot(active),
            "expected_planting_matches": active is not None and active.id == recommendation.planting_id,
            "latest_telemetry": _telemetry_payload(latest) if latest is not None else None,
            "current_factor_evaluation": current_evaluation,
        },
        "note": "Current context is queried separately; historical recommendation text is not treated as current observation.",
    }


def explain_alert_tool(session: Session, alert_id: int) -> dict[str, Any]:
    alert = session.get(Alert, alert_id)
    if alert is None:
        return {"error": "Alert not found.", "alert_id": alert_id}
    planting = session.get(Planting, alert.planting_id) if alert.planting_id is not None else None
    details = alert.details if isinstance(alert.details, dict) else {}
    reference = _source_reference(planting, details.get("target_source_reference_id"))
    return {
        "alert": _alert_payload(alert),
        "explanation": {
            "observed_value": alert.observed_value,
            "configured_target_min": details.get("target_min"),
            "configured_target_max": details.get("target_max"),
            "unit": details.get("unit"),
            "direction": details.get("direction"),
            "profile_name": details.get("profile_name"),
            "target_source_reference": reference,
            "claim_scope": "outside configured target range; not an emergency or safety-limit claim",
        },
    }


def explain_recommendation_tool(session: Session, recommendation_id: int) -> dict[str, Any]:
    context = get_recommendation_status_tool(session, recommendation_id)
    if "error" in context:
        return context
    return {
        **context,
        "explanation": {
            "proposal_type": "review_required",
            "authority": "application_generated",
            "contains_dosing_or_duration": False,
            "approval_is_execution": False,
            "command_sent_is_verified_execution": False,
            "all_actuator_execution_is_simulated": True,
        },
    }


def get_action_history_tool(session: Session, tower_id: int, limit: int = 50) -> dict[str, Any]:
    if session.get(Tower, tower_id) is None:
        return {"error": "Tower not found.", "tower_id": tower_id}
    bounded_limit = min(max(limit, 1), 200)
    rows = list(
        session.scalars(
            select(ActionLog)
            .where(ActionLog.tower_id == tower_id)
            .order_by(ActionLog.occurred_at.desc(), ActionLog.id.desc())
            .limit(bounded_limit)
        )
    )
    return {
        "tower_id": tower_id,
        "history": [
            {
                "event_id": row.id,
                "recommendation_id": row.recommendation_id,
                "event_type": row.event_type,
                "actuator_type": row.actuator_type,
                "status": row.status,
                "details": row.details,
                "occurred_at": _iso(row.occurred_at),
            }
            for row in rows
        ],
    }
