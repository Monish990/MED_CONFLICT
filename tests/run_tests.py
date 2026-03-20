#!/usr/bin/env python3
"""
Self-contained test runner for MedConflict conflict detection logic.

Uses Python stdlib only (dataclasses, enum, json, pathlib) so it runs
in any environment without external dependencies.

The conflict detection logic is reimplemented verbatim from
app/services/conflict_detector.py — the only difference is that model
classes use @dataclass instead of Pydantic BaseModel.

Run:
    python3 tests/run_tests.py
"""

import json
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

# ─── Colour helpers ──────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):  print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def section(title): print(f"\n{BOLD}{CYAN}▶ {title}{RESET}")


# ─── Minimal model layer (dataclasses mirror the Pydantic schema) ─────────────

class SourceType(str, Enum):
    CLINIC_EMR        = "clinic_emr"
    HOSPITAL_DISCHARGE = "hospital_discharge"
    PATIENT_REPORTED  = "patient_reported"

class ConflictType(str, Enum):
    DOSE_MISMATCH           = "dose_mismatch"
    DRUG_CLASS_COMBINATION  = "drug_class_combination"
    STOPPED_VS_ACTIVE       = "stopped_vs_active"
    DOSE_OUT_OF_RANGE       = "dose_out_of_range"

class ConflictSeverity(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"

class MedicationStatus(str, Enum):
    ACTIVE  = "active"
    STOPPED = "stopped"
    HOLD    = "hold"

UNIT_MAP  = {"milligrams":"mg","micrograms":"mcg","μg":"mcg",
             "millilitres":"ml","milliliters":"ml"}
FREQ_MAP  = {"qd":"once daily","od":"once daily","daily":"once daily",
             "bid":"twice daily","bd":"twice daily",
             "tid":"three times daily","tds":"three times daily",
             "qid":"four times daily","qds":"four times daily"}
ROUTE_MAP = {"po":"oral","by mouth":"oral","intravenous":"iv",
             "subcutaneous":"sc","sublingual":"sl"}

def _norm(s, mapping):
    if s is None: return s
    s = s.strip().lower()
    return mapping.get(s, s)

@dataclass
class MedicationItem:
    name: str
    dose_mg: Optional[float] = None
    dose_unit: Optional[str] = None
    frequency: Optional[str] = None
    route: Optional[str] = None
    status: MedicationStatus = MedicationStatus.ACTIVE

    def __post_init__(self):
        self.name      = self.name.strip().lower()
        self.dose_unit = _norm(self.dose_unit, UNIT_MAP)
        self.frequency = _norm(self.frequency, FREQ_MAP)
        self.route     = _norm(self.route, ROUTE_MAP)
        if isinstance(self.status, str):
            self.status = MedicationStatus(self.status)

@dataclass
class MedicationSnapshot:
    snapshot_id: str
    source: SourceType
    ingested_at: datetime
    medications: List[MedicationItem] = field(default_factory=list)

@dataclass
class ConflictEvidence:
    source_a: str
    source_b: Optional[str]
    drug_a: str
    drug_b: Optional[str]
    detail: str

@dataclass
class Conflict:
    conflict_id: str
    patient_id: str
    clinic_id: str
    conflict_type: ConflictType
    severity: ConflictSeverity
    evidence: ConflictEvidence
    rule_id: Optional[str] = None
    detected_at: datetime = field(default_factory=datetime.utcnow)


# ─── Load rules ──────────────────────────────────────────────────────────────

_RULES_PATH = Path(__file__).parent.parent / "data" / "conflict_rules.json"
with _RULES_PATH.open() as _f:
    RULES: dict = json.load(_f)


# ─── Conflict detection (mirrors app/services/conflict_detector.py) ──────────

def _drug_class(name: str) -> Optional[str]:
    for cls, members in RULES["drug_classes"].items():
        if name in members:
            return cls
    return None

def _fingerprint(pid, ct, da, db, rule_id=None):
    return f"{pid}|{ct}|{da}|{db or ''}|{rule_id or ''}"

def _make(pid, cid, ct, sev, ev, rule_id=None):
    return Conflict(
        conflict_id=str(uuid.uuid4()),
        patient_id=pid, clinic_id=cid,
        conflict_type=ct, severity=sev,
        evidence=ev, rule_id=rule_id,
    )

def detect_conflicts(patient_id, clinic_id, snapshots):
    conflicts, seen = [], set()

    def _add(c, da, db=None):
        fp = _fingerprint(patient_id, c.conflict_type, da, db or "", c.rule_id)
        if fp not in seen:
            seen.add(fp); conflicts.append(c)

    # Latest snapshot per source
    latest = {}
    for s in sorted(snapshots, key=lambda x: x.ingested_at):
        latest[s.source] = s
    snaps = list(latest.values())

    _check_cross(patient_id, clinic_id, snaps, _add)
    _check_combos(patient_id, clinic_id, snaps, _add)
    for s in snaps:
        _check_ranges(patient_id, clinic_id, s, _add)
    return conflicts

def _check_cross(pid, cid, snaps, _add):
    tol = RULES["dose_mismatch_tolerance_pct"] / 100.0
    drug_map = {}
    for snap in snaps:
        for med in snap.medications:
            drug_map.setdefault(med.name, []).append((snap, med))
    for drug, entries in drug_map.items():
        if len(entries) < 2: continue
        for i in range(len(entries)):
            for j in range(i+1, len(entries)):
                sa, ma = entries[i]; sb, mb = entries[j]
                statuses = {ma.status, mb.status}
                if MedicationStatus.ACTIVE in statuses and MedicationStatus.STOPPED in statuses:
                    ev = ConflictEvidence(sa.snapshot_id, sb.snapshot_id, drug, None,
                        f"'{drug}' is {ma.status.value} in {sa.source.value} "
                        f"but {mb.status.value} in {sb.source.value}")
                    _add(_make(pid, cid, ConflictType.STOPPED_VS_ACTIVE,
                               ConflictSeverity.HIGH, ev), drug)
                if (ma.status == MedicationStatus.ACTIVE and
                        mb.status == MedicationStatus.ACTIVE and
                        ma.dose_mg is not None and mb.dose_mg is not None):
                    avg = (ma.dose_mg + mb.dose_mg) / 2
                    if avg > 0:
                        diff = abs(ma.dose_mg - mb.dose_mg) / avg
                        if diff > tol:
                            ev = ConflictEvidence(sa.snapshot_id, sb.snapshot_id, drug, None,
                                f"'{drug}': {ma.dose_mg} vs {mb.dose_mg} ({diff:.0%} diff)")
                            _add(_make(pid, cid, ConflictType.DOSE_MISMATCH,
                                       ConflictSeverity.MEDIUM, ev), drug)

def _check_combos(pid, cid, snaps, _add):
    active = {}
    for snap in snaps:
        for med in snap.medications:
            if med.status == MedicationStatus.ACTIVE:
                active[med.name] = snap.snapshot_id
    for rule in RULES["blacklisted_combinations"]:
        sev = ConflictSeverity(rule["severity"]); rid = rule["id"]
        if "classes" in rule:
            ca, cb = rule["classes"]
            ma = [d for d in active if _drug_class(d) == ca]
            mb = [d for d in active if _drug_class(d) == cb]
            if ma and mb:
                for da in ma:
                    for db in mb:
                        ev = ConflictEvidence(active[da], active[db], da, db, rule["reason"])
                        _add(_make(pid, cid, ConflictType.DRUG_CLASS_COMBINATION, sev, ev, rid), da, db)
        elif "drugs" in rule:
            da, db = rule["drugs"]
            if da in active and db in active:
                ev = ConflictEvidence(active[da], active[db], da, db, rule["reason"])
                _add(_make(pid, cid, ConflictType.DRUG_CLASS_COMBINATION, sev, ev, rid), da, db)

def _check_ranges(pid, cid, snap, _add):
    for med in snap.medications:
        if med.status != MedicationStatus.ACTIVE or med.dose_mg is None: continue
        rule = RULES["dose_ranges"].get(med.name)
        if rule is None: continue
        lo, hi = rule["min_mg"], rule["max_mg"]
        if med.dose_mg < lo or med.dose_mg > hi:
            ev = ConflictEvidence(snap.snapshot_id, None, med.name, None,
                f"'{med.name}' {med.dose_mg} outside [{lo}–{hi}]")
            _add(_make(pid, cid, ConflictType.DOSE_OUT_OF_RANGE, ConflictSeverity.HIGH, ev), med.name)


# ─── Test helpers ─────────────────────────────────────────────────────────────

def snap(source, meds, ingested_at=None):
    return MedicationSnapshot(
        snapshot_id=str(uuid.uuid4()),
        source=source,
        ingested_at=ingested_at or datetime.utcnow(),
        medications=[MedicationItem(**m) for m in meds],
    )

PASS = FAIL = 0

def assert_true(condition, name):
    global PASS, FAIL
    if condition:
        ok(name); PASS += 1
    else:
        fail(name); FAIL += 1

def assert_false(condition, name):
    assert_true(not condition, name)

def run(name, fn):
    global FAIL
    try:
        fn()
    except Exception as e:
        fail(f"{name}  →  {e}")
        traceback.print_exc()
        FAIL += 1


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CASES
# ═══════════════════════════════════════════════════════════════════════════════

def test_dose_mismatch_detected():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"lisinopril","dose_mg":5, "status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"lisinopril","dose_mg":20,"status":"active"}]),
    ]
    cs = detect_conflicts("P1","C1",snaps)
    types = [c.conflict_type for c in cs]
    assert_true(ConflictType.DOSE_MISMATCH in types, "dose mismatch detected (5 mg vs 20 mg)")

