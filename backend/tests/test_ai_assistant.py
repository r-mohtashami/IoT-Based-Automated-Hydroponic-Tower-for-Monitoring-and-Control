from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.ai_assistant as assistant
from app.ai_assistant import (
    AssistantUnavailableError,
    TurnAuthorization,
    assistant_tools,
    run_assistant,
)
from app.models import Base, PlantProfile, Planting, TelemetryReading, Tower
from app.seed_builtin_profiles import seed_builtin_profiles


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def model_available(self, _model: str) -> bool:
        return True


def _function_response(name: str, arguments: dict | str):
    if isinstance(arguments, str):
        import json
        arguments = json.loads(arguments)
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }


def _text_response(text: str):
    return {"message": {"role": "assistant", "content": text}}


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _start_basil(session: Session, tower_key: str = "sim-assistant") -> tuple[Tower, Planting]:
    seed_builtin_profiles(session)
    basil = session.scalar(select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic"))
    assert basil is not None
    tower = Tower(name="Assistant Tower", tower_key=tower_key)
    session.add(tower)
    session.commit()
    planting = Planting.from_profile(tower_id=tower.id, plant_profile=basil)
    session.add(planting)
    session.commit()
    return tower, planting


def test_responses_tool_call_loop_returns_application_data_to_model(session: Session) -> None:
    tower = Tower(name="Assistant Loop Tower", tower_key="sim-loop")
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
    client = FakeClient(
        [
            _function_response("get_latest_telemetry", f'{{"tower_id":{tower.id}}}'),
            _text_response("PPFD is not monitored in the latest simulated reading."),
        ]
    )

    result = run_assistant(session, "What is the latest telemetry?", client=client, model="test-model")

    assert result.message == "PPFD is not monitored in the latest simulated reading."
    assert result.tool_trace[0]["tool"] == "get_latest_telemetry"
    assert result.tool_trace[0]["result"]["latest_telemetry"]["sensors"]["ppfd_umol_m2_s"]["status"] == "not_monitored"
    second_messages = client.calls[1]["messages"]
    tool_output = next(item for item in second_messages if item.get("role") == "tool")
    assert "not_monitored" in tool_output["content"]
    first_tools = client.calls[0]["tools"]
    latest_tool = next(item for item in first_tools if item["function"]["name"] == "get_latest_telemetry")
    assert latest_tool["function"]["parameters"]["required"] == ["tower_id"]


def test_tool_exposure_is_scoped_to_exact_turn_action_and_recommendation() -> None:
    read_names = {tool["name"] for tool in assistant_tools(None)}
    assert not read_names.intersection({"approve_recommendation", "reject_recommendation", "execute_recommendation"})

    cases = [
        (TurnAuthorization("approve_recommendation", 7), "approve_recommendation", 7),
        (TurnAuthorization("reject_recommendation", 8), "reject_recommendation", 8),
        (TurnAuthorization("execute_recommendation", 9), "execute_recommendation", 9),
    ]
    for authorization, expected_tool, recommendation_id in cases:
        tools = assistant_tools(authorization)
        action_tools = [tool for tool in tools if tool["name"] in {"approve_recommendation", "reject_recommendation", "execute_recommendation"}]
        assert [tool["name"] for tool in action_tools] == [expected_tool]
        assert action_tools[0]["parameters"]["properties"]["recommendation_id"]["enum"] == [recommendation_id]


def test_informational_turn_rejects_model_generated_mutation(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched = []
    monkeypatch.setattr(assistant, "dispatch_tool", lambda *_args, **_kwargs: dispatched.append(True))
    client = FakeClient([_function_response("approve_recommendation", '{"recommendation_id":7}')])

    result = run_assistant(session, "Show recommendation 7 status.", client=client, model="test-model")

    assert dispatched == []
    assert result.tool_trace[0]["result"]["error"] == "No mutation is authorized for this assistant turn."
    assert result.message.startswith("Action not performed:")


def test_explicit_approve_allows_only_approved_recommendation(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched = []

    def fake_dispatch(_session, name, arguments):
        dispatched.append((name, arguments))
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "pending"}, "current_context": {}}
        if name == "approve_recommendation":
            return {
                "decision": {
                    "recommendation_id": 7,
                    "status": "approved",
                    "revalidation_passed": True,
                    "approval_is_execution": False,
                    "execution_created": False,
                }
            }
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient(
        [
            _function_response("get_recommendation_status", '{"recommendation_id":7}'),
            _function_response("approve_recommendation", '{"recommendation_id":7}'),
        ]
    )

    result = run_assistant(session, "Approve recommendation 7.", authorization=TurnAuthorization("approve_recommendation", 7), client=client, model="test-model")

    assert dispatched == [
        ("get_recommendation_status", {"recommendation_id": 7}),
        ("approve_recommendation", {"recommendation_id": 7}),
    ]
    assert result.message == "Recommendation 7 was approved after application revalidation. It has not been executed."


def test_explicit_approve_uses_only_application_authorized_action(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched = []

    def fake_dispatch(_session, name, arguments):
        dispatched.append((name, arguments))
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "pending"}, "current_context": {}}
        if name == "approve_recommendation":
            return {"decision": {"status": "approved", "revalidation_passed": True}}
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient([_function_response("execute_recommendation", '{"recommendation_id":7}')])

    result = run_assistant(
        session,
        "Approve recommendation 7.",
        authorization=TurnAuthorization("approve_recommendation", 7),
        client=client,
        model="test-model",
    )

    assert dispatched == [
        ("get_recommendation_status", {"recommendation_id": 7}),
        ("approve_recommendation", {"recommendation_id": 7}),
    ]
    assert client.calls == []
    assert "not been executed" in result.message


