"""Stateful virtual hydroponic hardware simulator.

All numeric dynamics, sensor-noise envelopes, drift coefficients and actuator-effect
coefficients in this module are SIMULATED / ASSUMED APPLICATION CONFIGURATION.
They are not agronomic evidence, real dosing guidance, or physical-device safety rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import sin, pi
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

SENSOR_FIELDS = (
    "ph", "ec_ms_cm", "water_temp_c", "air_temp_c", "humidity_pct",
    "water_level_pct", "ppfd_umol_m2_s",
)

# SIMULATED / ASSUMED APPLICATION CONFIGURATION.
INITIAL_VALUES = {
    "ph": 6.1,
    "ec_ms_cm": 1.5,
    "water_temp_c": 21.0,
    "air_temp_c": 23.0,
    "humidity_pct": 60.0,
    "water_level_pct": 82.0,
}
SENSOR_NOISE = {
    "ph": 0.015,
    "ec_ms_cm": 0.012,
    "water_temp_c": 0.08,
    "air_temp_c": 0.10,
    "humidity_pct": 0.25,
    "water_level_pct": 0.12,
    "ppfd_umol_m2_s": 2.5,
}
# Temporal dynamics below are deliberately simple and are NOT real-world dosing guidance.
# They are chosen to preserve continuity and make simulator behavior easy to explain.
DEFAULT_TICK_SECONDS = 5.0
EFFECT_DURATION_SECONDS = 60.0
ACTUATOR_TOTAL_EFFECT = {
    "ph_up_pump": ("ph", +0.32),
    "ph_down_pump": ("ph", -0.32),
    "nutrient_dosing_pump": ("ec_ms_cm", +0.24),
    "refill_inlet": ("water_level_pct", +12.0),
}
WATER_USE_PCT_PER_HOUR = 0.15
WATER_TEMP_APPROACH_C_PER_HOUR = 0.30
AIR_TEMP_APPROACH_C_PER_HOUR = 0.60
HUMIDITY_APPROACH_PCT_PER_HOUR = 3.0
AMBIENT_PPFD_NIGHT = 2.0
AMBIENT_PPFD_DAY_PEAK = 120.0
AMBIENT_PPFD_CHANGE_PER_MINUTE = 10.0
GROW_LIGHT_PPFD = 300.0
GROW_LIGHT_RAMP_PER_SECOND = 8.0

ACTUATOR_COMMAND_TOPIC_TEMPLATE = "hydroponics/towers/{tower_key}/commands/actuator"
ACTUATOR_EVENT_TOPIC_TEMPLATE = "hydroponics/towers/{tower_key}/events/actuator"
ALLOWED_ACTUATORS = {
    "ph_up_pump", "ph_down_pump", "nutrient_dosing_pump",
    "refill_inlet", "circulation_pump", "grow_light", "review_indicator",
}


@dataclass(frozen=True)
class Deviation:
    tower_key: str
    factor: str
    offset: float


@dataclass
class VirtualTowerState:
    tower_key: str
    ph: float
    ec_ms_cm: float
    water_temp_c: float
    air_temp_c: float
    humidity_pct: float
    water_level_pct: float
    ambient_ppfd: float = 28.0
    grow_light_output_ppfd: float = GROW_LIGHT_PPFD
    circulation_on: bool = True
    grow_light_on: bool = True
    transient_effects: dict[str, float] = field(default_factory=dict)
    updated_at: str | None = None

    @classmethod
    def initial(cls, tower_key: str, tower_index: int) -> "VirtualTowerState":
        # Initial variation must stay bounded regardless of how many Towers exist.
        # The old linear-by-position offset put later Towers directly on or beyond
        # crop boundaries (for example sim-11 started at EC 1.600), so ordinary
        # sensor noise could create repeated resolve/reopen cycles.  Use the stable
        # tower key only to choose a small deterministic synthetic variant.  This is
        # simulator configuration, not an agronomic target or safety threshold.
        del tower_index  # kept in the signature for compatibility with callers/tests
        variant = (hashlib.sha256(tower_key.encode("utf-8")).digest()[0] % 5) - 2
        return cls(
            tower_key=tower_key,
            ph=INITIAL_VALUES["ph"] + 0.01 * variant,
            ec_ms_cm=INITIAL_VALUES["ec_ms_cm"] + 0.01 * variant,
            water_temp_c=INITIAL_VALUES["water_temp_c"] + 0.03 * variant,
            air_temp_c=INITIAL_VALUES["air_temp_c"] + 0.04 * variant,
            humidity_pct=INITIAL_VALUES["humidity_pct"] + 0.08 * variant,
            water_level_pct=INITIAL_VALUES["water_level_pct"] - 0.12 * variant,
        )


@dataclass(frozen=True)
class SimulatorConfig:
    tower_count: int
    interval_seconds: float
    mqtt_host: str
    mqtt_port: int
    random_seed: int
    missing_sensors: dict[str, set[str]]
    deviation: Deviation | None
    run_once: bool
    state_path: str
    discover_towers_from_backend: bool
    backend_base_url: str
    tower_refresh_seconds: float

    @classmethod
    def from_env(cls) -> "SimulatorConfig":
        tower_count = int(os.getenv("SIMULATED_TOWER_COUNT", "3"))
        interval_seconds = float(os.getenv("SIM_TELEMETRY_INTERVAL_SECONDS", "5"))
        if tower_count <= 0:
            raise ValueError("SIMULATED_TOWER_COUNT must be greater than zero.")
        if interval_seconds <= 0:
            raise ValueError("SIM_TELEMETRY_INTERVAL_SECONDS must be greater than zero.")
        tower_refresh_seconds = float(os.getenv("SIM_TOWER_REFRESH_SECONDS", "2"))
        if tower_refresh_seconds <= 0:
            raise ValueError("SIM_TOWER_REFRESH_SECONDS must be greater than zero.")
        return cls(
            tower_count=tower_count,
            interval_seconds=interval_seconds,
            mqtt_host=os.getenv("MQTT_HOST", "mosquitto"),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
            random_seed=int(os.getenv("SIM_RANDOM_SEED", "42")),
            missing_sensors=parse_missing_sensors(os.getenv("SIM_MISSING_SENSORS", "")),
            deviation=parse_deviation(),
            run_once=os.getenv("SIM_RUN_ONCE", "false").strip().lower() in {"1", "true", "yes", "on"},
            state_path=os.getenv("SIM_STATE_PATH", "/simulator-state/state.json"),
            discover_towers_from_backend=os.getenv("SIM_DISCOVER_TOWERS_FROM_BACKEND", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            backend_base_url=os.getenv("BACKEND_BASE_URL", "http://backend:8000").rstrip("/"),
            tower_refresh_seconds=tower_refresh_seconds,
        )


def tower_keys(count: int) -> list[str]:
    if count <= 0:
        raise ValueError("Tower count must be greater than zero.")
    return [f"sim-{index}" for index in range(1, count + 1)]


def fetch_backend_tower_keys(base_url: str, timeout_seconds: float = 2.0) -> list[str]:
    """Return application-managed simulated tower keys from the backend.

    The backend owns logical Tower membership; the simulator still owns the virtual
    physical/device state for each returned key. This keeps Add/Remove Tower usable
    without requiring a manual simulator-count restart.
    """
    try:
        with urlopen(f"{base_url.rstrip('/')}/towers", timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not discover towers from backend: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("Backend tower discovery response must be a list.")
    result: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        key = item.get("tower_key")
        if isinstance(key, str) and key.strip() and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _tower_index_from_key(tower_key: str, fallback: int) -> int:
    # Dynamic UI-created keys may contain a database-derived suffix that is much
    # larger than the number of active virtual devices. Use the active-list position
    # for initial-value variation so late-created Towers still start in the same
    # plausible synthetic envelope.
    return max(1, fallback)


def sync_states_to_tower_keys(
    states: dict[str, VirtualTowerState],
    keys: list[str],
) -> tuple[list[str], list[str]]:
    """Add new virtual towers and forget removed ones while preserving active state."""
    desired = set(keys)
    removed = [key for key in list(states) if key not in desired]
    for key in removed:
        states.pop(key, None)
    added: list[str] = []
    for position, key in enumerate(keys, start=1):
        if key not in states:
            states[key] = VirtualTowerState.initial(
                key, _tower_index_from_key(key, position)
            )
            added.append(key)
    return added, removed


def parse_missing_sensors(raw: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if not raw.strip():
        return result
    for item in raw.split(";"):
        tower_key, separator, factors_raw = item.partition(":")
        if not separator or not tower_key.strip() or not factors_raw.strip():
            raise ValueError("SIM_MISSING_SENSORS must use tower:factor,factor syntax.")
        factors = {factor.strip() for factor in factors_raw.split(",") if factor.strip()}
        unknown = factors.difference(SENSOR_FIELDS)
        if unknown:
            raise ValueError(f"Unknown missing sensor factor(s): {', '.join(sorted(unknown))}")
        result[tower_key.strip()] = factors
    return result


def parse_deviation() -> Deviation | None:
    tower_key = os.getenv("SIM_DEVIATION_TOWER", "").strip()
    factor = os.getenv("SIM_DEVIATION_FACTOR", "").strip()
    offset = os.getenv("SIM_DEVIATION_OFFSET", "").strip()
    if not any((tower_key, factor, offset)):
        return None
    if not all((tower_key, factor, offset)):
        raise ValueError("SIM_DEVIATION_TOWER, SIM_DEVIATION_FACTOR and SIM_DEVIATION_OFFSET must be set together.")
    if factor not in SENSOR_FIELDS:
        raise ValueError(f"SIM_DEVIATION_FACTOR must be one of: {', '.join(SENSOR_FIELDS)}")
    return Deviation(tower_key=tower_key, factor=factor, offset=float(offset))


def load_states(path: str, keys: list[str]) -> dict[str, VirtualTowerState]:
    """Restore simulator-owned physical/device state across technical restarts."""
    saved: dict[str, object] = {}
    file_path = Path(path)
    if file_path.exists():
        try:
            raw = json.loads(file_path.read_text())
            if isinstance(raw, dict):
                saved = raw
        except (OSError, json.JSONDecodeError):
            saved = {}
    states: dict[str, VirtualTowerState] = {}
    for index, key in enumerate(keys, start=1):
        data = saved.get(key)
        if isinstance(data, dict):
            try:
                states[key] = VirtualTowerState(**data)
                continue
            except TypeError:
                pass
        states[key] = VirtualTowerState.initial(key, index)
    return states


def save_states(path: str, states: dict[str, VirtualTowerState]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({key: asdict(value) for key, value in states.items()}, sort_keys=True))
    tmp.replace(file_path)


def inject_disturbance(state: VirtualTowerState, deviation: Deviation | None) -> None:
    """Apply an explicit scenario disturbance to physical state, not serialized sensor output."""
    if deviation is None or deviation.tower_key != state.tower_key:
        return
    if deviation.factor == "ppfd_umol_m2_s":
        state.ambient_ppfd = max(0.0, state.ambient_ppfd + deviation.offset)
    elif hasattr(state, deviation.factor):
        current = getattr(state, deviation.factor)
        setattr(state, deviation.factor, float(current) + deviation.offset)


def apply_actuator_command(state: VirtualTowerState, command: dict[str, object]) -> None:
    actuator = str(command["actuator_type"])
    if actuator in ACTUATOR_TOTAL_EFFECT:
        state.transient_effects[actuator] = EFFECT_DURATION_SECONDS
    elif actuator == "circulation_pump":
        state.circulation_on = str(command["state"]) == "on"
    elif actuator == "grow_light":
        state.grow_light_on = str(command["state"]) == "on"
    state.updated_at = datetime.now(timezone.utc).isoformat()


def _move_toward(current: float, target: float, max_delta: float) -> float:
    if current < target:
        return min(target, current + max_delta)
    if current > target:
        return max(target, current - max_delta)
    return current


def daylight_target_ppfd(at: datetime) -> float:
    """Simple smooth 06:00-18:00 synthetic daylight envelope.

    Clock shape is SIMULATED / ASSUMED APPLICATION CONFIGURATION, not a
    location-specific solar model. It exists to prevent implausible random jumps.
    """
    local_hour = at.hour + at.minute / 60 + at.second / 3600
    if local_hour <= 6.0 or local_hour >= 18.0:
        return AMBIENT_PPFD_NIGHT
    daylight_progress = (local_hour - 6.0) / 12.0
    return AMBIENT_PPFD_NIGHT + AMBIENT_PPFD_DAY_PEAK * sin(pi * daylight_progress)


def advance_state(
    state: VirtualTowerState,
    elapsed_seconds: float = DEFAULT_TICK_SECONDS,
    now: datetime | None = None,
) -> None:
    """Advance one virtual tick while preserving physically plausible continuity.

    All rates are SIMULATED / ASSUMED APPLICATION CONFIGURATION.
    """
    seconds = max(0.0, float(elapsed_seconds))
    hours = seconds / 3600.0

    # Slow natural evolution. These factors must not jump independently each tick.
    state.water_level_pct -= WATER_USE_PCT_PER_HOUR * hours
    state.water_temp_c = _move_toward(
        state.water_temp_c, 21.0, WATER_TEMP_APPROACH_C_PER_HOUR * hours
    )
    state.air_temp_c = _move_toward(
        state.air_temp_c, 23.0, AIR_TEMP_APPROACH_C_PER_HOUR * hours
    )
    state.humidity_pct = _move_toward(
        state.humidity_pct, 60.0, HUMIDITY_APPROACH_PCT_PER_HOUR * hours
    )

    # Ambient light follows a smooth daily envelope. Grow-light output ramps rather
    # than appearing/disappearing as an arbitrary sensor-value jump.
    timestamp = now or datetime.now(timezone.utc)
    ambient_target = daylight_target_ppfd(timestamp)
    ambient_step = AMBIENT_PPFD_CHANGE_PER_MINUTE * (seconds / 60.0)
    state.ambient_ppfd = _move_toward(state.ambient_ppfd, ambient_target, ambient_step)
    lamp_target = GROW_LIGHT_PPFD if state.grow_light_on else 0.0
    state.grow_light_output_ppfd = _move_toward(
        state.grow_light_output_ppfd, lamp_target, GROW_LIGHT_RAMP_PER_SECOND * seconds
    )

    finished: list[str] = []
    for actuator, remaining_seconds in list(state.transient_effects.items()):
        factor, total_delta = ACTUATOR_TOTAL_EFFECT[actuator]
        active_seconds = min(seconds, max(0.0, float(remaining_seconds)))
        delta = total_delta * (active_seconds / EFFECT_DURATION_SECONDS)
        setattr(state, factor, float(getattr(state, factor)) + delta)
        next_remaining = float(remaining_seconds) - active_seconds
        if next_remaining <= 0:
            finished.append(actuator)
        else:
            state.transient_effects[actuator] = next_remaining
    for actuator in finished:
        state.transient_effects.pop(actuator, None)

    state.ph = min(14.0, max(0.0, state.ph))
    state.ec_ms_cm = max(0.0, state.ec_ms_cm)
    state.water_level_pct = min(100.0, max(0.0, state.water_level_pct))
    state.humidity_pct = min(100.0, max(0.0, state.humidity_pct))
    state.ambient_ppfd = max(0.0, state.ambient_ppfd)
    state.grow_light_output_ppfd = max(0.0, state.grow_light_output_ppfd)
    state.updated_at = datetime.now(timezone.utc).isoformat()


def observe_state(
    state: VirtualTowerState,
    rng: random.Random,
    missing_sensors: set[str] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, object]:
    missing = missing_sensors or set()
    ppfd = state.ambient_ppfd + state.grow_light_output_ppfd
    values = {
        "ph": state.ph,
        "ec_ms_cm": state.ec_ms_cm,
        "water_temp_c": state.water_temp_c,
        "air_temp_c": state.air_temp_c,
        "humidity_pct": state.humidity_pct,
        "water_level_pct": state.water_level_pct,
        "ppfd_umol_m2_s": ppfd,
    }
    payload: dict[str, object] = {
        "simulated": True,
        "tower_key": state.tower_key,
        "timestamp": (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "circulation_on": state.circulation_on,
    }
    for factor, value in values.items():
        if factor not in missing:
            payload[factor] = round(float(value) + rng.uniform(-SENSOR_NOISE[factor], SENSOR_NOISE[factor]), 3)
    return payload


def generate_telemetry(
    tower_key: str,
    tower_index: int,
    rng: random.Random,
    missing_sensors: set[str] | None = None,
    deviation: Deviation | None = None,
) -> dict[str, object]:
    """Compatibility helper: create one state, apply scenario once, then observe it."""
    state = VirtualTowerState.initial(tower_key, tower_index)
    inject_disturbance(state, deviation)
    return observe_state(state, rng, missing_sensors)


def parse_actuator_command(topic: str, payload: bytes | str, known_towers: set[str]) -> dict[str, object]:
    parts = topic.split("/")
    if len(parts) != 5 or parts[:2] != ["hydroponics", "towers"] or parts[3:] != ["commands", "actuator"]:
        raise ValueError("Unexpected actuator command topic.")
    topic_tower_key = parts[2]
    if topic_tower_key not in known_towers:
        raise ValueError("Actuator command targets an unknown simulated tower.")
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("Actuator command payload must be valid JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("Actuator command payload must be an object.")
    if data.get("simulated") is not True or data.get("tower_key") != topic_tower_key:
        raise ValueError("Invalid actuator command tower_key/simulated field.")
    if data.get("actuator_type") not in ALLOWED_ACTUATORS:
        raise ValueError("Unsupported simulated actuator_type.")
    if data.get("command") not in {"apply_step", "set_state"}:
        raise ValueError("Unsupported simulated actuator command.")
    if data.get("state") not in {"on", "off", "completed"}:
        raise ValueError("Invalid actuator state.")
    command_id = data.get("command_id")
    recommendation_id = data.get("recommendation_id")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("Actuator command_id is required.")
    if not isinstance(recommendation_id, int) or recommendation_id <= 0:
        raise ValueError("Actuator recommendation_id must be a positive integer.")
    return data


def build_actuator_ack(command: dict[str, object], timestamp: datetime | None = None) -> dict[str, object]:
    observed_at = timestamp or datetime.now(timezone.utc)
    return {
        "simulated": True,
        "tower_key": command["tower_key"],
        "command_id": command["command_id"],
        "recommendation_id": command["recommendation_id"],
        "actuator_type": command["actuator_type"],
        "state": command["state"],
        "status": "executed",
        "timestamp": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