def test_within_tolerance_no_conflict():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"lisinopril","dose_mg":10,  "status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"lisinopril","dose_mg":10.5,"status":"active"}]),
    ]
    dm = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_MISMATCH]
    assert_false(dm, "no dose mismatch within 10 % tolerance (10 vs 10.5 mg)")

def test_missing_dose_one_side_no_crash():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"lisinopril","status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"lisinopril","dose_mg":20,"status":"active"}]),
    ]
    dm = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_MISMATCH]
    assert_false(dm, "missing dose on one side → no dose mismatch (no crash)")

def test_both_doses_missing_no_conflict():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"lisinopril","status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"lisinopril","status":"active"}]),
    ]
    dm = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_MISMATCH]
    assert_false(dm, "both doses missing → no dose mismatch")

def test_three_way_dose_mismatch():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"amlodipine","dose_mg":5,  "status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"amlodipine","dose_mg":10, "status":"active"}]),
        snap(SourceType.PATIENT_REPORTED,   [{"name":"amlodipine","dose_mg":7.5,"status":"active"}]),
    ]
    dm = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_MISMATCH]
    assert_true(len(dm) >= 1, "three-way dose mismatch: at least one conflict from three sources")

def test_deduplication_same_run():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"lisinopril","dose_mg":5,"status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"lisinopril","dose_mg":20,"status":"active"}]),
    ]
    dm = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_MISMATCH]
    assert_true(len(dm) == 1, "dedup: exactly one dose-mismatch object per pair in single run")

