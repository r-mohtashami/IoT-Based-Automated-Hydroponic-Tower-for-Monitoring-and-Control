from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.actuator_execution import execute_recommendation as execute_controlled_recommendation
from app.ai_context_tools import get_recommendation_status_tool
from app.models import Recommendation
from app.recommendation_decisions import (
    approve_recommendation as approve_controlled_recommendation,
    reject_recommendation as reject_controlled_recommendation,
)


def approve_recommendation_tool(session: Session, recommendation_id: int) -> dict[str, Any]:
    preflight = get_recommendation_status_tool(session, recommendation_id)
    recommendation = session.get(Recommendation, recommendation_id)
    if recommendation is None:
        return preflight
    if recommendation.status != "pending":
        return {
            "preflight": preflight,
            "error": "Only pending Recommendations can be approved.",
            "recommendation_status": recommendation.status,
        }
    result = approve_controlled_recommendation(session, recommendation)
    return {
        "preflight": preflight,
        "decision": {
            "recommendation_id": recommendation.id,
            "status": recommendation.status,
            "revalidation_passed": result.valid,
            "reason": result.reason,
            "revalidation": result.details,
            "approval_is_execution": False,
            "execution_created": False,
        },
    }


def reject_recommendation_tool(
    session: Session,
    recommendation_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    preflight = get_recommendation_status_tool(session, recommendation_id)
    recommendation = session.get(Recommendation, recommendation_id)
    if recommendation is None:
        return preflight
    if recommendation.status != "pending":
        return {
            "preflight": preflight,
            "error": "Only pending Recommendations can be rejected.",
            "recommendation_status": recommendation.status,
        }
    reject_controlled_recommendation(session, recommendation, reason=reason)
    return {
        "preflight": preflight,
        "decision": {
            "recommendation_id": recommendation.id,
            "status": recommendation.status,
            "decision_note": recommendation.decision_note,
            "execution_created": False,
        },
    }


def execute_recommendation_tool(session: Session, recommendation_id: int) -> dict[str, Any]:
    preflight = get_recommendation_status_tool(session, recommendation_id)
    try:
        result = execute_controlled_recommendation(session, recommendation_id)
    except (LookupError, ValueError) as exc:
        return {"preflight": preflight, "error": str(exc)}
    return {
        "preflight": preflight,
        "execution": {
            **result.__dict__,
            "simulated": True,
            "approval_is_execution": False,
            "verification_note": "command_sent and execution_verified are separate application evidence fields",
        },
    }
