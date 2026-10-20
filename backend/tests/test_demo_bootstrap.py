from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.demo_bootstrap import CANONICAL_BUILTIN_NAMES, prepare_demo, reset_demo_data
from app.models import Base, PlantProfile, Planting, Tower
from app.seed_builtin_profiles import seed_builtin_profiles


def test_clean_demo_removes_historical_residue_and_keeps_only_official_builtins() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_builtin_profiles(session)
        session.add(
            PlantProfile(
                name="Phase 3 Builtin API Test",
                profile_type="system_builtin",
                targets={},
                source_references=[],
            )
        )
        session.add(PlantProfile(name="Old Custom", profile_type="user_custom", targets={}, source_references=[]))
        session.add(Tower(name="Historical Tower", tower_key="old-tower"))
        session.commit()

        reset_demo_data(session)
        summary = prepare_demo(session, 3)

        builtin_names = list(
            session.scalars(
                select(PlantProfile.name)
                .where(PlantProfile.profile_type == "system_builtin")
                .order_by(PlantProfile.name)
            )
        )
        tower_keys = list(session.scalars(select(Tower.tower_key).order_by(Tower.id)))
        planting = session.scalar(select(Planting).where(Planting.status == "active"))

        assert builtin_names == sorted(CANONICAL_BUILTIN_NAMES)
        assert "Phase 3 Builtin API Test" not in builtin_names
        assert tower_keys == ["sim-1", "sim-2", "sim-3"]
        assert summary["tower_count"] == 3
        assert planting is not None
        assert planting.tower.tower_key == "sim-1"
        assert planting.profile_snapshot["name"] == "Basil — Hydroponic"
        ph_target = planting.profile_snapshot["targets"]["ph"]
        assert ph_target["min"] == 5.5
        assert ph_target["max"] == 6.0
        assert ph_target["unit"] == "pH"
        assert ph_target["source_reference_id"] == "osu-hla-6722-table-2"
        assert ph_target["provenance"] == "system_evidence_backed"


def test_demo_tower_count_has_no_hard_coded_ten_limit() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        reset_demo_data(session)
        summary = prepare_demo(session, 12)
        assert summary["tower_count"] == 12
        assert summary["tower_keys"][-1] == "sim-12"