def test_stopped_vs_active():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"metformin","dose_mg":1000,"status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"metformin","dose_mg":1000,"status":"stopped"}]),
    ]
    sv = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.STOPPED_VS_ACTIVE]
    assert_true(len(sv) >= 1, "stopped-vs-active detected (active clinic vs stopped hospital)")

def test_both_active_no_stopped_conflict():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"metformin","status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"metformin","status":"active"}]),
    ]
    sv = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.STOPPED_VS_ACTIVE]
    assert_false(sv, "both active → no stopped-vs-active conflict")

def test_both_stopped_no_conflict():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"metformin","status":"stopped"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"metformin","status":"stopped"}]),
    ]
    sv = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.STOPPED_VS_ACTIVE]
    assert_false(sv, "both stopped → no stopped-vs-active conflict")

def test_ace_plus_arb_combo():
    snaps = [snap(SourceType.CLINIC_EMR, [
        {"name":"lisinopril","status":"active"},
        {"name":"losartan",  "status":"active"},
    ])]
    rids = {c.rule_id for c in detect_conflicts("P1","C1",snaps)
            if c.conflict_type==ConflictType.DRUG_CLASS_COMBINATION}
    assert_true("COMBO_001" in rids, "ACE inhibitor + ARB → COMBO_001 fired")

