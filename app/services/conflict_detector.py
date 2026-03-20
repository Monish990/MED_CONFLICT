"""
Conflict detection service.

Rules evaluated on every ingest
--------------------------------
1. DOSE_MISMATCH      – same drug (normalised name) appears in ≥2 sources
                        with doses that differ beyond the tolerance threshold.
2. STOPPED_VS_ACTIVE  – drug marked as "stopped" in one source is "active"
                        in another source (more recent ingest wins nothing;
                        we flag both directions as conflicts for clinician review).
3. DRUG_CLASS_COMBINATION – the *current* active medication set (latest snapshot
                        per source) contains both members of a blacklisted pair.
4. DOSE_OUT_OF_RANGE  – dose for a known drug falls outside the safe range
                        defined in conflict_rules.json.

Deduplication
-------------
A conflict is identified by (patient_id, conflict_type, drug_a, drug_b).
Before inserting we check whether an UNRESOLVED conflict with the same
fingerprint already exists – if so we skip it rather than creating duplicates.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.models.schemas import (
    Conflict, ConflictEvidence, ConflictSeverity, ConflictStatus,
    ConflictType, MedicationItem, MedicationStatus, MedicationSnapshot,
    SourceType,
)

# Load rules once at module import
_RULES_PATH = Path(__file__).parent.parent.parent / "data" / "conflict_rules.json"
with _RULES_PATH.open() as _f:
    RULES: dict = json.load(_f)


def _drug_class(drug_name: str) -> Optional[str]:
    """Return the class key for a drug name, or None."""
    for cls, members in RULES["drug_classes"].items():
        if drug_name in members:
            return cls
    return None


def _fingerprint(patient_id: str, conflict_type: ConflictType,
                 drug_a: str, drug_b: Optional[str], rule_id: Optional[str] = None) -> str:
    """Stable string used for deduplication lookups."""
    key = f"{patient_id}|{conflict_type}|{drug_a}|{drug_b or ''}|{rule_id or ''}"
    return hashlib.sha256(key.encode()).hexdigest()


def _make_conflict(
    patient_id: str,
    clinic_id: str,
    conflict_type: ConflictType,
    severity: ConflictSeverity,
    evidence: ConflictEvidence,
    rule_id: Optional[str] = None,
) -> Conflict:
    return Conflict(
        conflict_id=str(uuid.uuid4()),
        patient_id=patient_id,
        clinic_id=clinic_id,
        conflict_type=conflict_type,
        severity=severity,
        status=ConflictStatus.UNRESOLVED,
        rule_id=rule_id,
        evidence=evidence,
        detected_at=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_conflicts(
    patient_id: str,
    clinic_id: str,
    snapshots: List[MedicationSnapshot],
) -> List[Conflict]:
    """
    Run all conflict rules against the full snapshot list for a patient.
    Returns a list of new Conflict objects (without DB interaction).
    """
    conflicts: List[Conflict] = []
    seen_fingerprints: set[str] = set()

    def _add(c: Conflict, drug_a: str, drug_b: Optional[str] = None) -> None:
        fp = _fingerprint(patient_id, c.conflict_type, drug_a, drug_b or "", c.rule_id)
        if fp not in seen_fingerprints:
            seen_fingerprints.add(fp)
            conflicts.append(c)

    # Build a per-source map of the *latest* snapshot
    latest_per_source: Dict[SourceType, MedicationSnapshot] = {}
    for snap in sorted(snapshots, key=lambda s: s.ingested_at):
        latest_per_source[snap.source] = snap

    all_latest_snaps = list(latest_per_source.values())

    # ── Rule 1 & 2: cross-source per-drug comparisons ──────────────────────
    _check_cross_source(patient_id, clinic_id, all_latest_snaps,
                        _add, conflicts)

    # ── Rule 3: drug-class combinations within merged active set ───────────
    _check_class_combinations(patient_id, clinic_id, all_latest_snaps,
                               _add, conflicts)

    # ── Rule 4: dose out of range (per snapshot) ───────────────────────────
    for snap in all_latest_snaps:
        _check_dose_ranges(patient_id, clinic_id, snap, _add, conflicts)

    return conflicts


def _check_cross_source(
    patient_id: str, clinic_id: str,
    snaps: List[MedicationSnapshot],
    _add, conflicts: List[Conflict],
) -> None:
    """Dose mismatch and stopped-vs-active checks across sources."""
    tolerance = RULES["dose_mismatch_tolerance_pct"] / 100.0

    # Index: drug_name -> list of (snapshot, MedicationItem)
    drug_map: Dict[str, List[Tuple[MedicationSnapshot, MedicationItem]]] = {}
    for snap in snaps:
        for med in snap.medications:
            drug_map.setdefault(med.name, []).append((snap, med))

    for drug_name, entries in drug_map.items():
        if len(entries) < 2:
            continue
        # Compare every pair
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                snap_a, med_a = entries[i]
                snap_b, med_b = entries[j]

                # Stopped-vs-active
                statuses = {med_a.status, med_b.status}
                if (MedicationStatus.ACTIVE in statuses and
                        MedicationStatus.STOPPED in statuses):
                    ev = ConflictEvidence(
                        source_a=snap_a.snapshot_id,
                        source_b=snap_b.snapshot_id,
                        drug_a=drug_name,
                        drug_b=None,
                        detail=(
                            f"'{drug_name}' is {med_a.status.value} in "
                            f"{snap_a.source.value} but {med_b.status.value} "
                            f"in {snap_b.source.value}"
                        ),
                    )
                    c = _make_conflict(patient_id, clinic_id,
                                       ConflictType.STOPPED_VS_ACTIVE,
                                       ConflictSeverity.HIGH, ev)
                    _add(c, drug_name)

                # Dose mismatch (only compare active meds with known doses)
                if (med_a.status == MedicationStatus.ACTIVE and
                        med_b.status == MedicationStatus.ACTIVE and
                        med_a.dose_mg is not None and med_b.dose_mg is not None):
                    avg = (med_a.dose_mg + med_b.dose_mg) / 2
                    if avg > 0:
                        diff_pct = abs(med_a.dose_mg - med_b.dose_mg) / avg
                        if diff_pct > tolerance:
                            ev = ConflictEvidence(
                                source_a=snap_a.snapshot_id,
                                source_b=snap_b.snapshot_id,
                                drug_a=drug_name,
                                drug_b=None,
                                detail=(
                                    f"'{drug_name}': {med_a.dose_mg} "
                                    f"{med_a.dose_unit or 'mg'} in "
                                    f"{snap_a.source.value} vs "
                                    f"{med_b.dose_mg} {med_b.dose_unit or 'mg'} "
                                    f"in {snap_b.source.value} "
                                    f"({diff_pct:.0%} difference)"
                                ),
                            )
                            c = _make_conflict(
                                patient_id, clinic_id,
                                ConflictType.DOSE_MISMATCH,
                                ConflictSeverity.MEDIUM, ev)
                            _add(c, drug_name)


def _check_class_combinations(
    patient_id: str, clinic_id: str,
    snaps: List[MedicationSnapshot],
    _add, conflicts: List[Conflict],
) -> None:
    """Check blacklisted drug-class (and specific-drug) combinations."""
    # Merge all active drugs across sources
    active_drugs: Dict[str, str] = {}  # name -> snapshot_id
    for snap in snaps:
        for med in snap.medications:
            if med.status == MedicationStatus.ACTIVE:
                active_drugs[med.name] = snap.snapshot_id

    for rule in RULES["blacklisted_combinations"]:
        severity = ConflictSeverity(rule["severity"])
        rule_id = rule["id"]

        if "classes" in rule:
            cls_a, cls_b = rule["classes"]
            members_a = [d for d in active_drugs if _drug_class(d) == cls_a]
            members_b = [d for d in active_drugs if _drug_class(d) == cls_b]
            if members_a and members_b:
                for da in members_a:
                    for db in members_b:
                        ev = ConflictEvidence(
                            source_a=active_drugs[da],
                            source_b=active_drugs[db],
                            drug_a=da,
                            drug_b=db,
                            detail=rule["reason"],
                        )
                        c = _make_conflict(patient_id, clinic_id,
                                           ConflictType.DRUG_CLASS_COMBINATION,
                                           severity, ev, rule_id)
                        _add(c, da, db)

        elif "drugs" in rule:
            drug_a_name, drug_b_name = rule["drugs"]
            if drug_a_name in active_drugs and drug_b_name in active_drugs:
                ev = ConflictEvidence(
                    source_a=active_drugs[drug_a_name],
                    source_b=active_drugs[drug_b_name],
                    drug_a=drug_a_name,
                    drug_b=drug_b_name,
                    detail=rule["reason"],
                )
                c = _make_conflict(patient_id, clinic_id,
                                   ConflictType.DRUG_CLASS_COMBINATION,
                                   severity, ev, rule_id)
                _add(c, drug_a_name, drug_b_name)


def _check_dose_ranges(
    patient_id: str, clinic_id: str,
    snap: MedicationSnapshot,
    _add, conflicts: List[Conflict],
) -> None:
    """Flag doses outside the known-safe range for a drug."""
    for med in snap.medications:
        if med.status != MedicationStatus.ACTIVE:
            continue
        if med.dose_mg is None:
            continue
        rule = RULES["dose_ranges"].get(med.name)
        if rule is None:
            continue
        lo, hi = rule["min_mg"], rule["max_mg"]
        if med.dose_mg < lo or med.dose_mg > hi:
            ev = ConflictEvidence(
                source_a=snap.snapshot_id,
                source_b=None,
                drug_a=med.name,
                drug_b=None,
                detail=(
                    f"'{med.name}' dose {med.dose_mg} "
                    f"{med.dose_unit or 'mg'} in {snap.source.value} "
                    f"is outside safe range [{lo}–{hi} {rule['unit']}]"
                ),
            )
            c = _make_conflict(patient_id, clinic_id,
                               ConflictType.DOSE_OUT_OF_RANGE,
                               ConflictSeverity.HIGH, ev)
            _add(c, med.name)
