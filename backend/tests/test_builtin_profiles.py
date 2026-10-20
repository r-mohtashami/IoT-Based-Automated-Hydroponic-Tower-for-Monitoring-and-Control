from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, PlantProfile, Planting, Tower
from app.seed_builtin_profiles import seed_builtin_profiles


def test_seed_creates_evidence_backed_builtins_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        first = seed_builtin_profiles(session)
        second = seed_builtin_profiles(session)

        profiles = list(
            session.scalars(
                select(PlantProfile)
                .where(PlantProfile.profile_type == "system_builtin")
                .order_by(PlantProfile.name)
            )
        )

        assert first == (2, 0)
        assert second == (0, 2)
        assert [profile.name for profile in profiles] == [
            "Basil — Hydroponic",
            "Lettuce — Hydroponic",
        ]

        basil, lettuce = profiles
        assert basil.targets["ph"]["provenance"] == "system_evidence_backed"
        assert basil.targets["ec_ms_cm"]["provenance"] == "system_evidence_backed"
        comparable = {k: {ik: iv for ik, iv in v.items() if ik != "provenance"} for k, v in basil.targets.items()}
        assert comparable == {
            "ph": {
                "min": 5.5,
                "max": 6.0,
                "unit": "pH",
                "source_reference_id": "osu-hla-6722-table-2",
            },
            "ec_ms_cm": {
                "min": 1.0,
                "max": 1.6,
                "unit": "mS/cm",
                "source_reference_id": "osu-hla-6722-table-2",
            },
        }
        assert lettuce.targets["ph"]["min"] == 6.0
        assert lettuce.targets["ph"]["max"] == 7.0
        assert lettuce.targets["ec_ms_cm"]["min"] == 1.2
        assert lettuce.targets["ec_ms_cm"]["max"] == 1.8

        for profile in profiles:
            assert set(profile.targets) == {"ph", "ec_ms_cm"}
            assert len(profile.source_references) == 1
            reference = profile.source_references[0]
            assert reference["institution"] == "Oklahoma State University Extension"
            assert reference["publication_id"] == "HLA-6722"
            assert reference["title"] == "Electrical Conductivity and pH Guide for Hydroponics"
            assert reference["authors"] == ["Bruce Dunn", "Hardeep Singh"]
            assert reference["location"].startswith("Table 2:")
            assert reference["supports"] == ["ph", "ec_ms_cm"]
            assert reference["url"].startswith("https://extension.okstate.edu/")


def test_reseeding_does_not_change_active_planting_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        seed_builtin_profiles(session)
        lettuce = session.scalar(
            select(PlantProfile).where(PlantProfile.name == "Lettuce — Hydroponic")
        )
        assert lettuce is not None

        tower = Tower(name="Seed Snapshot Tower")
        session.add(tower)
        session.commit()

        planting = Planting.from_profile(tower_id=tower.id, plant_profile=lettuce)
        session.add(planting)
        session.commit()
        planting_id = planting.id
        original_snapshot = planting.profile_snapshot.copy()

        assert seed_builtin_profiles(session) == (0, 2)
        session.expire_all()

        stored = session.get(Planting, planting_id)
        assert stored is not None
        assert stored.status == "active"
        assert stored.profile_snapshot == original_snapshot


def test_reseed_does_not_mutate_existing_builtin_targets_or_references() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    existing_targets = {"test_marker": {"value": "keep-existing"}}
    existing_references = [{"label": "existing-reference-must-not-change"}]

    with Session(engine) as session:
        existing = PlantProfile(
            name="Basil — Hydroponic",
            profile_type="system_builtin",
            targets=existing_targets.copy(),
            source_references=existing_references.copy(),
        )
        historical_artifact = PlantProfile(
            name="Phase 3 Builtin API Test",
            profile_type="system_builtin",
            targets={},
            source_references=[],
        )
        session.add_all([existing, historical_artifact])
        session.commit()
        existing_id = existing.id
        artifact_id = historical_artifact.id

        assert seed_builtin_profiles(session) == (1, 1)
        session.expire_all()

        stored = session.get(PlantProfile, existing_id)
        artifact = session.get(PlantProfile, artifact_id)
        assert stored is not None
        assert stored.targets == existing_targets
        assert stored.source_references == existing_references
        assert artifact is not None
        assert artifact.targets == {}
        assert artifact.source_references == []

        official_names = {
            profile.name
            for profile in session.scalars(
                select(PlantProfile).where(
                    PlantProfile.name.in_(["Basil — Hydroponic", "Lettuce — Hydroponic"])
                )
            )
        }
        assert official_names == {"Basil — Hydroponic", "Lettuce — Hydroponic"}


def test_seed_backfills_missing_provenance_only_for_matching_legacy_builtin() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    legacy_targets = {
        "ph": {
            "min": 5.5,
            "max": 6.0,
            "unit": "pH",
            "source_reference_id": "osu-hla-6722-table-2",
        },
        "ec_ms_cm": {
            "min": 1.0,
            "max": 1.6,
            "unit": "mS/cm",
            "source_reference_id": "osu-hla-6722-table-2",
        },
    }

    with Session(engine) as session:
        legacy = PlantProfile(
            name="Basil — Hydroponic",
            profile_type="system_builtin",
            targets=legacy_targets,
            source_references=[],
        )
        session.add(legacy)
        session.commit()
        legacy_id = legacy.id

        assert seed_builtin_profiles(session) == (1, 1)
        session.expire_all()

        stored = session.get(PlantProfile, legacy_id)
        assert stored is not None
        assert stored.targets["ph"]["provenance"] == "system_evidence_backed"
        assert stored.targets["ec_ms_cm"]["provenance"] == "system_evidence_backed"
        assert stored.targets["ph"]["min"] == 5.5
        assert stored.targets["ph"]["max"] == 6.0
        assert stored.source_references == []
