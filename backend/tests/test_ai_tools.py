import json
from datetime import datetime, timedelta, timezone

import pytest

import app.ai_action_tools as ai_action_tools
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai_assistant import ACTION_TOOLS, READ_TOOLS, TurnAuthorization, assistant_tools
from app.actuator_execution import ExecutionResult
from app.ai_tools import (
    approve_recommendation_tool,
    explain_alert_tool,
    get_latest_telemetry_tool,
    reject_recommendation_tool,
)
from app.models import Alert, Base, PlantProfile, Planting, Recommendation, TelemetryReading, Tower
from app.seed_builtin_profiles import seed_builtin_profiles
from app.telemetry_store import process_telemetry_message


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _payload(tower_key: str, timestamp: datetime, *, ph: float | None) -> str:
    data = {
        "simulated": True,
        "tower_key": tower_key,
        "timestamp": timestamp.isoformat(),
        "ec_ms_cm": 1.4,
        "water_temp_c": 21.0,
        "air_temp_c": 23.0,
        "humidity_pct": 60.0,
        "water_level_pct": 82.0,
        "ppfd_umol_m2_s": 320.0,
        "circulation_on": True,
    }
    if ph is not None:
        data["ph"] = ph
    return json.dumps(data)


def _start_basil(session: Session, tower_key: str = "sim-ai") -> tuple[Tower, Planting]:
    seed_builtin_profiles(session)
    basil = session.scalar(select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic"))
    assert basil is not None
    tower = Tower(name="AI Tool Tower", tower_key=tower_key)
    session.add(tower)
    session.commit()
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=basil)
    session.add(planting)
    session.commit()
    return tower, planting


def _process(session: Session, tower: Tower, when: datetime, ph: float | None) -> None:
    process_telemetry_message(
        session,
        f"hydroponics/towers/{tower.tower_key}/telemetry",
        _payload(tower.tower_key, when, ph=ph),
    )


def test_assistant_exposes_only_controlled_application_tools() -> None:
    tools = assistant_tools(TurnAuthorization("execute_recommendation", 11))
    names = {tool["name"] for tool in tools}

    assert names == {
        "list_towers",
        "get_tower_status",
        "get_active_planting",
        "get_latest_telemetry",
        "get_open_alerts",
        "get_recommendations",
        "get_recommendation_status",
        "explain_alert",
        "explain_recommendation",
        "get_action_history",
        "execute_recommendation",
    }
    assert "approve_recommendation" not in names
    assert "reject_recommendation" not in names
    assert all(tool["type"] == "function" for tool in tools)
    assert not any(name in names for name in {"sql", "raw_sql", "mqtt_publish", "mqtt_subscribe", "shell", "arbitrary_actuator"})

    execute = next(tool for tool in ACTION_TOOLS if tool["name"] == "execute_recommendation")
    assert set(execute["parameters"]["properties"]) == {"recommendation_id"}
    assert "actuator_type" not in execute["parameters"]["properties"]
    assert "duration" not in execute["parameters"]["properties"]
    assert "amount" not in execute["parameters"]["properties"]
    assert len(READ_TOOLS) == 10


def test_latest_telemetry_reports_missing_sensor_as_not_monitored(session: Session) -> None:
    tower = Tower(name="Missing Sensor AI Tower", tower_key="sim-missing-ai")
    session.add(tower)
    session.commit()
    session.add(
        TelemetryReading(
            tower_id=tower.id,
            recorded_at=datetime.now(timezone.utc),
            source="simulator",
            ph=5.8,
            ppfd_umol_m2_s=None,
            circulation_on=True,
        )
    )
    session.commit()

    result = get_latest_telemetry_tool(session, tower.id)

    assert result["latest_telemetry"]["simulated"] is True
    assert result["latest_telemetry"]["sensors"]["ppfd_umol_m2_s"] == {
        "status": "not_monitored",
        "value": None,
    }


