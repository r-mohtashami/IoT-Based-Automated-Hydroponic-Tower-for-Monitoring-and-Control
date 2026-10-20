import json
import os
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+pysqlite:////tmp/hydroponic_phase3_bootstrap.db",
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.actuator_mqtt import ActuatorAck, MqttExchangeResult
from app.db import get_session
from app.main import app
from app.models import ActionLog, ActuatorState, Alert, Base, PlantProfile, Planting, Recommendation, TelemetryReading
from app.seed_builtin_profiles import seed_builtin_profiles
from app.telemetry_store import process_telemetry_message


@pytest.fixture()
def api_environment() -> Generator[tuple[TestClient, object], None, None]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)

    def override_get_session() -> Generator[Session, None, None]:
        with Session(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as client:
        yield client, engine
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


def test_tower_create_list_get_and_update(api_environment) -> None:
    client, _engine = api_environment

    create_response = client.post(
        "/towers",
        json={"name": "Home Tower", "description": "Kitchen test tower"},
    )
    assert create_response.status_code == 201
    tower = create_response.json()
    assert tower["name"] == "Home Tower"

    tower_id = tower["id"]
    list_response = client.get("/towers")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [tower_id]

    get_response = client.get(f"/towers/{tower_id}")
    assert get_response.status_code == 200
    assert get_response.json()["description"] == "Kitchen test tower"

    update_response = client.patch(
        f"/towers/{tower_id}",
        json={"name": "Home Tower Updated", "description": None},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Home Tower Updated"
    assert update_response.json()["description"] is None


def test_custom_profile_can_be_created_and_updated(api_environment) -> None:
    client, _engine = api_environment

    response = client.post(
        "/plant-profiles/custom",
        json={
            "name": "My Custom Profile",
            "targets": {"ph": {"min": 5.4, "max": 6.1}},
            "source_references": [{"label": "owner-configured-test"}],
        },
    )
    assert response.status_code == 201
    profile = response.json()
    assert profile["profile_type"] == "user_custom"
    assert profile["source_profile_id"] is None

    update_response = client.patch(
        f"/plant-profiles/{profile['id']}",
        json={"name": "My Updated Profile", "targets": {"ph": {"min": 5.3, "max": 6.2}}},
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["name"] == "My Updated Profile"
    assert updated["targets"]["ph"]["min"] == 5.3


def test_customized_copy_preserves_source_linkage_and_copied_data(api_environment) -> None:
    client, _engine = api_environment

    source = client.post(
        "/plant-profiles/custom",
        json={
            "name": "Source Profile",
            "targets": {"ec_ms_cm": {"min": 1.1, "max": 1.7}},
            "source_references": [{"label": "source-reference"}],
        },
    ).json()

    copy_response = client.post(
        f"/plant-profiles/{source['id']}/customized-copy",
        json={"name": "Customized Copy"},
    )
    assert copy_response.status_code == 201
    copied = copy_response.json()
    assert copied["profile_type"] == "user_customized"
    assert copied["source_profile_id"] == source["id"]
    assert copied["targets"] == source["targets"]
    assert copied["source_references"] == source["source_references"]

    client.patch(
        f"/plant-profiles/{source['id']}",
        json={"targets": {"ec_ms_cm": {"min": 1.0, "max": 1.8}}},
    )
    stored_copy = client.get(f"/plant-profiles/{copied['id']}").json()
    assert stored_copy["targets"]["ec_ms_cm"]["min"] == 1.1


def test_builtin_profile_update_is_rejected_by_api(api_environment) -> None:
    client, engine = api_environment

    with Session(engine) as session:
        built_in = PlantProfile(
            name="Built-in Test Placeholder",
            profile_type="system_builtin",
            targets={},
            source_references=[],
        )
        session.add(built_in)
        session.commit()
        profile_id = built_in.id

    response = client.patch(
        f"/plant-profiles/{profile_id}",
        json={"name": "Attempted overwrite"},
    )
    assert response.status_code == 403
    assert "customized copy" in response.json()["detail"]


def test_planting_lifecycle_conflict_and_snapshot_independence(api_environment) -> None:
    client, _engine = api_environment

    tower = client.post("/towers", json={"name": "Planting Tower"}).json()
    profile = client.post(
        "/plant-profiles/custom",
        json={
            "name": "Planting Profile",
            "targets": {"ph": {"min": 5.4, "max": 6.1}},
            "source_references": [{"label": "test-reference"}],
        },
    ).json()

    start_response = client.post(
        f"/towers/{tower['id']}/planting",
        json={"plant_profile_id": profile["id"], "growth_stage": "test-stage"},
    )
    assert start_response.status_code == 201
    assert start_response.json()["status"] == "active"
    assert isinstance(start_response.json()["planting_id"], int)
    assert start_response.json()["profile_snapshot"]["targets"]["ph"]["min"] == 5.4
    assert "profile_id" not in start_response.json()["profile_snapshot"]

    second_start = client.post(
        f"/towers/{tower['id']}/planting",
        json={"plant_profile_id": profile["id"]},
    )
    assert second_start.status_code == 409
    assert second_start.json()["detail"] == "Tower already has an active planting."

    profile_update = client.patch(
        f"/plant-profiles/{profile['id']}",
        json={"targets": {"ph": {"min": 5.3, "max": 6.2}}},
    )
    assert profile_update.status_code == 200

    active_response = client.get(f"/towers/{tower['id']}/planting")
    assert active_response.status_code == 200
    active = active_response.json()
    assert active["profile_snapshot"]["targets"]["ph"]["min"] == 5.4

    end_response = client.post(
        f"/towers/{tower['id']}/planting/end",
        json={"status": "completed"},
    )
    assert end_response.status_code == 200
    assert end_response.json()["status"] == "completed"
    assert end_response.json()["ended_at"] is not None

    no_active = client.get(f"/towers/{tower['id']}/planting")
    assert no_active.status_code == 404

    new_start = client.post(
        f"/towers/{tower['id']}/planting",
        json={"plant_profile_id": profile["id"]},
    )
    assert new_start.status_code == 201
    assert new_start.json()["status"] == "active"


def test_ending_planting_closes_open_alerts_and_unexecuted_recommendations(api_environment) -> None:
    client, engine = api_environment

    tower = client.post("/towers", json={"name": "Lifecycle Tower", "tower_key": "sim-lifecycle"}).json()
    profile = client.post(
        "/plant-profiles/custom",
        json={
            "name": "Lifecycle Plant",
            "targets": {"ph": {"min": 5.5, "max": 6.0}},
            "source_references": [],
        },
    ).json()
    planting = client.post(
        f"/towers/{tower['id']}/planting",
        json={"plant_profile_id": profile["id"]},
    ).json()

    with Session(engine) as session:
        process_telemetry_message(
            session,
            "hydroponics/towers/sim-lifecycle/telemetry",
            json.dumps(
                {
                    "simulated": True,
                    "tower_key": "sim-lifecycle",
                    "timestamp": "2026-08-26T18:00:00Z",
                    "ph": 6.4,
                    "ec_ms_cm": 1.4,
                    "water_temp_c": 21.0,
                    "air_temp_c": 23.0,
                    "humidity_pct": 60.0,
                    "water_level_pct": 80.0,
                    "ppfd_umol_m2_s": 300.0,
                    "circulation_on": True,
                }
            ),
        )

    response = client.post(
        f"/towers/{tower['id']}/planting/end",
        json={"status": "completed"},
    )
    assert response.status_code == 200

    with Session(engine) as session:
        alert = session.scalar(select(Alert).where(Alert.planting_id == planting["planting_id"]))
        recommendation = session.scalar(
            select(Recommendation).where(Recommendation.planting_id == planting["planting_id"])
        )
        assert alert is not None
        assert alert.status == "resolved"
        assert alert.details["resolution_reason"].startswith("The planting ended")
        assert recommendation is not None
        assert recommendation.status == "stale"
        event = session.scalar(
            select(ActionLog).where(
                ActionLog.recommendation_id == recommendation.id,
                ActionLog.event_type == "recommendation_stale",
            )
        )
        assert event is not None


def test_tower_key_is_exposed_and_must_be_unique(api_environment) -> None:
    client, _engine = api_environment

    first = client.post(
        "/towers",
        json={"name": "Mapped Tower", "tower_key": "sim-1"},
    )
    assert first.status_code == 201
    assert first.json()["tower_key"] == "sim-1"

    duplicate = client.post(
        "/towers",
        json={"name": "Duplicate Mapping", "tower_key": "sim-1"},
    )
    assert duplicate.status_code == 409


def test_latest_and_history_telemetry_are_tower_scoped_ordered_and_limited(
    api_environment,
) -> None:
    client, engine = api_environment

    first_tower = client.post(
        "/towers", json={"name": "Telemetry Tower 1", "tower_key": "sim-1"}
    ).json()
    second_tower = client.post(
        "/towers", json={"name": "Telemetry Tower 2", "tower_key": "sim-2"}
    ).json()

    with Session(engine) as session:
        session.add_all(
            [
                TelemetryReading(
                    tower_id=first_tower["id"],
                    recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
                    source="simulator",
                    ph=6.0,
                    circulation_on=True,
                ),
                TelemetryReading(
                    tower_id=first_tower["id"],
                    recorded_at=datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc),
                    source="simulator",
                    ph=6.1,
                    circulation_on=True,
                ),
                TelemetryReading(
                    tower_id=first_tower["id"],
                    recorded_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
                    source="simulator",
                    ph=6.2,
                    circulation_on=False,
                ),
                TelemetryReading(
                    tower_id=second_tower["id"],
                    recorded_at=datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc),
                    source="simulator",
                    ph=7.7,
                    circulation_on=True,
                ),
            ]
        )
        session.commit()

    latest = client.get(f"/towers/{first_tower['id']}/telemetry/latest")
    assert latest.status_code == 200
    assert latest.json()["ph"] == 6.2
    assert latest.json()["simulated"] is True
    assert latest.json()["circulation_on"] is False

    history = client.get(
        f"/towers/{first_tower['id']}/telemetry/history",
        params={"limit": 2},
    )
    assert history.status_code == 200
    items = history.json()
    assert [item["ph"] for item in items] == [6.1, 6.2]
    assert [item["timestamp"] for item in items] == sorted(
        item["timestamp"] for item in items
    )
    assert all(item["ph"] != 7.7 for item in items)

    invalid_limit = client.get(
        f"/towers/{first_tower['id']}/telemetry/history",
        params={"limit": 1001},
    )
    assert invalid_limit.status_code == 422


def test_seeded_builtin_profile_update_is_rejected_by_api(api_environment) -> None:
    client, engine = api_environment

    with Session(engine) as session:
        seed_builtin_profiles(session)
        built_in = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
        )
        assert built_in is not None
        profile_id = built_in.id

    response = client.patch(
        f"/plant-profiles/{profile_id}",
        json={"targets": {"ph": {"min": 1, "max": 2}}},
    )
    assert response.status_code == 403
    assert "customized copy" in response.json()["detail"]


