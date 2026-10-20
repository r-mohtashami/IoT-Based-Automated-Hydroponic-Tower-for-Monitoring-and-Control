from copy import deepcopy
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actuator_execution import execute_recommendation
from app.ai_assistant import AssistantUnavailableError, TurnAuthorization, assistant_available, assistant_model, run_assistant
from app.db import get_session
from app.models import ActionLog, ActuatorState, Alert, Planting, PlantProfile, Recommendation, TelemetryReading, Tower
from app.recommendation_decisions import approve_recommendation, reject_recommendation
from app.profile_validation import customized_copy_targets, merge_user_target_update, normalize_targets
from app.schemas import (
    ActionLogResponse,
    ActuatorStateResponse,
    AlertResponse,
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantStatusResponse,
    PlantingEnd,
    PlantingResponse,
    PlantingStart,
    PlantProfileCopyCreate,
    PlantProfileCreate,
    PlantProfileResponse,
    PlantProfileUpdate,
    RecommendationExecutionResponse,
    RecommendationReject,
    RecommendationResponse,
    TelemetryResponse,
    TowerCreate,
    TowerResponse,
    TowerUpdate,
)

router = APIRouter()


def get_tower_or_404(session: Session, tower_id: int) -> Tower:
    tower = session.get(Tower, tower_id)
    if tower is None:
        raise HTTPException(status_code=404, detail="Tower not found.")
    return tower


def get_profile_or_404(session: Session, profile_id: int) -> PlantProfile:
    profile = session.get(PlantProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Plant profile not found.")
    return profile




def get_recommendation_or_404(session: Session, recommendation_id: int) -> Recommendation:
    recommendation = session.get(Recommendation, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Recommendation not found.")
    return recommendation


def get_active_planting(session: Session, tower_id: int) -> Planting | None:
    return session.scalar(
        select(Planting).where(
            Planting.tower_id == tower_id,
            Planting.status == "active",
        )
    )



def telemetry_response(reading: TelemetryReading) -> TelemetryResponse:
    return TelemetryResponse(
        timestamp=reading.recorded_at,
        simulated=reading.source == "simulator",
        ph=reading.ph,
        ec_ms_cm=reading.ec_ms_cm,
        water_temp_c=reading.water_temp_c,
        air_temp_c=reading.air_temp_c,
        humidity_pct=reading.humidity_pct,
        water_level_pct=reading.water_level_pct,
        ppfd_umol_m2_s=reading.ppfd_umol_m2_s,
        circulation_on=reading.circulation_on,
    )


def planting_response(planting: Planting) -> PlantingResponse:
    snapshot = deepcopy(planting.profile_snapshot)
    snapshot.pop("profile_id", None)
    return PlantingResponse(
        planting_id=planting.id,
        plant_profile_id=planting.plant_profile_id,
        profile_name=snapshot.get("name", ""),
        status=planting.status,
        started_at=planting.started_at,
        ended_at=planting.ended_at,
        growth_stage=planting.growth_stage,
        profile_snapshot=snapshot,
    )


def _close_inactive_planting_state(
    session: Session,
    *,
    tower_id: int,
    planting_id: int,
    occurred_at: datetime,
    reason: str,
) -> None:
    """Close operational state that must not survive the planting that created it."""
    alerts = list(
        session.scalars(
            select(Alert).where(
                Alert.tower_id == tower_id,
                Alert.planting_id == planting_id,
                Alert.status == "open",
            )
        )
    )
    for alert in alerts:
        details = dict(alert.details) if isinstance(alert.details, dict) else {}
        details.update({"resolution_reason": reason, "resolved_without_recovery": True})
        alert.details = details
        alert.status = "resolved"
        alert.resolved_at = occurred_at

    recommendations = list(
        session.scalars(
            select(Recommendation).where(
                Recommendation.tower_id == tower_id,
                Recommendation.planting_id == planting_id,
                Recommendation.status.in_(["pending", "approved"]),
            )
        )
    )
    for recommendation in recommendations:
        recommendation.status = "stale"
        recommendation.revalidated_at = occurred_at
        recommendation.decision_note = reason
        session.add(
            ActionLog(
                tower_id=tower_id,
                recommendation_id=recommendation.id,
                event_type="recommendation_stale",
                status="stale",
                details={"reason": reason, "planting_ended": True},
                occurred_at=occurred_at,
            )
        )


@router.post("/towers", response_model=TowerResponse, status_code=status.HTTP_201_CREATED)
def create_tower(payload: TowerCreate, session: Session = Depends(get_session)) -> Tower:
    tower = Tower(
        name=payload.name,
        tower_key=payload.tower_key,
        description=payload.description,
    )
    session.add(tower)
    try:
        # UI-created Towers may omit an internal device key. Derive a stable key from
        # the persisted ID so removing/re-adding a Tower never silently reuses an old
        # virtual-device identity. Explicit keys remain supported for deterministic tests.
        session.flush()
        if tower.tower_key is None:
            tower.tower_key = f"sim-{tower.id}"
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Tower key is already in use.",
        ) from exc
    session.refresh(tower)
    return tower


@router.get("/towers", response_model=list[TowerResponse])
def list_towers(session: Session = Depends(get_session)) -> list[Tower]:
    return list(session.scalars(select(Tower).order_by(Tower.id)))


@router.get("/towers/{tower_id}", response_model=TowerResponse)
def get_tower(tower_id: int, session: Session = Depends(get_session)) -> Tower:
    return get_tower_or_404(session, tower_id)


@router.patch("/towers/{tower_id}", response_model=TowerResponse)
def update_tower(
    tower_id: int,
    payload: TowerUpdate,
    session: Session = Depends(get_session),
) -> Tower:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No tower fields were provided.")
    if "name" in updates and updates["name"] is None:
        raise HTTPException(status_code=422, detail="Tower name cannot be null.")

    tower = get_tower_or_404(session, tower_id)
    for field, value in updates.items():
        setattr(tower, field, value)

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Tower key is already in use.",
        ) from exc
    session.refresh(tower)
    return tower


