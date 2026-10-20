from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actuator_mqtt import ActuatorAck, MqttExchangeResult, publish_and_wait_for_ack
from app.models import ActionLog, ActuatorState, Recommendation
from app.recommendation_decisions import revalidate_recommendation


@dataclass(frozen=True)
class ActionDefinition:
    actuator_type: str
    command: str
    state: str


# Application-owned mapping. Numeric physical effects live only in the simulator and are
# SIMULATED / ASSUMED APPLICATION CONFIGURATION, never dosing guidance.
EXECUTABLE_ACTIONS = {
    "increase_ph": ActionDefinition("ph_up_pump", "apply_step", "completed"),
    "decrease_ph": ActionDefinition("ph_down_pump", "apply_step", "completed"),
    "increase_ec": ActionDefinition("nutrient_dosing_pump", "apply_step", "completed"),
    "refill_water": ActionDefinition("refill_inlet", "apply_step", "completed"),
    "increase_light": ActionDefinition("grow_light", "set_state", "on"),
    "decrease_light": ActionDefinition("grow_light", "set_state", "off"),
    "enable_circulation": ActionDefinition("circulation_pump", "set_state", "on"),
    "disable_circulation": ActionDefinition("circulation_pump", "set_state", "off"),
}


@dataclass(frozen=True)
class ExecutionClaim:
    allowed: bool
    recommendation_id: int
    command_id: str | None
    tower_key: str | None
    action: ActionDefinition | None
    reason: str
    revalidation: dict[str, object]
    recommendation_status: str


@dataclass(frozen=True)
class ExecutionResult:
    recommendation_id: int
    recommendation_status: str
    execution_status: str
    command_id: str | None
    actuator_type: str | None
    desired_state: str | None
    command_sent: bool
    execution_verified: bool
    reason: str
    revalidation: dict[str, object]


def _log(
    session: Session,
    recommendation: Recommendation,
    *,
    event_type: str,
    status: str,
    actuator_type: str | None,
    details: dict[str, object],
    occurred_at: datetime,
) -> None:
    session.add(
        ActionLog(
            tower_id=recommendation.tower_id,
            recommendation_id=recommendation.id,
            event_type=event_type,
            actuator_type=actuator_type,
            status=status,
            details=details,
            occurred_at=occurred_at,
        )
    )


