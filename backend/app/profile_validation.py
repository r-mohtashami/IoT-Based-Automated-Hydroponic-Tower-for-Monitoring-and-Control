"""Application-owned PlantProfile target validation and provenance normalization."""
from __future__ import annotations

import math
from copy import deepcopy
from numbers import Real
from typing import Any

FACTOR_UNITS = {
    "ph": "pH",
    "ec_ms_cm": "mS/cm",
    "water_temp_c": "°C",
    "air_temp_c": "°C",
    "humidity_pct": "%",
    "ppfd_umol_m2_s": "µmol/m²/s",
}


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite numeric value.")
    return float(value)


def normalize_targets(targets: object, *, builtin: bool = False) -> dict[str, Any]:
    if not isinstance(targets, dict):
        raise ValueError("targets must be an object keyed by supported factor.")
    normalized: dict[str, Any] = {}
    for factor, raw in targets.items():
        if factor not in FACTOR_UNITS:
            raise ValueError(f"Unsupported target factor: {factor}.")
        if not isinstance(raw, dict):
            raise ValueError(f"Target {factor} must be an object with min/max values.")
        minimum = _finite_number(raw.get("min"), f"{factor}.min")
        maximum = _finite_number(raw.get("max"), f"{factor}.max")
        if minimum > maximum:
            raise ValueError(f"{factor}.min cannot exceed {factor}.max.")
        item = {"min": minimum, "max": maximum, "unit": FACTOR_UNITS[factor]}
        reference = raw.get("source_reference_id") if isinstance(raw.get("source_reference_id"), str) else None
        provenance = raw.get("provenance")
        if builtin:
            item["provenance"] = "system_evidence_backed" if reference else "system_configured"
            if reference:
                item["source_reference_id"] = reference
        else:
            # User-authored/edited values cannot inherit an evidence claim silently.
            item["provenance"] = "user_configured"
            user_source = raw.get("user_source")
            if isinstance(user_source, str) and user_source.strip():
                item["user_source"] = user_source.strip()
                item["provenance"] = "user_configured_with_user_source"
            elif provenance == "system_evidence_backed" and reference:
                # only preserved by copy endpoint before a value is edited
                item["provenance"] = "system_evidence_backed"
                item["source_reference_id"] = reference
        normalized[factor] = item
    return normalized


def customized_copy_targets(targets: dict[str, Any]) -> dict[str, Any]:
    """A fresh copy may retain factor-level evidence until that factor is edited."""
    return deepcopy(targets)


def merge_user_target_update(current: dict[str, Any], replacement: dict[str, Any]) -> dict[str, Any]:
    """Normalize an editable profile target replacement with factor-level provenance.

    A customized copy may legitimately keep evidence metadata for factors that the
    user did not change. Once a factor is edited, that factor becomes user-configured
    and must lose the inherited evidence claim. The replacement is still a complete
    target set: omitted factors are intentionally removed.
    """
    if not isinstance(replacement, dict):
        raise ValueError("targets must be an object keyed by supported factor.")

    result: dict[str, Any] = {}
    for factor, raw in replacement.items():
        if factor not in FACTOR_UNITS:
            raise ValueError(f"Unsupported target factor: {factor}.")
        if not isinstance(raw, dict):
            raise ValueError(f"Target {factor} must be an object with min/max values.")

        existing = current.get(factor) if isinstance(current, dict) else None
        preserve_evidence = (
            isinstance(existing, dict)
            and existing.get("provenance") == "system_evidence_backed"
            and raw.get("provenance") == "system_evidence_backed"
            and raw.get("source_reference_id") == existing.get("source_reference_id")
            and raw.get("min") == existing.get("min")
            and raw.get("max") == existing.get("max")
            and not raw.get("user_source")
        )
        if preserve_evidence:
            result[factor] = deepcopy(existing)
        else:
            result[factor] = normalize_targets({factor: raw}, builtin=False)[factor]
    return result