def test_alert_explanation_uses_persisted_snapshot_target_and_source(session: Session) -> None:
    tower, _planting = _start_basil(session)
    _process(session, tower, datetime.now(timezone.utc) - timedelta(seconds=2), ph=5.2)
    alert = session.scalar(select(Alert))
    assert alert is not None

    result = explain_alert_tool(session, alert.id)

    explanation = result["explanation"]
    assert explanation["configured_target_min"] == 5.5
    assert explanation["configured_target_max"] == 6.0
    assert explanation["direction"] == "below"
    assert explanation["target_source_reference"]["institution"] == "Oklahoma State University Extension"
    assert explanation["target_source_reference"]["publication_id"] == "HLA-6722"
    assert explanation["claim_scope"] == "outside configured target range; not an emergency or safety-limit claim"


def test_ai_approval_wrapper_cannot_bypass_recovery_revalidation(session: Session) -> None:
    tower, _planting = _start_basil(session, "sim-ai-stale")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _process(session, tower, now - timedelta(seconds=2), ph=5.2)
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None
    _process(session, tower, now - timedelta(seconds=1), ph=5.8)

    result = approve_recommendation_tool(session, recommendation.id)

    # The monitoring Alert is intentionally debounced, but the controlled
    # approval path still revalidates the latest telemetry immediately.
    assert result["preflight"]["recommendation"]["status"] == "pending"
    assert result["preflight"]["current_context"]["current_factor_evaluation"]["status"] == "within"
    assert result["decision"]["status"] == "stale"
    assert result["decision"]["revalidation_passed"] is False
    assert "returned within" in result["decision"]["reason"]


def test_ai_reject_wrapper_uses_existing_decision_path(session: Session) -> None:
    tower, _planting = _start_basil(session, "sim-ai-reject")
    _process(session, tower, datetime.now(timezone.utc) - timedelta(seconds=1), ph=5.2)
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None

    result = reject_recommendation_tool(session, recommendation.id, "Owner rejected from AI interface")

    assert result["decision"]["status"] == "rejected"
    assert result["decision"]["execution_created"] is False
    stored = session.get(Recommendation, recommendation.id)
    assert stored is not None
    assert stored.decision_note == "Owner rejected from AI interface"


def test_execute_wrapper_preserves_command_sent_vs_verified_distinction(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tower, _planting = _start_basil(session, "sim-ai-execute")
    _process(session, tower, datetime.now(timezone.utc) - timedelta(seconds=1), ph=6.2)
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None
    recommendation.status = "approved"
    session.commit()

    def fake_execute(_session: Session, recommendation_id: int) -> ExecutionResult:
        return ExecutionResult(
            recommendation_id=recommendation_id,
            recommendation_status="failed",
            execution_status="failed",
            command_id="cmd-test",
            actuator_type="review_indicator",
            desired_state="on",
            command_sent=True,
            execution_verified=False,
            reason="Command sent but no correlated acknowledgement was received.",
            revalidation={"current_evaluation": "above"},
        )

    monkeypatch.setattr(ai_action_tools, "execute_controlled_recommendation", fake_execute)
    result = ai_action_tools.execute_recommendation_tool(session, recommendation.id)

    execution = result["execution"]
    assert execution["command_sent"] is True
    assert execution["execution_verified"] is False
    assert execution["simulated"] is True
    assert execution["approval_is_execution"] is False
    assert "separate application evidence fields" in execution["verification_note"]


def test_execute_wrapper_preserves_phase8_approval_requirement(session: Session) -> None:
    tower, _planting = _start_basil(session, "sim-ai-execute-pending")
    _process(session, tower, datetime.now(timezone.utc) - timedelta(seconds=1), ph=6.2)
    recommendation = session.scalar(select(Recommendation))
    assert recommendation is not None
    assert recommendation.status == "pending"

    result = ai_action_tools.execute_recommendation_tool(session, recommendation.id)

    assert result["preflight"]["recommendation"]["status"] == "pending"
    assert "error" in result
    assert "approved" in result["error"].lower()
    stored = session.get(Recommendation, recommendation.id)
    assert stored is not None
    assert stored.status == "pending"