def claim_execution(
    session: Session,
    recommendation_id: int,
    *,
    now: datetime | None = None,
) -> ExecutionClaim:
    recommendation = session.scalar(
        select(Recommendation)
        .where(Recommendation.id == recommendation_id)
        .with_for_update()
    )
    if recommendation is None:
        session.rollback()
        raise LookupError("Recommendation not found.")
    if recommendation.status != "approved":
        session.rollback()
        raise ValueError("Only approved Recommendations can be executed.")

    already_requested = session.scalar(
        select(ActionLog.id)
        .where(
            ActionLog.recommendation_id == recommendation.id,
            ActionLog.event_type == "execution_requested",
        )
        .limit(1)
    )
    if already_requested is not None:
        session.rollback()
        raise ValueError("Execution has already been requested for this Recommendation.")

    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    revalidation = revalidate_recommendation(
        session,
        recommendation,
        now=checked_at,
        required_status="approved",
    )
    recommendation.revalidated_at = checked_at

    if not revalidation.valid:
        recommendation.status = "stale"
        recommendation.decision_note = f"Execution blocked: {revalidation.reason}"
        _log(
            session,
            recommendation,
            event_type="execution_blocked",
            status="blocked",
            actuator_type=None,
            details={"revalidation": "failed", "reason": revalidation.reason, **revalidation.details},
            occurred_at=checked_at,
        )
        session.commit()
        return ExecutionClaim(
            False,
            recommendation.id,
            None,
            None,
            None,
            revalidation.reason,
            revalidation.details,
            recommendation.status,
        )

    action = EXECUTABLE_ACTIONS.get(recommendation.action_type or "")
    tower_key = recommendation.tower.tower_key
    executable = bool((recommendation.action_parameters or {}).get("executable", action is not None))
    if action is None and not executable:
        reason = "This recommendation requires manual review/action; no automatic simulated actuator is defined."
        recommendation.decision_note = reason
        _log(
            session,
            recommendation,
            event_type="execution_blocked",
            status="blocked",
            actuator_type=None,
            details={
                "revalidation": "passed",
                "reason": reason,
                "manual_or_non_executable": True,
                **revalidation.details,
            },
            occurred_at=checked_at,
        )
        session.commit()
        return ExecutionClaim(
            False, recommendation.id, None, tower_key, None, reason, revalidation.details, recommendation.status
        )
    if action is None or not tower_key:
        reason = "Recommendation has no application-defined executable simulated action or tower mapping."
        recommendation.status = "failed"
        recommendation.decision_note = reason
        _log(
            session,
            recommendation,
            event_type="execution_blocked",
            status="blocked",
            actuator_type=None,
            details={"revalidation": "passed", "reason": reason, **revalidation.details},
            occurred_at=checked_at,
        )
        session.commit()
        return ExecutionClaim(
            False, recommendation.id, None, tower_key, None, reason, revalidation.details, recommendation.status
        )

    command_id = str(uuid4())
    _log(
        session,
        recommendation,
        event_type="execution_requested",
        status="requested",
        actuator_type=action.actuator_type,
        details={
            "command_id": command_id,
            "tower_key": tower_key,
            "command": action.command,
            "desired_state": action.state,
            "revalidation": "passed",
            **revalidation.details,
        },
        occurred_at=checked_at,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError(
            "Execution has already been requested for this Recommendation."
        ) from exc
    return ExecutionClaim(
        True,
        recommendation.id,
        command_id,
        tower_key,
        action,
        revalidation.reason,
        revalidation.details,
        recommendation.status,
    )


def _record_sent(session: Session, claim: ExecutionClaim, sent_at: datetime) -> None:
    recommendation = session.get(Recommendation, claim.recommendation_id)
    assert recommendation is not None and claim.action is not None
    _log(
        session,
        recommendation,
        event_type="actuator_command_sent",
        status="sent",
        actuator_type=claim.action.actuator_type,
        details={
            "command_id": claim.command_id,
            "tower_key": claim.tower_key,
            "command": claim.action.command,
            "desired_state": claim.action.state,
            "simulated": True,
        },
        occurred_at=sent_at,
    )
    session.commit()


def _record_failed(session: Session, claim: ExecutionClaim, reason: str, command_sent: bool) -> None:
    recommendation = session.get(Recommendation, claim.recommendation_id)
    assert recommendation is not None and claim.action is not None
    failed_at = datetime.now(timezone.utc)
    recommendation.status = "failed"
    recommendation.decision_note = f"Execution failed: {reason}"
    _log(
        session,
        recommendation,
        event_type="execution_failed",
        status="failed",
        actuator_type=claim.action.actuator_type,
        details={
            "command_id": claim.command_id,
            "command_sent": command_sent,
            "execution_verified": False,
            "reason": reason,
        },
        occurred_at=failed_at,
    )
    session.commit()


def _record_verified(session: Session, claim: ExecutionClaim, ack: ActuatorAck) -> None:
    recommendation = session.get(Recommendation, claim.recommendation_id)
    assert recommendation is not None and claim.action is not None

    state = session.scalar(
        select(ActuatorState).where(
            ActuatorState.tower_id == recommendation.tower_id,
            ActuatorState.actuator_type == claim.action.actuator_type,
        )
    )
    state_data = {
        "simulated": True,
        "command_id": ack.command_id,
        "recommendation_id": recommendation.id,
        "verification_source": "simulator_ack",
    }
    if state is None:
        state = ActuatorState(
            tower_id=recommendation.tower_id,
            actuator_type=claim.action.actuator_type,
            state=ack.state,
            state_data=state_data,
            observed_at=ack.timestamp,
        )
        session.add(state)
    else:
        state.state = ack.state
        state.state_data = state_data
        state.observed_at = ack.timestamp

    recommendation.status = "executed"
    recommendation.executed_at = ack.timestamp
    recommendation.decision_note = (
        "Simulated actuator command acknowledged. Physical outcome is awaiting newer telemetry."
    )
    _log(
        session,
        recommendation,
        event_type="execution_verified",
        status="verified",
        actuator_type=claim.action.actuator_type,
        details={
            "command_id": ack.command_id,
            "tower_key": ack.tower_key,
            "state": ack.state,
            "simulated": True,
            "verification_source": "simulator_ack",
            "execution_verified": True,
            "physical_recovery_verified": False,
        },
        occurred_at=ack.timestamp,
    )
    _log(
        session, recommendation,
        event_type="awaiting_physical_outcome", status="awaiting_telemetry",
        actuator_type=claim.action.actuator_type,
        details={"command_id": ack.command_id, "requires_newer_telemetry": True},
        occurred_at=ack.timestamp,
    )
    session.commit()


def execute_recommendation(
    session: Session,
    recommendation_id: int,
    *,
    now: datetime | None = None,
    transport: Callable[[ExecutionClaim], MqttExchangeResult] | None = None,
) -> ExecutionResult:
    claim = claim_execution(session, recommendation_id, now=now)
    if not claim.allowed:
        return ExecutionResult(
            recommendation_id,
            claim.recommendation_status,
            "blocked",
            None,
            None,
            None,
            False,
            False,
            claim.reason,
            claim.revalidation,
        )

    exchange = (transport or publish_and_wait_for_ack)(claim)
    assert claim.action is not None
    if exchange.command_sent and exchange.sent_at is not None:
        _record_sent(session, claim, exchange.sent_at)

    if not exchange.command_sent or exchange.ack is None:
        reason = exchange.error or "Command was sent but execution was not verified."
        _record_failed(session, claim, reason, exchange.command_sent)
        return ExecutionResult(
            recommendation_id,
            "failed",
            "failed",
            claim.command_id,
            claim.action.actuator_type,
            claim.action.state,
            exchange.command_sent,
            False,
            reason,
            claim.revalidation,
        )

    _record_verified(session, claim, exchange.ack)
    return ExecutionResult(
        recommendation_id,
        "executed",
        "verified",
        claim.command_id,
        claim.action.actuator_type,
        claim.action.state,
        True,
        True,
        "Simulated actuator command acknowledged; physical recovery requires newer telemetry.",
        claim.revalidation,
    )