def test_alert_and_recommendation_endpoints_are_tower_scoped(api_environment) -> None:
    client, engine = api_environment

    tower = client.post(
        "/towers",
        json={"name": "Rule API Tower", "tower_key": "sim-rule-api"},
    ).json()

    with Session(engine) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
        )
        assert basil is not None
        planting = Planting.from_profile(tower_id=tower["id"], plant_profile=basil)
        session.add(planting)
        session.commit()

        process_telemetry_message(
            session,
            "hydroponics/towers/sim-rule-api/telemetry",
            json.dumps(
                {
                    "simulated": True,
                    "tower_key": "sim-rule-api",
                    "timestamp": "2026-08-26T13:00:00Z",
                    "ph": 5.2,
                    "ec_ms_cm": 1.4,
                    "circulation_on": True,
                }
            ),
        )

    alerts = client.get(f"/towers/{tower['id']}/alerts")
    assert alerts.status_code == 200
    alert_items = alerts.json()
    assert len(alert_items) == 1
    assert alert_items[0]["factor"] == "ph"
    assert alert_items[0]["observed_value"] == 5.2
    assert alert_items[0]["details"]["target_min"] == 5.5
    assert alert_items[0]["details"]["target_max"] == 6.0
    assert alert_items[0]["details"]["direction"] == "below"

    recommendations = client.get(f"/towers/{tower['id']}/recommendations")
    assert recommendations.status_code == 200
    recommendation_items = recommendations.json()
    assert len(recommendation_items) == 1
    assert recommendation_items[0]["action_type"] == "increase_ph"
    assert recommendation_items[0]["action_parameters"]["executable"] is True
    assert "outside the configured target range" in recommendation_items[0]["reason"]


