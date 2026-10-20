from pathlib import Path

from api_client import BackendClient


def test_read_only_assistant_request_has_no_mutation_authority() -> None:
    client = BackendClient("http://example.test")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"message": "ok", "model": "test", "tool_trace": []}

    client._request = fake_request  # type: ignore[method-assign]
    client.assistant_chat("Show tower status")

    assert captured["path"] == "/assistant/chat"
    assert captured["json"] == {"message": "Show tower status"}


def test_ai_mutation_request_contains_exact_action_and_recommendation() -> None:
    client = BackendClient("http://example.test")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"message": "approved", "model": "test", "tool_trace": []}

    client._request = fake_request  # type: ignore[method-assign]
    client.assistant_chat("Approve this recommendation", action="approve", recommendation_id=17)

    assert captured["json"] == {
        "message": "Approve this recommendation",
        "action_request": {"action": "approve", "recommendation_id": 17},
    }


def test_streamlit_client_has_no_direct_database_or_mqtt_dependency() -> None:
    source = (Path(__file__).parents[1] / "api_client.py").read_text()
    assert "sqlalchemy" not in source.lower()
    assert "psycopg" not in source.lower()
    assert "paho" not in source.lower()
    assert "mqtt" not in source.lower()


def test_authoritative_recommendation_state_refreshes_recommendation_and_history() -> None:
    client = BackendClient("http://example.test")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/recommendations"):
            return [
                {"id": 21, "status": "approved", "executed_at": None},
                {"id": 22, "status": "rejected", "executed_at": None},
            ]
        if path.endswith("/actions"):
            return [{"recommendation_id": 21, "event_type": "recommendation_approved", "details": {}}]
        raise AssertionError(path)

    client._request = fake_request  # type: ignore[method-assign]
    recommendation, history = client.authoritative_recommendation_state(4, 21)

    assert recommendation["status"] == "approved"
    assert history[0]["event_type"] == "recommendation_approved"
    assert [path for _method, path, _kwargs in calls] == [
        "/towers/4/recommendations",
        "/towers/4/actions",
    ]


def test_authoritative_recommendation_state_rejects_missing_backend_state() -> None:
    from api_client import ApiError

    client = BackendClient("http://example.test")
    client.list_recommendations = lambda tower_id, limit=100: []  # type: ignore[method-assign]

    try:
        client.authoritative_recommendation_state(4, 999)
    except ApiError as exc:
        assert "no longer available" in str(exc)
    else:
        raise AssertionError("Expected ApiError")


def test_streamlit_mutation_paths_refresh_authoritative_backend_state() -> None:
    source = (Path(__file__).parents[1] / "app.py").read_text()
    assert source.count("authoritative_recommendation_state") >= 3


def test_create_tower_uses_rest_and_keeps_internal_mapping_out_of_client_defaults() -> None:
    client = BackendClient("http://example.test")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": 7, "name": "Kitchen Tower", "tower_key": "sim-7"}

    client._request = fake_request  # type: ignore[method-assign]
    client.create_tower("Kitchen Tower", tower_key="sim-7")

    assert captured["method"] == "POST"
    assert captured["path"] == "/towers"
    assert captured["json"] == {"name": "Kitchen Tower", "tower_key": "sim-7"}


def test_actuator_states_are_read_from_backend_rest() -> None:
    client = BackendClient("http://example.test")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return [{"actuator_type": "review_indicator", "state": "on"}]

    client._request = fake_request  # type: ignore[method-assign]
    result = client.actuator_states(3)
    assert result[0]["state"] == "on"
    assert captured["method"] == "GET"
    assert captured["path"] == "/towers/3/actuator-states"


def test_update_and_delete_tower_use_backend_rest() -> None:
    client = BackendClient("http://example.test")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "PATCH":
            return {"id": 4, "name": "Renamed"}
        return None

    client._request = fake_request  # type: ignore[method-assign]
    updated = client.update_tower(4, name="Renamed")
    client.delete_tower(4)

    assert updated["name"] == "Renamed"
    assert calls == [
        ("PATCH", "/towers/4", {"json": {"name": "Renamed"}}),
        ("DELETE", "/towers/4", {}),
    ]


def test_assistant_chat_uses_dedicated_long_timeout() -> None:
    client = BackendClient(
        "http://example.test",
        timeout_seconds=8.0,
        assistant_timeout_seconds=120.0,
    )
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"message": "ok", "model": "test", "tool_trace": []}

    client._request = fake_request  # type: ignore[method-assign]
    client.assistant_chat("Show tower status")

    assert captured["path"] == "/assistant/chat"
    assert captured["timeout_seconds"] == 120.0


def test_normal_backend_calls_keep_short_default_timeout() -> None:
    client = BackendClient("http://example.test", timeout_seconds=8.0, assistant_timeout_seconds=120.0)
    assert client.timeout_seconds == 8.0
    assert client.assistant_timeout_seconds == 120.0
