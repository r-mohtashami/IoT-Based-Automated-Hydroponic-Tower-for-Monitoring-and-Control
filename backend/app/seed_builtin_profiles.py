from copy import deepcopy

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import PlantProfile


OSU_HLA_6722 = {
    "reference_id": "osu-hla-6722-table-2",
    "institution": "Oklahoma State University Extension",
    "authors": ["Bruce Dunn", "Hardeep Singh"],
    "title": "Electrical Conductivity and pH Guide for Hydroponics",
    "publication_id": "HLA-6722",
    "published": "April 2017",
    "url": "https://extension.okstate.edu/fact-sheets/electrical-conductivity-and-ph-guide-for-hydroponics",
    "location": "Table 2: Optimum range of electrical conductivity (EC) and pH values for hydroponic crops",
    "supports": ["ph", "ec_ms_cm"],
}


BUILTIN_PROFILES = (
    {
        "name": "Basil — Hydroponic",
        "targets": {
            "ph": {
                "min": 5.5,
                "max": 6.0,
                "unit": "pH",
                "source_reference_id": "osu-hla-6722-table-2",
                "provenance": "system_evidence_backed",
            },
            "ec_ms_cm": {
                "min": 1.0,
                "max": 1.6,
                "unit": "mS/cm",
                "source_reference_id": "osu-hla-6722-table-2",
                "provenance": "system_evidence_backed",
            },
        },
        "source_references": [OSU_HLA_6722],
    },
    {
        "name": "Lettuce — Hydroponic",
        "targets": {
            "ph": {
                "min": 6.0,
                "max": 7.0,
                "unit": "pH",
                "source_reference_id": "osu-hla-6722-table-2",
                "provenance": "system_evidence_backed",
            },
            "ec_ms_cm": {
                "min": 1.2,
                "max": 1.8,
                "unit": "mS/cm",
                "source_reference_id": "osu-hla-6722-table-2",
                "provenance": "system_evidence_backed",
            },
        },
        "source_references": [OSU_HLA_6722],
    },
)


def _matching_builtin_targets_with_provenance(
    profile: PlantProfile, definition: dict
) -> dict | None:
    """Return enriched targets for a safe legacy metadata migration, if needed.

    Built-ins remain immutable through the ORM/application surface.  This helper is
    only used by the system seed/migration path.  It adds provenance metadata only
    when an existing factor already exactly matches the canonical min/max/unit/source
    reference; agronomic values and references are never overwritten.
    """
    if not isinstance(profile.targets, dict):
        return None

    updated_targets = deepcopy(profile.targets)
    changed = False
    comparable_keys = ("min", "max", "unit", "source_reference_id")

    for factor, canonical_target in definition["targets"].items():
        existing_target = updated_targets.get(factor)
        if not isinstance(existing_target, dict):
            continue
        if any(existing_target.get(key) != canonical_target.get(key) for key in comparable_keys):
            continue
        if existing_target.get("provenance") is None:
            existing_target["provenance"] = canonical_target["provenance"]
            changed = True

    return updated_targets if changed else None


def seed_builtin_profiles(session: Session) -> tuple[int, int]:
    """Ensure canonical built-ins exist and enrich safe legacy metadata.

    Existing agronomic target values and source references are never overwritten.
    The only allowed update is adding missing factor-level provenance when the
    persisted target already exactly matches the canonical built-in definition.
    """
    created = 0
    existing = 0

    for definition in BUILTIN_PROFILES:
        matches = list(
            session.scalars(
                select(PlantProfile).where(
                    PlantProfile.profile_type == "system_builtin",
                    PlantProfile.name == definition["name"],
                )
            )
        )
        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple built-in profiles already exist with name {definition['name']!r}."
            )
        if matches:
            existing += 1
            enriched_targets = _matching_builtin_targets_with_provenance(matches[0], definition)
            if enriched_targets is not None:
                # Core UPDATE intentionally bypasses the ORM immutability guard for this
                # narrowly-scoped system metadata migration.
                session.execute(
                    update(PlantProfile)
                    .where(PlantProfile.id == matches[0].id)
                    .values(targets=enriched_targets)
                )
            continue

        session.add(
            PlantProfile(
                name=definition["name"],
                profile_type="system_builtin",
                targets=deepcopy(definition["targets"]),
                source_references=deepcopy(definition["source_references"]),
            )
        )
        created += 1

    session.commit()
    return created, existing


def main() -> None:
    with SessionLocal() as session:
        created, existing = seed_builtin_profiles(session)
    print(f"Built-in profiles ready. created={created} existing={existing}")


if __name__ == "__main__":
    main()