def test_warfarin_plus_aspirin():
    snaps = [snap(SourceType.CLINIC_EMR, [
        {"name":"warfarin","status":"active"},
        {"name":"aspirin", "status":"active"},
    ])]
    rids = {c.rule_id for c in detect_conflicts("P1","C1",snaps)
            if c.conflict_type==ConflictType.DRUG_CLASS_COMBINATION}
    assert_true("COMBO_006" in rids, "warfarin + aspirin → COMBO_006 fired")

def test_nsaid_plus_anticoagulant():
    snaps = [snap(SourceType.CLINIC_EMR, [
        {"name":"ibuprofen","status":"active"},
        {"name":"warfarin", "status":"active"},
    ])]
    rids = {c.rule_id for c in detect_conflicts("P1","C1",snaps)
            if c.conflict_type==ConflictType.DRUG_CLASS_COMBINATION}
    assert_true("COMBO_003" in rids, "NSAID + anticoagulant → COMBO_003 fired")

def test_nsaid_plus_ace_inhibitor():
    snaps = [snap(SourceType.CLINIC_EMR, [
        {"name":"naproxen", "status":"active"},
        {"name":"lisinopril","status":"active"},
    ])]
    rids = {c.rule_id for c in detect_conflicts("P1","C1",snaps)
            if c.conflict_type==ConflictType.DRUG_CLASS_COMBINATION}
    assert_true("COMBO_004" in rids, "NSAID + ACE inhibitor → COMBO_004 fired")

def test_stopped_drug_excluded_from_combo():
    snaps = [snap(SourceType.CLINIC_EMR, [
        {"name":"lisinopril","status":"stopped"},
        {"name":"losartan",  "status":"active"},
    ])]
    combo = [c for c in detect_conflicts("P1","C1",snaps)
             if c.conflict_type==ConflictType.DRUG_CLASS_COMBINATION and c.rule_id=="COMBO_001"]
    assert_false(combo, "stopped ACE inhibitor not counted → COMBO_001 NOT fired")

def test_dose_above_max():
    snaps = [snap(SourceType.CLINIC_EMR,
                  [{"name":"furosemide","dose_mg":1000,"dose_unit":"mg","status":"active"}])]
    oor = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_OUT_OF_RANGE]
    assert_true(len(oor)==1, "furosemide 1000 mg above max 600 → DOSE_OUT_OF_RANGE")

def test_dose_below_min():
    snaps = [snap(SourceType.CLINIC_EMR,
                  [{"name":"metformin","dose_mg":100,"dose_unit":"mg","status":"active"}])]
    oor = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_OUT_OF_RANGE]
    assert_true(len(oor)==1, "metformin 100 mg below min 500 → DOSE_OUT_OF_RANGE")

def test_dose_within_range_no_oor():
    snaps = [snap(SourceType.CLINIC_EMR,
                  [{"name":"furosemide","dose_mg":40,"dose_unit":"mg","status":"active"}])]
    oor = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_OUT_OF_RANGE]
    assert_false(oor, "furosemide 40 mg within range → no OOR conflict")