@router.delete("/towers/{tower_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tower(
    tower_id: int,
    session: Session = Depends(get_session),
) -> None:
    """Permanently remove one Tower and its local operational history.

    This is intentionally explicit rather than a cascading ORM relationship so the
    deletion order stays understandable and portable across SQLite/PostgreSQL.
    Plant profiles are reusable library entries and are therefore preserved.
    """
    get_tower_or_404(session, tower_id)

    # Child rows reference Recommendations/Alerts/Plantings, so delete from the
    # deepest operational history outward before deleting the Tower itself.
    session.execute(delete(ActionLog).where(ActionLog.tower_id == tower_id))
    session.execute(delete(ActuatorState).where(ActuatorState.tower_id == tower_id))
    session.execute(delete(Recommendation).where(Recommendation.tower_id == tower_id))
    session.execute(delete(Alert).where(Alert.tower_id == tower_id))
    session.execute(delete(TelemetryReading).where(TelemetryReading.tower_id == tower_id))
    session.execute(delete(Planting).where(Planting.tower_id == tower_id))
    session.execute(delete(Tower).where(Tower.id == tower_id))
    session.commit()


@router.get(
    "/towers/{tower_id}/telemetry/latest",
    response_model=TelemetryResponse,
)
def latest_telemetry(
    tower_id: int,
    session: Session = Depends(get_session),
) -> TelemetryResponse:
    get_tower_or_404(session, tower_id)
    reading = session.scalar(
        select(TelemetryReading)
        .where(TelemetryReading.tower_id == tower_id)
        .order_by(TelemetryReading.recorded_at.desc(), TelemetryReading.id.desc())
        .limit(1)
    )
    if reading is None:
        raise HTTPException(status_code=404, detail="Tower has no telemetry readings.")
    return telemetry_response(reading)


