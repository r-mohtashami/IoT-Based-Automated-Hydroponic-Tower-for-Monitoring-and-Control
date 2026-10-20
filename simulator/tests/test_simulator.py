import json
import random
from datetime import datetime, timezone

import pytest

from simulator import (
    ACTUATOR_EVENT_TOPIC_TEMPLATE,
    Deviation,
    build_actuator_ack,
    generate_telemetry,
    parse_actuator_command,
    tower_keys,
)


def test_tower_count_is_configurable_without_ten_tower_limit():
    assert len(tower_keys(3)) == 3
    assert tower_keys(10)[-1] == "sim-10"
    assert tower_keys(12)[-1] == "sim-12"


def test_towers_generate_distinguishable_simulated_payloads():
    rng = random.Random(42)
    first = generate_telemetry("sim-1", 1, rng)
    second = generate_telemetry("sim-2", 2, rng)

    assert first["simulated"] is True
    assert second["simulated"] is True
    assert first["tower_key"] != second["tower_key"]
    assert first["ph"] != second["ph"]


def test_missing_sensor_is_omitted_not_replaced():
    payload = generate_telemetry(
        "sim-1",
        1,
        random.Random(42),
        missing_sensors={"ppfd_umol_m2_s"},
    )
    assert "ppfd_umol_m2_s" not in payload
    assert "ph" in payload


def test_controlled_deviation_changes_only_configured_factor():
    normal = generate_telemetry("sim-1", 1, random.Random(42))
    deviated = generate_telemetry(
        "sim-1",
        1,
        random.Random(42),
        deviation=Deviation(tower_key="sim-1", factor="ph", offset=1.25),
    )

    assert deviated["ph"] == round(normal["ph"] + 1.25, 3)
    assert deviated["ec_ms_cm"] == normal["ec_ms_cm"]


def test_simulator_accepts_only_correlated_review_indicator_command():
    command = {
        "simulated": True,
        "command_id": "cmd-1",
        "tower_key": "sim-1",
        "recommendation_id": 7,
        "actuator_type": "review_indicator",
        "command": "set_state",
        "state": "on",
    }
    parsed = parse_actuator_command(
        "hydroponics/towers/sim-1/commands/actuator",
        json.dumps(command),
        {"sim-1", "sim-2"},
    )
    assert parsed["command_id"] == "cmd-1"

    with pytest.raises(ValueError, match="tower_key"):
        parse_actuator_command(
            "hydroponics/towers/sim-1/commands/actuator",
            json.dumps({**command, "tower_key": "sim-2"}),
            {"sim-1", "sim-2"},
        )


def test_simulator_ack_preserves_command_correlation_and_marks_simulated():
    command = {
        "simulated": True,
        "command_id": "cmd-ack",
        "tower_key": "sim-2",
        "recommendation_id": 9,
        "actuator_type": "review_indicator",
        "command": "set_state",
        "state": "on",
    }
    ack = build_actuator_ack(command, datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc))

    assert ACTUATOR_EVENT_TOPIC_TEMPLATE.format(tower_key="sim-2") == "hydroponics/towers/sim-2/events/actuator"
    assert ack["command_id"] == "cmd-ack"
    assert ack["tower_key"] == "sim-2"
    assert ack["state"] == "on"
    assert ack["status"] == "executed"
    assert ack["simulated"] is True

from pathlib import Path
from simulator import (
    VirtualTowerState, advance_state, apply_actuator_command, inject_disturbance,
    fetch_backend_tower_keys, load_states, observe_state, save_states,
    sync_states_to_tower_keys,
)