def test_unknown_drug_no_oor():
    snaps = [snap(SourceType.CLINIC_EMR,
                  [{"name":"unknowndrug","dose_mg":99999,"status":"active"}])]
    oor = [c for c in detect_conflicts("P1","C1",snaps) if c.conflict_type==ConflictType.DOSE_OUT_OF_RANGE]
    assert_false(oor, "unlisted drug at any dose → no OOR conflict")

def test_empty_medication_list():
    snaps = [snap(SourceType.CLINIC_EMR, []), snap(SourceType.HOSPITAL_DISCHARGE, [])]
    cs = detect_conflicts("P1","C1",snaps)
    assert_false(cs, "empty med lists → zero conflicts")

def test_single_source_no_cross_conflicts():
    snaps = [snap(SourceType.CLINIC_EMR,
                  [{"name":"aspirin","dose_mg":81,"status":"active"}])]
    cross = [c for c in detect_conflicts("P1","C1",snaps)
             if c.conflict_type in (ConflictType.DOSE_MISMATCH, ConflictType.STOPPED_VS_ACTIVE)]
    assert_false(cross, "single source → no cross-source conflicts possible")

def test_zero_dose_handled_without_crash():
    snaps = [
        snap(SourceType.CLINIC_EMR,         [{"name":"lisinopril","dose_mg":0, "status":"active"}]),
        snap(SourceType.HOSPITAL_DISCHARGE, [{"name":"lisinopril","dose_mg":10,"status":"active"}]),
    ]
    # avg=5, diff=10/5=200% > tol → should detect mismatch; no ZeroDivisionError
    try:
        cs = detect_conflicts("P1","C1",snaps)
        dm = [c for c in cs if c.conflict_type==ConflictType.DOSE_MISMATCH]
        assert_true(len(dm)==1, "zero dose vs nonzero: mismatch detected without crash")
    except ZeroDivisionError:
        assert_false(True, "ZeroDivisionError raised for zero dose comparison")

def test_latest_snapshot_per_source_wins():
    """
    Same source submitted twice. Second submission removes losartan.
    Conflict detection should use the newer snapshot → COMBO_001 should NOT fire.
    """
    t1 = datetime.utcnow() - timedelta(hours=2)
    t2 = datetime.utcnow()
    snaps = [
        snap(SourceType.CLINIC_EMR,
             [{"name":"lisinopril","status":"active"},{"name":"losartan","status":"active"}],
             ingested_at=t1),
        snap(SourceType.CLINIC_EMR,
             [{"name":"lisinopril","status":"active"}],  # losartan removed
             ingested_at=t2),
    ]
    combo = [c for c in detect_conflicts("P1","C1",snaps)
             if c.conflict_type==ConflictType.DRUG_CLASS_COMBINATION and c.rule_id=="COMBO_001"]
    assert_false(combo, "latest snapshot used per source → resolved ACE+ARB not re-flagged")

def test_multiple_conflicts_same_patient():
    snaps = [snap(SourceType.CLINIC_EMR, [
        {"name":"warfarin", "status":"active"},
        {"name":"aspirin",  "status":"active"},
        {"name":"ibuprofen","status":"active"},
    ])]
    cs = detect_conflicts("P1","C1",snaps)
    assert_true(len(cs) >= 2, "complex patient: ≥2 independent conflicts detected at once")

def test_normalization_name_lowercase():
    item = MedicationItem(name="  LISINOPRIL  ", status="active")
    assert_true(item.name == "lisinopril", "drug name normalised to lowercase + trimmed")

def test_normalization_unit():
    item = MedicationItem(name="x", dose_unit="Milligrams", status="active")
    assert_true(item.dose_unit == "mg", "dose unit 'Milligrams' → 'mg'")

