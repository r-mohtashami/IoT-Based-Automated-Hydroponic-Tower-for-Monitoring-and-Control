from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


TowerName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
TowerKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
ProfileName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
GrowthStage = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


class TowerCreate(BaseModel):
    name: TowerName
    tower_key: TowerKey | None = None
    description: str | None = Field(default=None, max_length=2000)


class TowerUpdate(BaseModel):
    name: TowerName | None = None
    tower_key: TowerKey | None = None
    description: str | None = Field(default=None, max_length=2000)


class TowerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tower_key: str | None
    name: str
    description: str | None
    created_at: datetime


class TelemetryResponse(BaseModel):
    timestamp: datetime
    simulated: bool
    ph: float | None
    ec_ms_cm: float | None
    water_temp_c: float | None
    air_temp_c: float | None
    humidity_pct: float | None
    water_level_pct: float | None
    ppfd_umol_m2_s: float | None
    circulation_on: bool | None


class PlantProfileCreate(BaseModel):
    name: ProfileName
    targets: dict[str, Any] = Field(default_factory=dict)
    source_references: list[dict[str, Any]] = Field(default_factory=list)


class PlantProfileCopyCreate(BaseModel):
    name: ProfileName


class PlantProfileUpdate(BaseModel):
    name: ProfileName | None = None
    targets: dict[str, Any] | None = None
    source_references: list[dict[str, Any]] | None = None


class PlantProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    profile_type: str
    source_profile_id: int | None
    targets: dict[str, Any]
    source_references: list[dict[str, Any]]
    created_at: datetime


class PlantingStart(BaseModel):
    plant_profile_id: int = Field(gt=0)
    growth_stage: GrowthStage | None = None


class PlantingEnd(BaseModel):
    status: Literal["completed", "cancelled"] = "completed"


class PlantingResponse(BaseModel):
    planting_id: int
    plant_profile_id: int
    profile_name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    growth_stage: str | None
    profile_snapshot: dict[str, Any]


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tower_id: int
    planting_id: int | None
    factor: str
    status: str
    message: str
    observed_value: float | None
    details: dict[str, Any] | None
    detected_at: datetime
    resolved_at: datetime | None


class RecommendationReject(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class RecommendationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alert_id: int
    tower_id: int
    planting_id: int | None
    status: str
    reason: str
    action_type: str | None
    action_parameters: dict[str, Any] | None
    created_at: datetime
    reviewed_at: datetime | None
    revalidated_at: datetime | None
    executed_at: datetime | None
    decision_note: str | None


class RecommendationExecutionResponse(BaseModel):
    recommendation_id: int
    recommendation_status: str
    execution_status: str
    command_id: str | None
    actuator_type: str | None
    desired_state: str | None
    command_sent: bool
    execution_verified: bool
    reason: str
    revalidation: dict[str, Any]


class ActionLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recommendation_id: int | None
    event_type: str
    actuator_type: str | None
    status: str | None
    details: dict[str, Any] | None
    occurred_at: datetime


class ActuatorStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    actuator_type: str
    state: str
    state_data: dict[str, Any] | None
    observed_at: datetime


class AssistantStatusResponse(BaseModel):
    available: bool
    model: str


class AssistantActionRequest(BaseModel):
    action: Literal["approve", "reject", "execute"]
    recommendation_id: int = Field(ge=1)


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    action_request: AssistantActionRequest | None = None


class AssistantChatResponse(BaseModel):
    message: str
    model: str
    tool_trace: list[dict[str, Any]]
