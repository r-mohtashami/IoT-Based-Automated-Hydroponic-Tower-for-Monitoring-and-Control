"""In-process closed-loop integration across backend command contract and simulator hardware model.

This test deliberately uses the production payload/ACK parsers but no network broker. Docker/MQTT
transport is validated separately by the existing transport unit tests and must still be run locally.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

SIM_PATH = Path(__file__).resolve().parents[2] / "simulator"
sys.path.append(str(SIM_PATH))

from simulator import (  # noqa: E402
    ACTUATOR_COMMAND_TOPIC_TEMPLATE,
    ACTUATOR_EVENT_TOPIC_TEMPLATE,
    VirtualTowerState,
    advance_state,
    apply_actuator_command,
    build_actuator_ack,
    observe_state,
    parse_actuator_command,
)
from app.actuator_execution import execute_recommendation  # noqa: E402
from app.actuator_mqtt import MqttExchangeResult, command_payload, parse_ack  # noqa: E402
from app.models import ActionLog, Alert, Base, PlantProfile, Planting, Recommendation, Tower  # noqa: E402
from app.recommendation_decisions import approve_recommendation  # noqa: E402
from app.seed_builtin_profiles import seed_builtin_profiles  # noqa: E402
from app.telemetry_store import process_telemetry_message  # noqa: E402


def _payload(state: VirtualTowerState, rng: random.Random, timestamp: datetime) -> str:
    payload = observe_state(state, rng, timestamp=timestamp)
    return json.dumps(payload)


def test_closed_loop_ph_correction_requires_ack_then_newer_telemetry_recovery():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    base = datetime.now(timezone.utc).replace(microsecond=0)
    rng = random.Random(7)
    virtual = VirtualTowerState.initial("sim-1", 1)
    virtual.ph = 5.20

    with Session(engine, expire_on_commit=False) as session:
        seed_builtin_profiles(session)
        basil = session.scalar(select(PlantProfile).where(PlantProfile.name == "Basil — Hydroponic"))
        tower = Tower(name="Closed Loop Tower", tower_key="sim-1")
        session.add(tower); session.commit()
        session.add(Planting.from_profile(tower_id=tower.id, plant_profile=basil)); session.commit()

        process_telemetry_message(session, "hydroponics/towers/sim-1/telemetry", _payload(virtual, rng, base))
        rec = session.scalar(select(Recommendation))
        alert = session.scalar(select(Alert))
        assert rec is not None and alert is not None and alert.status == "open"
        assert rec.action_type == "increase_ph"
        approval = approve_recommendation(session, rec, now=base + timedelta(milliseconds=200))
        assert approval.valid

        def simulator_transport(claim):
            wire = command_payload(claim)
            parsed = parse_actuator_command(
                ACTUATOR_COMMAND_TOPIC_TEMPLATE.format(tower_key="sim-1"),
                json.dumps(wire),
                {"sim-1"},
            )
            apply_actuator_command(virtual, parsed)
            ack_wire = build_actuator_ack(parsed, base + timedelta(milliseconds=400))
            ack = parse_ack(
                ACTUATOR_EVENT_TOPIC_TEMPLATE.format(tower_key="sim-1"),
                json.dumps(ack_wire),
                expected_command_id=claim.command_id,
                expected_tower_key="sim-1",
                expected_actuator_type=claim.action.actuator_type,
                expected_state=claim.action.state,
            )
            return MqttExchangeResult(True, base + timedelta(milliseconds=300), ack)

        result = execute_recommendation(
            session, rec.id, now=base + timedelta(milliseconds=250), transport=simulator_transport
        )
        assert result.execution_verified is True
        session.refresh(alert)
        assert alert.status == "open"  # ACK is not recovery.
        assert session.scalar(select(ActionLog).where(
            ActionLog.recommendation_id == rec.id,
            ActionLog.event_type == "awaiting_physical_outcome",
        )) is not None

        recovered = False
        for tick in range(1, 19):
            advance_state(virtual, 5.0)
            timestamp = base + timedelta(seconds=tick * 5)
            process_telemetry_message(
                session,
                "hydroponics/towers/sim-1/telemetry",
                _payload(virtual, rng, timestamp),
            )
            session.refresh(alert)
            if alert.status == "resolved":
                recovered = True
                break

        assert recovered is True
        assert virtual.ph > 5.20
        assert session.scalar(select(ActionLog).where(
            ActionLog.recommendation_id == rec.id,
            ActionLog.event_type == "physical_outcome_recovered",
        )) is not None