@router.get(
    "/towers/{tower_id}/telemetry/history",
    response_model=list[TelemetryResponse],
)
def telemetry_history(
    tower_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[TelemetryResponse]:
    get_tower_or_404(session, tower_id)
    newest_first = list(
        session.scalars(
            select(TelemetryReading)
            .where(TelemetryReading.tower_id == tower_id)
            .order_by(TelemetryReading.recorded_at.desc(), TelemetryReading.id.desc())
            .limit(limit)
        )
    )
    newest_first.reverse()
    return [telemetry_response(reading) for reading in newest_first]


@router.get("/plant-profiles", response_model=list[PlantProfileResponse])
def list_plant_profiles(session: Session = Depends(get_session)) -> list[PlantProfile]:
    return list(session.scalars(select(PlantProfile).order_by(PlantProfile.id)))


@router.get("/plant-profiles/{profile_id}", response_model=PlantProfileResponse)
def get_plant_profile(
    profile_id: int,
    session: Session = Depends(get_session),
) -> PlantProfile:
    return get_profile_or_404(session, profile_id)


@router.post(
    "/plant-profiles/custom",
    response_model=PlantProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_custom_profile(
    payload: PlantProfileCreate,
    session: Session = Depends(get_session),
) -> PlantProfile:
    try:
        targets = normalize_targets(payload.targets)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    profile = PlantProfile(
        name=payload.name,
        profile_type="user_custom",
        targets=targets,
        source_references=deepcopy(payload.source_references),
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


@router.post(
    "/plant-profiles/{profile_id}/customized-copy",
    response_model=PlantProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customized_profile_copy(
    profile_id: int,
    payload: PlantProfileCopyCreate,
    session: Session = Depends(get_session),
) -> PlantProfile:
    source = get_profile_or_404(session, profile_id)
    profile = PlantProfile(
        name=payload.name,
        profile_type="user_customized",
        source_profile_id=source.id,
        targets=customized_copy_targets(source.targets),
        source_references=deepcopy(source.source_references),
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


@router.patch("/plant-profiles/{profile_id}", response_model=PlantProfileResponse)
def update_plant_profile(
    profile_id: int,
    payload: PlantProfileUpdate,
    session: Session = Depends(get_session),
) -> PlantProfile:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No profile fields were provided.")
    if any(value is None for value in updates.values()):
        raise HTTPException(status_code=422, detail="Profile fields cannot be null.")

    profile = get_profile_or_404(session, profile_id)
    if profile.is_builtin:
        raise HTTPException(
            status_code=403,
            detail="Built-in plant profiles cannot be updated directly. Create a customized copy instead.",
        )

    for field, value in updates.items():
        if field == "targets":
            try:
                value = merge_user_target_update(profile.targets, value)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        setattr(profile, field, deepcopy(value))

    session.commit()
    session.refresh(profile)
    return profile


@router.post(
    "/towers/{tower_id}/planting",
    response_model=PlantingResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_planting(
    tower_id: int,
    payload: PlantingStart,
    session: Session = Depends(get_session),
) -> PlantingResponse:
    get_tower_or_404(session, tower_id)
    profile = get_profile_or_404(session, payload.plant_profile_id)

    if get_active_planting(session, tower_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="Tower already has an active planting.",
        )

    # Defensive reconciliation for data created before planting-end cleanup existed.
    # Historical alerts remain queryable, but they must not stay operationally open.
    reconcile_at = datetime.now(timezone.utc)
    inactive_planting_ids = list(
        session.scalars(
            select(Planting.id).where(
                Planting.tower_id == tower_id,
                Planting.status != "active",
            )
        )
    )
    for inactive_id in inactive_planting_ids:
        _close_inactive_planting_state(
            session,
            tower_id=tower_id,
            planting_id=inactive_id,
            occurred_at=reconcile_at,
            reason="A newer planting is starting, so operational state from the previous planting is no longer applicable.",
        )

    planting = Planting.from_profile(
        tower_id=tower_id,
        plant_profile=profile,
        growth_stage=payload.growth_stage,
    )
    session.add(planting)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Tower already has an active planting.",
        ) from exc

    session.refresh(planting)
    return planting_response(planting)


@router.get("/towers/{tower_id}/planting", response_model=PlantingResponse)
def read_active_planting(
    tower_id: int,
    session: Session = Depends(get_session),
) -> PlantingResponse:
    get_tower_or_404(session, tower_id)
    planting = get_active_planting(session, tower_id)
    if planting is None:
        raise HTTPException(status_code=404, detail="Tower has no active planting.")
    return planting_response(planting)


@router.post("/towers/{tower_id}/planting/end", response_model=PlantingResponse)
def end_active_planting(
    tower_id: int,
    payload: PlantingEnd,
    session: Session = Depends(get_session),
) -> PlantingResponse:
    get_tower_or_404(session, tower_id)
    planting = get_active_planting(session, tower_id)
    if planting is None:
        raise HTTPException(status_code=404, detail="Tower has no active planting.")

    ended_at = datetime.now(timezone.utc)
    planting.status = payload.status
    planting.ended_at = ended_at
    _close_inactive_planting_state(
        session,
        tower_id=tower_id,
        planting_id=planting.id,
        occurred_at=ended_at,
        reason="The planting ended before this recommendation was applied.",
    )
    session.commit()
    session.refresh(planting)
    return planting_response(planting)

@router.get(
    "/towers/{tower_id}/alerts",
    response_model=list[AlertResponse],
)
def list_tower_alerts(
    tower_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[Alert]:
    get_tower_or_404(session, tower_id)
    query = select(Alert).where(Alert.tower_id == tower_id)
    if status_filter is not None:
        if status_filter not in {"open", "resolved"}:
            raise HTTPException(status_code=422, detail="Alert status must be open or resolved.")
        query = query.where(Alert.status == status_filter)
    return list(
        session.scalars(
            query.order_by(Alert.detected_at.desc(), Alert.id.desc()).limit(limit)
        )
    )


@router.get(
    "/towers/{tower_id}/recommendations",
    response_model=list[RecommendationResponse],
)
def list_tower_recommendations(
    tower_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[Recommendation]:
    get_tower_or_404(session, tower_id)
    query = select(Recommendation).where(Recommendation.tower_id == tower_id)
    if status_filter is not None:
        allowed = {"pending", "approved", "rejected", "stale", "executed", "failed"}
        if status_filter not in allowed:
            raise HTTPException(status_code=422, detail="Unsupported recommendation status.")
        query = query.where(Recommendation.status == status_filter)
    return list(
        session.scalars(
            query.order_by(Recommendation.created_at.desc(), Recommendation.id.desc()).limit(limit)
        )
    )



@router.get(
    "/towers/{tower_id}/actuator-states",
    response_model=list[ActuatorStateResponse],
)
def list_tower_actuator_states(
    tower_id: int,
    session: Session = Depends(get_session),
) -> list[ActuatorState]:
    get_tower_or_404(session, tower_id)
    return list(
        session.scalars(
            select(ActuatorState)
            .where(ActuatorState.tower_id == tower_id)
            .order_by(ActuatorState.actuator_type)
        )
    )


@router.get(
    "/towers/{tower_id}/actions",
    response_model=list[ActionLogResponse],
)
def list_tower_action_history(
    tower_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[ActionLog]:
    get_tower_or_404(session, tower_id)
    return list(
        session.scalars(
            select(ActionLog)
            .where(ActionLog.tower_id == tower_id)
            .order_by(ActionLog.occurred_at.desc(), ActionLog.id.desc())
            .limit(limit)
        )
    )


@router.post(
    "/recommendations/{recommendation_id}/approve",
    response_model=RecommendationResponse,
)
def approve_pending_recommendation(
    recommendation_id: int,
    session: Session = Depends(get_session),
) -> Recommendation:
    recommendation = get_recommendation_or_404(session, recommendation_id)
    if recommendation.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Only pending Recommendations can be approved.",
        )
    result = approve_recommendation(session, recommendation)

    if not result.valid:
        raise HTTPException(status_code=409, detail=result.reason)
    return recommendation


@router.post(
    "/recommendations/{recommendation_id}/reject",
    response_model=RecommendationResponse,
)
def reject_pending_recommendation(
    recommendation_id: int,
    payload: RecommendationReject,
    session: Session = Depends(get_session),
) -> Recommendation:
    recommendation = get_recommendation_or_404(session, recommendation_id)
    if recommendation.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Only pending Recommendations can be rejected.",
        )
    reject_recommendation(session, recommendation, reason=payload.reason)
    return recommendation


@router.post(
    "/recommendations/{recommendation_id}/execute",
    response_model=RecommendationExecutionResponse,
)
def execute_approved_recommendation(
    recommendation_id: int,
    session: Session = Depends(get_session),
) -> RecommendationExecutionResponse:
    try:
        result = execute_recommendation(session, recommendation_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RecommendationExecutionResponse(**result.__dict__)


@router.get("/assistant/status", response_model=AssistantStatusResponse)
def assistant_status() -> AssistantStatusResponse:
    return AssistantStatusResponse(
        available=assistant_available(),
        model=assistant_model(),
    )


@router.post("/assistant/chat", response_model=AssistantChatResponse)
def assistant_chat(
    payload: AssistantChatRequest,
    session: Session = Depends(get_session),
) -> AssistantChatResponse:
    authorization = None
    if payload.action_request is not None:
        action_tools = {
            "approve": "approve_recommendation",
            "reject": "reject_recommendation",
            "execute": "execute_recommendation",
        }
        authorization = TurnAuthorization(
            tool_name=action_tools[payload.action_request.action],
            recommendation_id=payload.action_request.recommendation_id,
        )

    try:
        result = run_assistant(
            session,
            payload.message,
            authorization=authorization,
        )
    except AssistantUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AssistantChatResponse(
        message=result.message,
        model=result.model,
        tool_trace=result.tool_trace,
    )