def test_explicit_reject_uses_only_application_authorized_action(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched = []

    def fake_dispatch(_session, name, arguments):
        dispatched.append((name, arguments))
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "pending"}, "current_context": {}}
        if name == "reject_recommendation":
            return {"decision": {"status": "rejected"}}
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient([_function_response("execute_recommendation", '{"recommendation_id":7}')])

    result = run_assistant(
        session,
        "Reject recommendation 7.",
        authorization=TurnAuthorization("reject_recommendation", 7),
        client=client,
        model="test-model",
    )

    assert dispatched == [
        ("get_recommendation_status", {"recommendation_id": 7}),
        ("reject_recommendation", {"recommendation_id": 7}),
    ]
    assert client.calls == []
    assert "rejected" in result.message.lower()


def test_application_authorization_pins_exact_recommendation_id(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched = []

    def fake_dispatch(_session, name, arguments):
        dispatched.append((name, arguments))
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "pending"}, "current_context": {}}
        if name == "approve_recommendation":
            return {"decision": {"status": "approved", "revalidation_passed": True}}
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient([_function_response("approve_recommendation", '{"recommendation_id":8}')])

    result = run_assistant(
        session,
        "Approve recommendation 8.",
        authorization=TurnAuthorization("approve_recommendation", 7),
        client=client,
        model="test-model",
    )

    assert dispatched == [
        ("get_recommendation_status", {"recommendation_id": 7}),
        ("approve_recommendation", {"recommendation_id": 7}),
    ]
    assert client.calls == []
    assert "Recommendation 7" in result.message
    assert "Recommendation 8" not in result.message


def test_successful_approve_result_cannot_create_chained_execute_authority(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched = []

    def fake_dispatch(_session, name, arguments):
        dispatched.append((name, arguments))
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "pending"}, "current_context": {}}
        if name == "approve_recommendation":
            return {"decision": {"status": "approved", "revalidation_passed": True}}
        if name == "execute_recommendation":
            raise AssertionError("execute must never be dispatched in an approve-only turn")
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient(
        [
            _function_response("get_recommendation_status", '{"recommendation_id":7}'),
            _function_response("approve_recommendation", '{"recommendation_id":7}'),
            _function_response("execute_recommendation", '{"recommendation_id":7}'),
        ]
    )

    result = run_assistant(session, "Approve recommendation 7.", authorization=TurnAuthorization("approve_recommendation", 7), client=client, model="test-model")

    assert dispatched == [
        ("get_recommendation_status", {"recommendation_id": 7}),
        ("approve_recommendation", {"recommendation_id": 7}),
    ]
    assert client.calls == []
    assert "not been executed" in result.message


def test_failed_mutation_result_is_grounded_as_failure(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_dispatch(_session, name, arguments):
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "rejected"}, "current_context": {}}
        if name == "approve_recommendation":
            return {"error": "Only pending Recommendations can be approved.", "recommendation_status": "rejected"}
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient(
        [
            _function_response("get_recommendation_status", '{"recommendation_id":7}'),
            _function_response("approve_recommendation", '{"recommendation_id":7}'),
            _text_response("Recommendation 7 was approved successfully."),
        ]
    )

    result = run_assistant(session, "Approve recommendation 7.", authorization=TurnAuthorization("approve_recommendation", 7), client=client, model="test-model")

    assert "failed" in result.message.lower()
    assert "Only pending Recommendations can be approved." in result.message
    assert "approved successfully" not in result.message
    assert client.calls == []


