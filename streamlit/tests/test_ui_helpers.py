from datetime import datetime, timedelta, timezone

import pytest

from ui_helpers import (
    assistant_action_candidates,
    chart_domain,
    chart_points,
    execution_evidence,
    execution_feedback,
    parse_target_range,
    profile_origin_label,
    profile_type_label,
    sensor_number_and_unit,
    sensor_text,
    target_text,
    telemetry_is_stale,
)


def test_missing_sensor_is_not_monitored_and_never_zero_filled() -> None:
    assert sensor_text(None, "µmol/m²/s") == "Not monitored"


def test_number_and_unit_are_separate_for_responsive_cards() -> None:
    assert sensor_number_and_unit(1.526, "mS/cm") == ("1.526", "mS/cm")


def test_chart_points_skip_missing_values_instead_of_fabricating_data() -> None:
    history = [
        {"timestamp": "2026-09-01T10:00:00Z", "ph": 5.8},
        {"timestamp": "2026-09-01T10:01:00Z", "ph": None},
        {"timestamp": "2026-09-01T10:02:00Z", "ph": 5.9},
    ]
    assert chart_points(history, "ph") == [
        {"timestamp": "2026-09-01T10:00:00Z", "value": 5.8},
        {"timestamp": "2026-09-01T10:02:00Z", "value": 5.9},
    ]


def test_missing_target_uses_plain_language() -> None:
    assert target_text(None) == "No plant target"


def test_execution_evidence_keeps_command_sent_separate_from_verification() -> None:
    history = [
        {
            "recommendation_id": 8,
            "event_type": "actuator_command_sent",
            "details": {"simulated": True},
        }
    ]
    evidence = execution_evidence(history, 8)
    assert evidence["command_sent"] is True
    assert evidence["execution_verified"] is False
    assert evidence["label"] == "Command sent — verification not confirmed"


def test_verified_simulated_execution_requires_verification_event() -> None:
    history = [
        {
            "recommendation_id": 8,
            "event_type": "actuator_command_sent",
            "details": {"simulated": True},
        },
        {
            "recommendation_id": 8,
            "event_type": "execution_verified",
            "details": {"simulated": True, "verification_source": "simulator_ack"},
        },
    ]
    evidence = execution_evidence(history, 8)
    assert evidence["execution_verified"] is True
    assert evidence["label"] == "Actuator acknowledged — awaiting newer telemetry"


def test_profile_types_are_user_facing() -> None:
    assert profile_type_label("system_builtin") == "Evidence-backed"
    assert profile_type_label("user_customized") == "Customized plant"


def test_customized_profile_origin_names_source_when_available() -> None:
    profiles = [
        {"id": 7, "name": "Basil — Hydroponic", "profile_type": "system_builtin"},
        {"id": 12, "name": "My Basil", "profile_type": "user_customized", "source_profile_id": 7},
    ]
    assert profile_origin_label(profiles[1], profiles) == "Based on Basil · Settings changed by you"


def test_sent_but_unverified_failure_never_renders_as_verified() -> None:
    history = [
        {
            "recommendation_id": 9,
            "event_type": "actuator_command_sent",
            "details": {"simulated": True},
        },
        {
            "recommendation_id": 9,
            "event_type": "execution_failed",
            "details": {"command_sent": True, "execution_verified": False},
        },
    ]
    evidence = execution_evidence(history, 9)
    kind, message = execution_feedback(evidence)
    assert evidence["execution_verified"] is False
    assert kind == "warning"
    assert "verified" not in message.lower() or "unverified" in message.lower()
    assert "failed" in message.lower() or "unverified" in message.lower()


def test_verified_feedback_requires_execution_verified_event() -> None:
    history = [
        {
            "recommendation_id": 10,
            "event_type": "execution_verified",
            "details": {"simulated": True, "verification_source": "simulator_ack"},
        }
    ]
    kind, message = execution_feedback(execution_evidence(history, 10))
    assert kind == "warning"
    assert message == "Actuator acknowledged — awaiting newer telemetry"


def test_stale_telemetry_is_not_treated_as_current() -> None:
    now = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    assert telemetry_is_stale("2026-10-07T07:59:00Z", now=now, stale_after_seconds=120) is False
    assert telemetry_is_stale("2026-10-07T07:57:00Z", now=now, stale_after_seconds=120) is True
    assert telemetry_is_stale(None, now=now, stale_after_seconds=120) is True


def test_future_telemetry_beyond_clock_skew_is_not_treated_as_current() -> None:
    now = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    assert telemetry_is_stale(
        (now + timedelta(seconds=4)).isoformat(), now=now, max_future_clock_skew_seconds=5
    ) is False
    assert telemetry_is_stale(
        (now + timedelta(seconds=8)).isoformat(), now=now, max_future_clock_skew_seconds=5
    ) is True


def test_chart_domain_zooms_to_data_and_target_instead_of_forcing_zero() -> None:
    low, high = chart_domain([5.76, 5.81, 5.79], {"min": 5.5, "max": 6.0}, minimum_span=0.6)
    assert low > 5.0
    assert high < 6.5
    assert low <= 5.5
    assert high >= 6.0


def test_custom_target_range_validation() -> None:
    assert parse_target_range("5.5", "6.0", unit="pH") == {"min": 5.5, "max": 6.0, "unit": "pH"}
    with pytest.raises(ValueError, match="Minimum cannot"):
        parse_target_range("6.2", "5.8", unit="pH")
    with pytest.raises(ValueError, match="finite"):
        parse_target_range("nan", "6.0", unit="pH")


def test_assistant_action_candidates_expose_pending_and_then_approved_controls() -> None:
    pending_trace = [
        {
            "tool": "get_recommendations",
            "result": {
                "recommendations": [
                    {
                        "recommendation_id": 41,
                        "status": "pending",
                        "action_parameters": {"executable": True},
                    },
                    {
                        "recommendation_id": 42,
                        "status": "rejected",
                        "action_parameters": {"executable": True},
                    },
                    {
                        "recommendation_id": 43,
                        "status": "pending",
                        "action_parameters": {"executable": False},
                    },
                ]
            },
        }
    ]
    assert assistant_action_candidates(pending_trace) == [
        {"recommendation_id": 41, "status": "pending", "actions": ["approve", "reject"]}
    ]

    approved_trace = [
        {
            "tool": "get_recommendation_status",
            "result": {
                "recommendation": {
                    "recommendation_id": 41,
                    "status": "pending",
                    "action_parameters": {"executable": True},
                }
            },
        },
        {
            "tool": "approve_recommendation",
            "result": {"decision": {"recommendation_id": 41, "status": "approved"}},
        },
    ]
    assert assistant_action_candidates(approved_trace) == [
        {"recommendation_id": 41, "status": "approved", "actions": ["execute"]}
    ]


def test_assistant_action_candidates_clear_after_execution() -> None:
    trace = [
        {
            "tool": "get_recommendation_status",
            "result": {
                "recommendation": {
                    "recommendation_id": 44,
                    "status": "approved",
                    "action_parameters": {"executable": True},
                }
            },
        },
        {
            "tool": "execute_recommendation",
            "result": {"execution": {"recommendation_id": 44, "recommendation_status": "executed"}},
        },
    ]
    assert assistant_action_candidates(trace) == []