def test_recommendation_approve_endpoint_revalidates_and_records_decision(api_environment) -> None:
    client, engine = api_environment
    tower = client.post(
        "/towers",
        json={"name": "Approval Tower", "tower_key": "sim-approval-api"},
    ).json()

    with Session(engine) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
        )
        assert basil is not None
        session.add(Planting.from_profile(tower_id=tower["id"], plant_profile=basil))
        session.commit()
        now = datetime.now(timezone.utc)
        process_telemetry_message(
            session,
            "hydroponics/towers/sim-approval-api/telemetry",
            json.dumps(
                {
                    "simulated": True,
                    "tower_key": "sim-approval-api",
                    "timestamp": now.isoformat(),
                    "ph": 5.2,
                    "ec_ms_cm": 1.4,
                    "circulation_on": True,
                }
            ),
        )
        recommendation = session.scalar(select(Recommendation))
        assert recommendation is not None
        recommendation_id = recommendation.id

    response = client.post(f"/recommendations/{recommendation_id}/approve")
    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "approved"
    assert item["reviewed_at"] is not None
    assert item["revalidated_at"] is not None
    assert "Revalidation passed" in item["decision_note"]

    second_decision = client.post(f"/recommendations/{recommendation_id}/approve")
    assert second_decision.status_code == 409

    with Session(engine) as session:
        log = session.scalar(select(ActionLog))
        assert log is not None
        assert log.event_type == "recommendation_approved"
        assert log.details["execution_created"] is False