def test_command_sent_without_verification_is_reported_as_unverified(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_dispatch(_session, name, arguments):
        if name == "get_recommendation_status":
            return {"recommendation": {"recommendation_id": 7, "status": "approved"}, "current_context": {}}
        if name == "execute_recommendation":
            return {
                "execution": {
                    "recommendation_id": 7,
                    "recommendation_status": "failed",
                    "execution_status": "failed",
                    "command_sent": True,
                    "execution_verified": False,
                    "reason": "Command sent but no correlated acknowledgement was received.",
                    "simulated": True,
                }
            }
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient(
        [
            _function_response("get_recommendation_status", '{"recommendation_id":7}'),
            _function_response("execute_recommendation", '{"recommendation_id":7}'),
            _text_response("Execution was verified successfully."),
        ]
    )

    result = run_assistant(session, "Execute recommendation 7.", authorization=TurnAuthorization("execute_recommendation", 7), client=client, model="test-model")

    assert "command was sent" in result.message.lower()
    assert "not verified" in result.message.lower()
    assert "successful verified execution" in result.message.lower()
    assert client.calls == []


def test_unsupported_humidity_target_is_grounded_from_application_context(session: Session) -> None:
    tower, _planting = _start_basil(session, "sim-target-grounding")
    client = FakeClient(
        [
            _function_response("get_active_planting", f'{{"tower_id":{tower.id}}}'),
            _text_response("The configured humidity target is 60%."),
        ]
    )

    result = run_assistant(
        session,
        f"What is the configured humidity target for tower {tower.id}?",
        client=client,
        model="test-model",
    )

    assert "No configured/evidence-backed humidity target" in result.message
    assert "will not invent" in result.message
    assert "60%" not in result.message
    planting_context = result.tool_trace[0]["result"]["active_planting"]
    assert "humidity_pct" not in planting_context["configured_targets"]
    assert planting_context["configured_targets"]["ph"]["source_reference_id"] == "osu-hla-6722-table-2"


def test_explicit_action_does_not_trust_model_success_claim(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_dispatch(_session, name, arguments):
        if name == "get_recommendation_status":
            return {"error": "Recommendation not found.", "recommendation_id": 7}
        raise AssertionError(name)

    monkeypatch.setattr(assistant, "dispatch_tool", fake_dispatch)
    client = FakeClient([_text_response("Recommendation 7 was approved successfully.")])

    result = run_assistant(
        session,
        "Approve recommendation 7.",
        authorization=TurnAuthorization("approve_recommendation", 7),
        client=client,
        model="test-model",
    )

    assert result.message == "Action not performed: Recommendation not found."
    assert "approved successfully" not in result.message
    assert client.calls == []


def test_unreachable_ollama_returns_controlled_unavailable_error(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    class DownClient:
        def model_available(self, _model: str) -> bool:
            return False

    monkeypatch.setattr(assistant, "OllamaClient", lambda: DownClient())

    with pytest.raises(AssistantUnavailableError, match="local Ollama model"):
        run_assistant(session, "Hello")


def test_plain_chat_approve_request_requires_structured_authorization(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched = []
    monkeypatch.setattr(assistant, "dispatch_tool", lambda *_args, **_kwargs: dispatched.append(True))
    client = FakeClient([_text_response("Recommendation 288 is approved and ready for execution.")])

    result = run_assistant(
        session,
        "Approve Recommendation 288 now.",
        client=client,
        model="test-model",
    )

    assert dispatched == []
    assert client.calls == []
    assert result.tool_trace == []
    assert result.message.startswith("No action was performed.")
    assert "explicit action control" in result.message
    assert "approved and ready" not in result.message


def test_plain_chat_persian_execute_request_requires_structured_authorization(session: Session) -> None:
    client = FakeClient([_text_response("اجرا شد")])

    result = run_assistant(
        session,
        "ریکامندیشن ۲۸۸ رو اجرا کن",
        client=client,
        model="test-model",
    )

    assert client.calls == []
    assert result.message.startswith("No action was performed.")


def test_simulated_tool_data_is_disclosed_even_if_model_omits_it(session: Session) -> None:
    tower = Tower(name="Disclosure Tower", tower_key="sim-disclosure")
    session.add(tower)
    session.commit()
    session.add(
        TelemetryReading(
            tower_id=tower.id,
            recorded_at=datetime.now(timezone.utc),
            source="simulator",
            ph=5.8,
            circulation_on=True,
        )
    )
    session.commit()
    client = FakeClient(
        [
            _function_response("get_latest_telemetry", {"tower_id": tower.id}),
            _text_response("The latest pH is 5.8."),
        ]
    )

    result = run_assistant(
        session,
        f"What is the latest pH for tower {tower.id}?",
        client=client,
        model="test-model",
    )

    assert "The latest pH is 5.8." in result.message
    assert "simulated" in result.message.lower()


def test_user_facing_sanitizer_removes_invented_tower_name_alias() -> None:
    trace = [
        {
            "tool": "list_towers",
            "arguments": {},
            "result": {
                "towers": [
                    {"tower_id": 58, "tower_key": "sim-10", "name": "Demo Tower 10"},
                    {"tower_id": 59, "tower_key": "sim-11", "name": "01"},
                ]
            },
        },
        {
            "tool": "get_tower_status",
            "arguments": {"tower_id": 59},
            "result": {"tower": {"tower_id": 59, "tower_key": "sim-11", "name": "01"}},
        },
    ]

    sanitized = assistant._sanitize_user_facing_identifiers(
        "What is the current status of tower 01?",
        "**Tower 01 Status:**\n- **Name:** 01 (Demo Tower 11)\n- pH: 5.8",
        trace,
    )

    assert "Demo Tower 11" not in sanitized
    assert "**Name:** 01" in sanitized
    assert "sim-11" not in sanitized


def test_fix_intent_instruction_requires_read_only_recommendation_inspection() -> None:
    instructions = assistant.ASSISTANT_INSTRUCTIONS.lower()
    assert "fix, resolve, correct" in instructions
    assert "actionable" in instructions
    assert "read-only tools" in instructions
    assert "do not mutate" in instructions