def test_initial_state_variation_is_bounded_and_stable_for_many_towers():
    states = [VirtualTowerState.initial(f"sim-{index}", index) for index in range(1, 101)]

    assert all(6.08 <= state.ph <= 6.12 for state in states)
    assert all(1.48 <= state.ec_ms_cm <= 1.52 for state in states)
    assert all(20.94 <= state.water_temp_c <= 21.06 for state in states)
    assert all(22.92 <= state.air_temp_c <= 23.08 for state in states)
    assert all(59.84 <= state.humidity_pct <= 60.16 for state in states)
    assert all(81.76 <= state.water_level_pct <= 82.24 for state in states)

    # A Tower's initial physical state must not change just because its list
    # position changes after another Tower is added or removed.
    first = VirtualTowerState.initial("sim-11", 11)
    moved = VirtualTowerState.initial("sim-11", 999)
    assert first == moved
    assert first.ec_ms_cm == pytest.approx(1.48)


def test_ph_actuator_effect_is_gradual_and_stateful():
    state = VirtualTowerState.initial("sim-1", 1)
    start = state.ph
    apply_actuator_command(state, {
        "actuator_type": "ph_up_pump", "state": "completed"
    })
    advance_state(state)
    first = state.ph
    advance_state(state)
    second = state.ph
    assert first > start
    assert second > first


def test_ph_down_and_nutrient_effect_directions():
    ph = VirtualTowerState.initial("sim-1", 1)
    ec = VirtualTowerState.initial("sim-2", 2)
    start_ph, start_ec = ph.ph, ec.ec_ms_cm
    apply_actuator_command(ph, {"actuator_type": "ph_down_pump", "state": "completed"})
    apply_actuator_command(ec, {"actuator_type": "nutrient_dosing_pump", "state": "completed"})
    advance_state(ph); advance_state(ec)
    assert ph.ph < start_ph
    assert ec.ec_ms_cm > start_ec


def test_refill_light_and_circulation_change_virtual_hardware():
    state = VirtualTowerState.initial("sim-1", 1)
    state.water_level_pct = 40.0
    apply_actuator_command(state, {"actuator_type": "refill_inlet", "state": "completed"})
    advance_state(state)
    assert state.water_level_pct > 40.0
    rng = random.Random(1)
    apply_actuator_command(state, {"actuator_type": "grow_light", "state": "off"})
    for _ in range(8):
        advance_state(state, 5.0, datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    dark = observe_state(state, rng)["ppfd_umol_m2_s"]
    apply_actuator_command(state, {"actuator_type": "grow_light", "state": "on"})
    for _ in range(8):
        advance_state(state, 5.0, datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc))
    lit = observe_state(state, random.Random(1))["ppfd_umol_m2_s"]
    assert lit > dark + 250
    apply_actuator_command(state, {"actuator_type": "circulation_pump", "state": "off"})
    assert observe_state(state, random.Random(2))["circulation_on"] is False


def test_disturbance_changes_physical_state_not_only_observation():
    state = VirtualTowerState.initial("sim-1", 1)
    initial = state.ph
    inject_disturbance(state, Deviation("sim-1", "ph", 0.7))
    assert state.ph == pytest.approx(initial + 0.7)
    a = observe_state(state, random.Random(10))["ph"]
    b = observe_state(state, random.Random(11))["ph"]
    assert a != b
    assert abs(a - state.ph) < 0.03
    assert abs(b - state.ph) < 0.03


def test_persistent_state_restores_after_restart(tmp_path: Path):
    path = tmp_path / "state.json"
    states = {"sim-1": VirtualTowerState.initial("sim-1", 1)}
    states["sim-1"].ph = 6.77
    states["sim-1"].circulation_on = False
    save_states(str(path), states)
    restored = load_states(str(path), ["sim-1"])
    assert restored["sim-1"].ph == pytest.approx(6.77)
    assert restored["sim-1"].circulation_on is False


def test_tower_actuator_isolation():
    first = VirtualTowerState.initial("sim-1", 1)
    second = VirtualTowerState.initial("sim-2", 2)
    second_before = second.ph
    apply_actuator_command(first, {"actuator_type": "ph_down_pump", "state": "completed"})
    advance_state(first)
    advance_state(second)
    assert first.ph < 6.1
    assert second.ph == pytest.approx(second_before)


