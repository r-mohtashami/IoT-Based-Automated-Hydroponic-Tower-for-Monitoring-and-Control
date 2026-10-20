from __future__ import annotations

import argparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    ActionLog,
    ActuatorState,
    Alert,
    PlantProfile,
    Planting,
    Recommendation,
    TelemetryReading,
    Tower,
)
from app.seed_builtin_profiles import BUILTIN_PROFILES, seed_builtin_profiles


CANONICAL_BUILTIN_NAMES = tuple(item["name"] for item in BUILTIN_PROFILES)


def reset_demo_data(session: Session) -> None:
    """Remove mutable/demo history while preserving only canonical evidence-backed built-ins."""
    for model in (
        ActionLog,
        ActuatorState,
        Recommendation,
        Alert,
        TelemetryReading,
        Planting,
        Tower,
    ):
        session.execute(delete(model))

    session.execute(delete(PlantProfile).where(PlantProfile.profile_type != "system_builtin"))
    session.execute(
        delete(PlantProfile).where(
            PlantProfile.profile_type == "system_builtin",
            PlantProfile.name.not_in(CANONICAL_BUILTIN_NAMES),
        )
    )

    for name in CANONICAL_BUILTIN_NAMES:
        ids = list(
            session.scalars(
                select(PlantProfile.id)
                .where(
                    PlantProfile.profile_type == "system_builtin",
                    PlantProfile.name == name,
                )
                .order_by(PlantProfile.id)
            )
        )
        if len(ids) > 1:
            session.execute(delete(PlantProfile).where(PlantProfile.id.in_(ids[1:])))

    session.commit()


def prepare_demo(session: Session, tower_count: int) -> dict[str, object]:
    if tower_count <= 0:
        raise ValueError("tower_count must be greater than zero")

    seed_builtin_profiles(session)
    basil = session.scalar(
        select(PlantProfile).where(
            PlantProfile.profile_type == "system_builtin",
            PlantProfile.name == "Basil — Hydroponic",
        )
    )
    if basil is None:
        raise RuntimeError("Canonical Basil built-in profile is unavailable.")

    towers = [
        Tower(
            tower_key=f"sim-{index}",
            name=f"Demo Tower {index}",
            description="Simulated demo tower managed through the project backend.",
        )
        for index in range(1, tower_count + 1)
    ]
    session.add_all(towers)
    session.commit()

    planting = Planting.from_profile(tower_id=towers[0].id, plant_profile=basil)
    session.add(planting)
    session.commit()

    builtins = list(
        session.scalars(
            select(PlantProfile)
            .where(PlantProfile.profile_type == "system_builtin")
            .order_by(PlantProfile.name)
        )
    )
    return {
        "tower_count": len(towers),
        "tower_keys": [tower.tower_key for tower in towers],
        "active_planting_tower_key": towers[0].tower_key,
        "active_profile": basil.name,
        "official_builtin_profiles": [profile.name for profile in builtins],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a clean reproducible local demo dataset.")
    parser.add_argument("--reset", action="store_true", help="Explicitly remove prior mutable demo/development data.")
    parser.add_argument("--tower-count", type=int, default=3, help="Number of sim-N Towers to create; no hard-coded maximum.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.reset:
        raise SystemExit("Refusing to modify data without explicit --reset.")

    with SessionLocal() as session:
        reset_demo_data(session)
        summary = prepare_demo(session, args.tower_count)

    print("Clean demo dataset ready.")
    print(f"tower_count={summary['tower_count']}")
    print("tower_keys=" + ",".join(summary["tower_keys"]))
    print(f"active_planting={summary['active_planting_tower_key']} -> {summary['active_profile']}")
    print("official_builtin_profiles=" + " | ".join(summary["official_builtin_profiles"]))


if __name__ == "__main__":
    main()