def test_normalization_frequency_qd():
    item = MedicationItem(name="x", frequency="QD", status="active")
    assert_true(item.frequency == "once daily", "frequency 'QD' → 'once daily'")

def test_normalization_frequency_bid():
    item = MedicationItem(name="x", frequency="BID", status="active")
    assert_true(item.frequency == "twice daily", "frequency 'BID' → 'twice daily'")

def test_normalization_route_po():
    item = MedicationItem(name="x", route="PO", status="active")
    assert_true(item.route == "oral", "route 'PO' → 'oral'")


# ─── Aggregation logic tests ─────────────────────────────────────────────────

def test_aggregation_unresolved_filter():
    """Simulate the in-memory equivalent of the unresolved aggregation pipeline."""
    fake_conflicts = [
        {"patient_id":"P1","clinic_id":"CLINIC_A","status":"unresolved"},
        {"patient_id":"P1","clinic_id":"CLINIC_A","status":"unresolved"},
        {"patient_id":"P2","clinic_id":"CLINIC_A","status":"resolved"},
        {"patient_id":"P3","clinic_id":"CLINIC_B","status":"unresolved"},
    ]
    # Simulate: filter clinic_A + unresolved, group by patient, count ≥ 1
    from collections import Counter
    clinic = "CLINIC_A"
    counts = Counter(
        c["patient_id"]
        for c in fake_conflicts
        if c["clinic_id"] == clinic and c["status"] == "unresolved"
    )
    result = {pid: cnt for pid, cnt in counts.items() if cnt >= 1}
    assert_true("P1" in result and result["P1"] == 2,
                "aggregation: P1 has 2 unresolved conflicts in CLINIC_A")
    assert_true("P2" not in result,
                "aggregation: P2 excluded (resolved conflict only)")
    assert_true("P3" not in result,
                "aggregation: P3 excluded (different clinic)")

def test_aggregation_multi_conflict_30_day_window():
    """Simulate the 30-day / ≥2 conflicts report in memory."""
    from collections import Counter
    now = datetime.utcnow()
    fake_conflicts = [
        {"patient_id":"P1","clinic_id":"C1","detected_at": now - timedelta(days=5)},
        {"patient_id":"P1","clinic_id":"C1","detected_at": now - timedelta(days=10)},
        {"patient_id":"P2","clinic_id":"C1","detected_at": now - timedelta(days=1)},
        {"patient_id":"P3","clinic_id":"C1","detected_at": now - timedelta(days=40)},  # outside window
    ]
    since = now - timedelta(days=30)
    clinic = "C1"
    counts = Counter(
        c["patient_id"]
        for c in fake_conflicts
        if c["clinic_id"] == clinic and c["detected_at"] >= since
    )
    result = {pid: cnt for pid, cnt in counts.items() if cnt >= 2}
    assert_true("P1" in result, "30-day multi-conflict: P1 has 2 conflicts in window")
    assert_true("P2" not in result, "30-day multi-conflict: P2 excluded (only 1 conflict)")
    assert_true("P3" not in result, "30-day multi-conflict: P3 excluded (outside 30-day window)")

def test_aggregation_cross_clinic_summary():
    """Simulate the global summary aggregation."""
    from collections import defaultdict, Counter
    fake_conflicts = [
        {"clinic_id":"CLINIC_A","status":"unresolved"},
        {"clinic_id":"CLINIC_A","status":"unresolved"},
        {"clinic_id":"CLINIC_A","status":"resolved"},
        {"clinic_id":"CLINIC_B","status":"unresolved"},
        {"clinic_id":"CLINIC_B","status":"dismissed"},
    ]
    summary = defaultdict(Counter)
    for c in fake_conflicts:
        summary[c["clinic_id"]][c["status"]] += 1
    totals = {cid: sum(v.values()) for cid, v in summary.items()}
    assert_true(totals["CLINIC_A"] == 3, "summary: CLINIC_A has 3 total conflicts")
    assert_true(totals["CLINIC_B"] == 2, "summary: CLINIC_B has 2 total conflicts")
    assert_true(summary["CLINIC_A"]["unresolved"] == 2,
                "summary: CLINIC_A has 2 unresolved")
    assert_true(summary["CLINIC_B"]["dismissed"] == 1,
                "summary: CLINIC_B has 1 dismissed")