def test_environmental_factors_change_smoothly_per_tick():
    state = VirtualTowerState.initial("sim-1", 1)
    state.water_temp_c = 25.0
    state.air_temp_c = 30.0
    state.humidity_pct = 75.0
    state.water_level_pct = 80.0
    before = (state.water_temp_c, state.air_temp_c, state.humidity_pct, state.water_level_pct)
    advance_state(state, 5.0, datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    after = (state.water_temp_c, state.air_temp_c, state.humidity_pct, state.water_level_pct)
    assert 0 < before[0] - after[0] < 0.01
    assert 0 < before[1] - after[1] < 0.01
    assert 0 < before[2] - after[2] < 0.01
    assert 0 < before[3] - after[3] < 0.01


def test_ambient_light_and_grow_light_transition_are_gradual():
    state = VirtualTowerState.initial("sim-1", 1)
    state.ambient_ppfd = 20.0
    noon = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    before_ambient = state.ambient_ppfd
    advance_state(state, 5.0, noon)
    assert 0 < state.ambient_ppfd - before_ambient <= 1.0

    apply_actuator_command(state, {"actuator_type": "grow_light", "state": "off"})
    before_lamp = state.grow_light_output_ppfd
    advance_state(state, 5.0, noon)
    assert 0 < before_lamp - state.grow_light_output_ppfd < before_lamp
    assert state.grow_light_output_ppfd > 0


def test_actuator_effect_uses_elapsed_time_not_telemetry_tick_count():
    fast = VirtualTowerState.initial("sim-1", 1)
    slow = VirtualTowerState.initial("sim-2", 2)
    slow.ph = fast.ph
    apply_actuator_command(fast, {"actuator_type": "ph_up_pump", "state": "completed"})
    apply_actuator_command(slow, {"actuator_type": "ph_up_pump", "state": "completed"})
    for _ in range(12):
        advance_state(fast, 5.0)
    for _ in range(6):
        advance_state(slow, 10.0)
    assert fast.ph == pytest.approx(slow.ph, abs=1e-9)


def test_dynamic_tower_sync_adds_and_removes_virtual_state():
    states = {
        "sim-1": VirtualTowerState.initial("sim-1", 1),
        "sim-2": VirtualTowerState.initial("sim-2", 2),
    }
    original_ph = states["sim-1"].ph
    states["sim-1"].ph = 6.77

    added, removed = sync_states_to_tower_keys(states, ["sim-1", "sim-3"])

    assert added == ["sim-3"]
    assert removed == ["sim-2"]
    assert set(states) == {"sim-1", "sim-3"}
    assert states["sim-1"].ph == pytest.approx(6.77)
    assert states["sim-3"].tower_key == "sim-3"
    assert 6.08 <= states["sim-3"].ph <= 6.12
    assert 1.48 <= states["sim-3"].ec_ms_cm <= 1.52


def test_backend_tower_discovery_filters_missing_and_duplicate_keys(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps([
                {"tower_key": "sim-1"},
                {"tower_key": None},
                {"tower_key": "sim-2"},
                {"tower_key": "sim-1"},
            ]).encode("utf-8")

    monkeypatch.setattr("simulator.urlopen", lambda *_args, **_kwargs: FakeResponse())
    assert fetch_backend_tower_keys("http://backend:8000") == ["sim-1", "sim-2"]


def test_explicit_simulator_state_reset_removes_only_requested_file(tmp_path: Path):
    from reset_state import reset_state

    state_file = tmp_path / "state.json"
    other_file = tmp_path / "keep.txt"
    state_file.write_text("{}")
    other_file.write_text("keep")

    assert reset_state(str(state_file)) is True
    assert not state_file.exists()
    assert other_file.read_text() == "keep"
    assert reset_state(str(state_file)) is False