def test_recommendation_reject_endpoint_records_optional_reason(api_environment) -> None:
    client, engine = api_environment
    tower = client.post(
        "/towers",
        json={"name": "Reject Tower", "tower_key": "sim-reject-api"},
    ).json()

    with Session(engine) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
        )
        assert basil is not None
        session.add(Planting.from_profile(tower_id=tower["id"], plant_profile=basil))
        session.commit()
        process_telemetry_message(
            session,
            "hydroponics/towers/sim-reject-api/telemetry",
            json.dumps(
                {
                    "simulated": True,
                    "tower_key": "sim-reject-api",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "ph": 5.2,
                    "ec_ms_cm": 1.4,
                    "circulation_on": True,
                }
            ),
        )
        recommendation = session.scalar(select(Recommendation))
        assert recommendation is not None
        recommendation_id = recommendation.id

    response = client.post(
        f"/recommendations/{recommendation_id}/reject",
        json={"reason": "Owner chose to keep monitoring."},
    )
    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "rejected"
    assert item["decision_note"] == "Owner chose to keep monitoring."
    assert item["reviewed_at"] is not None
    assert item["revalidated_at"] is None

    second_decision = client.post(f"/recommendations/{recommendation_id}/approve")
    assert second_decision.status_code == 409


def test_execute_approved_recommendation_endpoint_reports_verified_simulated_execution(
    api_environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = api_environment
    now = datetime.now(timezone.utc).replace(microsecond=0)

    tower = client.post(
        "/towers",
        json={"name": "Execution API Tower", "tower_key": "sim-execution-api"},
    ).json()
    with Session(engine, expire_on_commit=False) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
        )
        assert basil is not None
        session.add(Planting.from_profile(tower_id=tower["id"], plant_profile=basil))
        session.commit()
        process_telemetry_message(
            session,
            "hydroponics/towers/sim-execution-api/telemetry",
            json.dumps(
                {
                    "simulated": True,
                    "tower_key": "sim-execution-api",
                    "timestamp": (now - timedelta(seconds=1)).isoformat(),
                    "ph": 5.2,
                    "ec_ms_cm": 1.4,
                    "circulation_on": True,
                }
            ),
        )
        recommendation = session.scalar(select(Recommendation))
        assert recommendation is not None
        recommendation_id = recommendation.id

    approve_response = client.post(f"/recommendations/{recommendation_id}/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "approved"

    def fake_exchange(claim):
        assert claim.command_id is not None
        assert claim.tower_key == "sim-execution-api"
        assert claim.action is not None
        ack_at = datetime.now(timezone.utc)
        return MqttExchangeResult(
            command_sent=True,
            sent_at=ack_at,
            ack=ActuatorAck(
                command_id=claim.command_id,
                tower_key=claim.tower_key,
                actuator_type=claim.action.actuator_type,
                state=claim.action.state,
                    timestamp=ack_at,
            ),
        )

    monkeypatch.setattr("app.actuator_execution.publish_and_wait_for_ack", fake_exchange)
    execute_response = client.post(f"/recommendations/{recommendation_id}/execute")

    assert execute_response.status_code == 200
    body = execute_response.json()
    assert body["recommendation_status"] == "executed"
    assert body["execution_status"] == "verified"
    assert body["command_sent"] is True
    assert body["execution_verified"] is True
    assert body["actuator_type"] == "ph_up_pump"
    assert body["desired_state"] == "completed"



def test_assistant_api_maps_structured_action_request_to_exact_authorization(
    api_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _engine = api_environment
    captured = {}

    def fake_run_assistant(_session, message, *, authorization=None, client=None, model=None):
        captured["message"] = message
        captured["authorization"] = authorization
        return type("Result", (), {"message": "ok", "model": "test-model", "tool_trace": []})()

    monkeypatch.setattr("app.api.run_assistant", fake_run_assistant)
    response = client.post(
        "/assistant/chat",
        json={
            "message": "Approve recommendation 7.",
            "action_request": {"action": "approve", "recommendation_id": 7},
        },
    )

    assert response.status_code == 200
    authorization = captured["authorization"]
    assert authorization.tool_name == "approve_recommendation"
    assert authorization.recommendation_id == 7


def test_legacy_generic_allow_actions_flag_does_not_grant_mutation_authority(
    api_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _engine = api_environment
    captured = {}

    def fake_run_assistant(_session, message, *, authorization=None, client=None, model=None):
        captured["authorization"] = authorization
        return type("Result", (), {"message": "read-only", "model": "test-model", "tool_trace": []})()

    monkeypatch.setattr("app.api.run_assistant", fake_run_assistant)
    response = client.post(
        "/assistant/chat",
        json={"message": "Show recommendation 7 status.", "allow_actions": True},
    )

    assert response.status_code == 200
    assert captured["authorization"] is None

def test_assistant_unavailable_does_not_break_core_api(api_environment, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _engine = api_environment

    class DownClient:
        def model_available(self, _model: str) -> bool:
            return False

    monkeypatch.setattr("app.ai_assistant.OllamaClient", lambda: DownClient())

    assistant_response = client.post(
        "/assistant/chat",
        json={"message": "Show tower status"},
    )
    assert assistant_response.status_code == 503
    assert "local Ollama model" in assistant_response.json()["detail"]

    core_response = client.get("/towers")
    assert core_response.status_code == 200


def test_action_history_is_available_through_rest(api_environment) -> None:
    client, engine = api_environment
    tower = client.post("/towers", json={"name": "History Tower"}).json()

    with Session(engine) as session:
        session.add(
            ActionLog(
                tower_id=tower["id"],
                recommendation_id=None,
                event_type="execution_requested",
                actuator_type="review_indicator",
                status="requested",
                details={"simulated": True},
                occurred_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    response = client.get(f"/towers/{tower['id']}/actions")
    assert response.status_code == 200
    history = response.json()
    assert len(history) == 1
    assert history[0]["event_type"] == "execution_requested"
    assert history[0]["actuator_type"] == "review_indicator"
    assert history[0]["details"]["simulated"] is True


def test_assistant_status_reports_unavailable_local_model_without_breaking_ui_api(
    api_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _engine = api_environment
    monkeypatch.setenv("OLLAMA_MODEL", "test-model")
    monkeypatch.setattr("app.api.assistant_available", lambda: False)

    response = client.get("/assistant/status")
    assert response.status_code == 200
    assert response.json() == {"available": False, "model": "test-model"}


def test_verified_actuator_state_is_available_through_rest(api_environment) -> None:
    client, engine = api_environment
    tower = client.post("/towers", json={"name": "Actuator State Tower"}).json()

    with Session(engine) as session:
        session.add(
            ActuatorState(
                tower_id=tower["id"],
                actuator_type="review_indicator",
                state="on",
                state_data={
                    "simulated": True,
                    "command_id": "cmd-state",
                    "verification_source": "simulator_ack",
                },
                observed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    response = client.get(f"/towers/{tower['id']}/actuator-states")
    assert response.status_code == 200
    states = response.json()
    assert len(states) == 1
    assert states[0]["actuator_type"] == "review_indicator"
    assert states[0]["state"] == "on"
    assert states[0]["state_data"]["simulated"] is True
    assert states[0]["state_data"]["verification_source"] == "simulator_ack"


def test_custom_profile_rejects_unknown_factor_and_applies_unit_and_user_provenance(api_environment) -> None:
    client, _engine = api_environment
    bad = client.post('/plant-profiles/custom', json={'name':'Bad','targets':{'mystery':{'min':1,'max':2}},'source_references':[]})
    assert bad.status_code == 422
    good = client.post('/plant-profiles/custom', json={'name':'User pH','targets':{'ph':{'min':5.4,'max':6.2,'unit':'wrong'}},'source_references':[]})
    assert good.status_code == 201
    target = good.json()['targets']['ph']
    assert target['unit'] == 'pH'
    assert target['provenance'] == 'user_configured'


def test_customized_profile_edit_drops_inherited_factor_evidence(api_environment) -> None:
    client, engine = api_environment
    with Session(engine) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(select(PlantProfile).where(PlantProfile.name == 'Basil — Hydroponic'))
        profile_id = basil.id
    copied = client.post(f'/plant-profiles/{profile_id}/customized-copy', json={'name':'My Basil'}).json()
    assert copied['targets']['ph']['provenance'] == 'system_evidence_backed'
    edited = client.patch(f"/plant-profiles/{copied['id']}", json={'targets':{'ph':{'min':5.4,'max':6.2}}})
    assert edited.status_code == 200
    target = edited.json()['targets']['ph']
    assert target['provenance'] == 'user_configured'
    assert 'source_reference_id' not in target


def test_delete_tower_removes_local_history_but_preserves_plant_profile(api_environment) -> None:
    client, engine = api_environment

    tower = client.post(
        "/towers",
        json={"name": "Disposable Tower", "tower_key": "sim-disposable"},
    ).json()
    profile = client.post(
        "/plant-profiles/custom",
        json={
            "name": "Reusable Plant",
            "targets": {"ph": {"min": 5.5, "max": 6.0}},
            "source_references": [],
        },
    ).json()
    planting = client.post(
        f"/towers/{tower['id']}/planting",
        json={"plant_profile_id": profile["id"]},
    ).json()

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        reading = TelemetryReading(
            tower_id=tower["id"],
            recorded_at=now,
            source="simulator",
            ph=6.4,
            ec_ms_cm=1.4,
            water_temp_c=21.0,
            air_temp_c=23.0,
            humidity_pct=60.0,
            water_level_pct=80.0,
            ppfd_umol_m2_s=300.0,
            circulation_on=True,
        )
        session.add(reading)
        session.flush()
        alert = Alert(
            tower_id=tower["id"],
            planting_id=planting["planting_id"],
            factor="ph",
            status="open",
            message="test",
            observed_value=6.4,
            details={"direction": "above"},
            detected_at=now,
        )
        session.add(alert)
        session.flush()
        recommendation = Recommendation(
            alert_id=alert.id,
            tower_id=tower["id"],
            planting_id=planting["planting_id"],
            status="pending",
            reason="test",
            action_type="decrease_ph",
            action_parameters={"executable": True},
        )
        session.add(recommendation)
        session.flush()
        session.add(
            ActionLog(
                tower_id=tower["id"],
                recommendation_id=recommendation.id,
                event_type="recommendation_created",
                status="pending",
                details={},
                occurred_at=now,
            )
        )
        session.add(
            ActuatorState(
                tower_id=tower["id"],
                actuator_type="ph_down_pump",
                state="completed",
                state_data={},
                observed_at=now,
            )
        )
        session.commit()

    response = client.delete(f"/towers/{tower['id']}")
    assert response.status_code == 204
    assert client.get(f"/towers/{tower['id']}").status_code == 404

    with Session(engine) as session:
        assert session.get(PlantProfile, profile["id"]) is not None
        assert session.scalar(select(Planting).where(Planting.tower_id == tower["id"])) is None
        assert session.scalar(select(TelemetryReading).where(TelemetryReading.tower_id == tower["id"])) is None
        assert session.scalar(select(Alert).where(Alert.tower_id == tower["id"])) is None
        assert session.scalar(select(Recommendation).where(Recommendation.tower_id == tower["id"])) is None
        assert session.scalar(select(ActionLog).where(ActionLog.tower_id == tower["id"])) is None
        assert session.scalar(select(ActuatorState).where(ActuatorState.tower_id == tower["id"])) is None


def test_delete_unknown_tower_returns_404(api_environment) -> None:
    client, _engine = api_environment
    response = client.delete("/towers/99999")
    assert response.status_code == 404


def test_customized_profile_edit_preserves_unchanged_factor_evidence(api_environment) -> None:
    client, engine = api_environment
    with Session(engine) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic")
        )
        profile_id = basil.id

    copied = client.post(
        f"/plant-profiles/{profile_id}/customized-copy",
        json={"name": "My Factor-Level Basil"},
    ).json()
    replacement = {
        "ph": {"min": 5.6, "max": 6.1},
        "ec_ms_cm": copied["targets"]["ec_ms_cm"],
    }
    edited = client.patch(
        f"/plant-profiles/{copied['id']}",
        json={"targets": replacement},
    )
    assert edited.status_code == 200
    targets = edited.json()["targets"]
    assert targets["ph"]["provenance"] == "user_configured"
    assert "source_reference_id" not in targets["ph"]
    assert targets["ec_ms_cm"]["provenance"] == "system_evidence_backed"
    assert "source_reference_id" in targets["ec_ms_cm"]


def test_tower_without_explicit_key_gets_stable_backend_device_key(api_environment) -> None:
    client, _engine = api_environment
    first = client.post("/towers", json={"name": "Auto Key One"})
    second = client.post("/towers", json={"name": "Auto Key Two"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["tower_key"] == f"sim-{first.json()['id']}"
    assert second.json()["tower_key"] == f"sim-{second.json()['id']}"
    assert first.json()["tower_key"] != second.json()["tower_key"]