# ─── Runner ──────────────────────────────────────────────────────────────────

ALL_TESTS = [
    # Dose mismatch
    ("dose mismatch detected", test_dose_mismatch_detected),
    ("within tolerance no conflict", test_within_tolerance_no_conflict),
    ("missing dose one side – no crash", test_missing_dose_one_side_no_crash),
    ("both doses missing", test_both_doses_missing_no_conflict),
    ("three-way dose mismatch", test_three_way_dose_mismatch),
    ("deduplication same run", test_deduplication_same_run),
    # Stopped vs active
    ("stopped-vs-active detected", test_stopped_vs_active),
    ("both active – no stopped conflict", test_both_active_no_stopped_conflict),
    ("both stopped – no conflict", test_both_stopped_no_conflict),
    # Drug class combinations
    ("ACE + ARB combo", test_ace_plus_arb_combo),
    ("warfarin + aspirin", test_warfarin_plus_aspirin),
    ("NSAID + anticoagulant", test_nsaid_plus_anticoagulant),
    ("NSAID + ACE inhibitor", test_nsaid_plus_ace_inhibitor),
    ("stopped drug excluded from combo", test_stopped_drug_excluded_from_combo),
    # Dose out of range
    ("dose above max", test_dose_above_max),
    ("dose below min", test_dose_below_min),
    ("dose within range – no OOR", test_dose_within_range_no_oor),
    ("unknown drug – no OOR", test_unknown_drug_no_oor),
    # Edge cases
    ("empty medication list", test_empty_medication_list),
    ("single source – no cross conflicts", test_single_source_no_cross_conflicts),
    ("zero dose no crash", test_zero_dose_handled_without_crash),
    ("latest snapshot per source wins", test_latest_snapshot_per_source_wins),
    ("multiple conflicts same patient", test_multiple_conflicts_same_patient),
    # Normalisation
    ("normalise name", test_normalization_name_lowercase),
    ("normalise unit", test_normalization_unit),
    ("normalise frequency QD", test_normalization_frequency_qd),
    ("normalise frequency BID", test_normalization_frequency_bid),
    ("normalise route PO", test_normalization_route_po),
    # Aggregation
    ("aggregation: unresolved filter", test_aggregation_unresolved_filter),
    ("aggregation: 30-day multi-conflict", test_aggregation_multi_conflict_30_day_window),
    ("aggregation: cross-clinic summary", test_aggregation_cross_clinic_summary),
]

GROUPS = {
    "Dose Mismatch":           ALL_TESTS[0:6],
    "Stopped vs Active":       ALL_TESTS[6:9],
    "Drug Class Combinations": ALL_TESTS[9:14],
    "Dose Out of Range":       ALL_TESTS[14:18],
    "Edge Cases":              ALL_TESTS[18:23],
    "Normalisation":           ALL_TESTS[23:28],
    "Aggregation Logic":       ALL_TESTS[28:],
}

if __name__ == "__main__":
    print(f"\n{BOLD}MedConflict – Conflict Detection Test Suite{RESET}")
    print("=" * 52)
    for group, tests in GROUPS.items():
        section(group)
        for name, fn in tests:
            run(name, fn)

    total = PASS + FAIL
    colour = GREEN if FAIL == 0 else RED
    print(f"\n{'='*52}")
    print(f"{BOLD}{colour}Results: {PASS}/{total} passed", end="")
    if FAIL:
        print(f"  ({FAIL} FAILED)", end="")
    print(RESET)
    sys.exit(0 if FAIL == 0 else 1)
