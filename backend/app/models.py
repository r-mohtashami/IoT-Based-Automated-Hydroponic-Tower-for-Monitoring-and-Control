from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


JSON_DATA = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class Tower(Base):
    __tablename__ = "towers"
    __table_args__ = (
        Index("uq_towers_tower_key", "tower_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tower_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plantings: Mapped[list[Planting]] = relationship(back_populates="tower")
    telemetry_readings: Mapped[list[TelemetryReading]] = relationship(back_populates="tower")
    alerts: Mapped[list[Alert]] = relationship(back_populates="tower")
    recommendations: Mapped[list[Recommendation]] = relationship(back_populates="tower")
    actuator_states: Mapped[list[ActuatorState]] = relationship(back_populates="tower")
    action_logs: Mapped[list[ActionLog]] = relationship(back_populates="tower")


class PlantProfile(Base):
    __tablename__ = "plant_profiles"
    __table_args__ = (
        CheckConstraint(
            "profile_type IN ('system_builtin', 'user_customized', 'user_custom')",
            name="ck_plant_profiles_profile_type",
        ),
        CheckConstraint(
            "profile_type != 'user_customized' OR source_profile_id IS NOT NULL",
            name="ck_plant_profiles_customized_has_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    profile_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("plant_profiles.id"), nullable=True
    )
    targets: Mapped[dict[str, Any]] = mapped_column(
        JSON_DATA, nullable=False, default=dict
    )
    source_references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DATA, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source_profile: Mapped[PlantProfile | None] = relationship(
        remote_side="PlantProfile.id",
        back_populates="customized_copies",
    )
    customized_copies: Mapped[list[PlantProfile]] = relationship(
        back_populates="source_profile"
    )
    plantings: Mapped[list[Planting]] = relationship(back_populates="plant_profile")

    @property
    def is_builtin(self) -> bool:
        return self.profile_type == "system_builtin"

    def snapshot(self) -> dict[str, Any]:
        """Return a detached copy of the profile data used by a planting."""
        return {
            "profile_id": self.id,
            "name": self.name,
            "profile_type": self.profile_type,
            "targets": deepcopy(self.targets),
            "source_references": deepcopy(self.source_references),
        }


@event.listens_for(PlantProfile, "before_update")
def prevent_builtin_profile_update(_mapper: Any, _connection: Any, target: PlantProfile) -> None:
    state = inspect(target)
    profile_type_history = state.attrs.profile_type.history
    original_type = (
        profile_type_history.deleted[0]
        if profile_type_history.deleted
        else target.profile_type
    )
    column_changed = any(
        state.attrs[column.key].history.has_changes()
        for column in target.__table__.columns
    )
    if original_type == "system_builtin" and column_changed:
        raise ValueError(
            "Built-in plant profiles are immutable; create a user-customized copy instead."
        )


@event.listens_for(PlantProfile, "before_delete")
def prevent_builtin_profile_delete(_mapper: Any, _connection: Any, target: PlantProfile) -> None:
    if target.is_builtin:
        raise ValueError("Built-in plant profiles cannot be deleted.")


class Planting(Base):
    __tablename__ = "plantings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'cancelled')",
            name="ck_plantings_status",
        ),
        Index(
            "uq_plantings_one_active_per_tower",
            "tower_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tower_id: Mapped[int] = mapped_column(ForeignKey("towers.id"), nullable=False)
    plant_profile_id: Mapped[int] = mapped_column(
        ForeignKey("plant_profiles.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    growth_stage: Mapped[str | None] = mapped_column(String(80))
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DATA, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tower: Mapped[Tower] = relationship(back_populates="plantings")
    plant_profile: Mapped[PlantProfile] = relationship(back_populates="plantings")
    alerts: Mapped[list[Alert]] = relationship(back_populates="planting")
    recommendations: Mapped[list[Recommendation]] = relationship(back_populates="planting")

    @classmethod
    def from_profile(
        cls,
        *,
        tower_id: int,
        plant_profile: PlantProfile,
        started_at: datetime | None = None,
        growth_stage: str | None = None,
    ) -> Planting:
        if plant_profile.id is None:
            raise ValueError("PlantProfile must be persisted before starting a planting.")
        return cls(
            tower_id=tower_id,
            plant_profile_id=plant_profile.id,
            status="active",
            started_at=started_at or datetime.now(timezone.utc),
            growth_stage=growth_stage,
            profile_snapshot=plant_profile.snapshot(),
        )


class TelemetryReading(Base):
    __tablename__ = "telemetry_readings"
    __table_args__ = (
        Index("ix_telemetry_tower_recorded_at", "tower_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tower_id: Mapped[int] = mapped_column(ForeignKey("towers.id"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="simulator")

    ph: Mapped[float | None] = mapped_column(Float)
    ec_ms_cm: Mapped[float | None] = mapped_column(Float)
    water_temp_c: Mapped[float | None] = mapped_column(Float)
    air_temp_c: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    water_level_pct: Mapped[float | None] = mapped_column(Float)
    ppfd_umol_m2_s: Mapped[float | None] = mapped_column(Float)
    circulation_on: Mapped[bool | None] = mapped_column(nullable=True)

    tower: Mapped[Tower] = relationship(back_populates="telemetry_readings")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_alerts_status",
        ),
        Index("ix_alerts_tower_status", "tower_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tower_id: Mapped[int] = mapped_column(ForeignKey("towers.id"), nullable=False)
    planting_id: Mapped[int | None] = mapped_column(ForeignKey("plantings.id"))
    factor: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON_DATA)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tower: Mapped[Tower] = relationship(back_populates="alerts")
    planting: Mapped[Planting | None] = relationship(back_populates="alerts")
    recommendations: Mapped[list[Recommendation]] = relationship(back_populates="alert")


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'stale', 'executed', 'failed')",
            name="ck_recommendations_status",
        ),
        Index("ix_recommendations_tower_status", "tower_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), nullable=False)
    tower_id: Mapped[int] = mapped_column(ForeignKey("towers.id"), nullable=False)
    planting_id: Mapped[int | None] = mapped_column(ForeignKey("plantings.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str | None] = mapped_column(String(80))
    action_parameters: Mapped[dict[str, Any] | None] = mapped_column(JSON_DATA)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

    alert: Mapped[Alert] = relationship(back_populates="recommendations")
    tower: Mapped[Tower] = relationship(back_populates="recommendations")
    planting: Mapped[Planting | None] = relationship(back_populates="recommendations")
    action_logs: Mapped[list[ActionLog]] = relationship(back_populates="recommendation")


class ActuatorState(Base):
    __tablename__ = "actuator_states"
    __table_args__ = (
        UniqueConstraint(
            "tower_id", "actuator_type", name="uq_actuator_states_tower_actuator"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tower_id: Mapped[int] = mapped_column(ForeignKey("towers.id"), nullable=False)
    actuator_type: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(80), nullable=False)
    state_data: Mapped[dict[str, Any] | None] = mapped_column(JSON_DATA)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    tower: Mapped[Tower] = relationship(back_populates="actuator_states")


class ActionLog(Base):
    __tablename__ = "action_logs"
    __table_args__ = (
        Index("ix_action_logs_tower_occurred_at", "tower_id", "occurred_at"),
        Index(
            "uq_action_logs_one_execution_request_per_recommendation",
            "recommendation_id",
            unique=True,
            postgresql_where=text("event_type = 'execution_requested'"),
            sqlite_where=text("event_type = 'execution_requested'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tower_id: Mapped[int] = mapped_column(ForeignKey("towers.id"), nullable=False)
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id")
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actuator_type: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str | None] = mapped_column(String(40))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON_DATA)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    tower: Mapped[Tower] = relationship(back_populates="action_logs")
    recommendation: Mapped[Recommendation | None] = relationship(back_populates="action_logs")
