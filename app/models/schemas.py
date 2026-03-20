"""
Pydantic models and MongoDB document schemas.

Design decisions
----------------
* MedicationSnapshot is stored as a *versioned sub-document* inside the
  Patient document (versions array).  Each ingest from any source appends a new
  element rather than mutating existing ones – this gives a full audit trail
  at the cost of slightly larger documents.

* Conflicts are stored in a top-level `conflicts` collection (referenced by
  patient_id) instead of being embedded inside Patient.  Rationale:
    - Conflicts can be large and numerous; embedding would cause document growth
      beyond the 16 MB MongoDB limit for active chronic-care patients.
    - Reporting queries ("all unresolved conflicts across clinic X") are easier
      with a dedicated, indexed collection.
    - Resolution updates only touch the conflict document, not the patient.

* Trade-off: two-collection joins are needed for some queries.  We accept
  this for correctness and auditability.

Indexes (see app/core/database.py):
  patients:   { patient_id: 1 }  unique
              { clinic_id: 1 }
  conflicts:  { patient_id: 1, status: 1 }
              { clinic_id: 1, status: 1, detected_at: -1 }
              { detected_at: -1 }   (TTL / range queries)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
import re


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class SourceType(str, Enum):
    CLINIC_EMR = "clinic_emr"
    HOSPITAL_DISCHARGE = "hospital_discharge"
    PATIENT_REPORTED = "patient_reported"


class ConflictType(str, Enum):
    DOSE_MISMATCH = "dose_mismatch"
    DRUG_CLASS_COMBINATION = "drug_class_combination"
    STOPPED_VS_ACTIVE = "stopped_vs_active"
    DOSE_OUT_OF_RANGE = "dose_out_of_range"


class ConflictSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConflictStatus(str, Enum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class MedicationStatus(str, Enum):
    ACTIVE = "active"
    STOPPED = "stopped"
    HOLD = "hold"


# ---------------------------------------------------------------------------
# Medication item
# ---------------------------------------------------------------------------

class MedicationItem(BaseModel):
    name: str
    dose_mg: Optional[float] = None
    dose_unit: Optional[str] = None          # e.g. "mg", "units", "mcg"
    frequency: Optional[str] = None          # e.g. "once daily"
    route: Optional[str] = None              # e.g. "oral", "iv"
    status: MedicationStatus = MedicationStatus.ACTIVE
    prescriber: Optional[str] = None
    raw_name: Optional[str] = None           # original string before normalisation

    @field_validator("name", mode="before")
    @classmethod
    def normalise_name(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("dose_unit", mode="before")
    @classmethod
    def normalise_unit(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        # collapse common abbreviations
        unit_map = {"milligrams": "mg", "micrograms": "mcg", "μg": "mcg",
                    "millilitres": "ml", "milliliters": "ml"}
        return unit_map.get(v, v)

    @field_validator("frequency", mode="before")
    @classmethod
    def normalise_frequency(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        freq_map = {
            "qd": "once daily", "od": "once daily", "daily": "once daily",
            "bid": "twice daily", "bd": "twice daily",
            "tid": "three times daily", "tds": "three times daily",
            "qid": "four times daily", "qds": "four times daily",
        }
        return freq_map.get(v, v)

    @field_validator("route", mode="before")
    @classmethod
    def normalise_route(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        route_map = {"po": "oral", "by mouth": "oral", "intravenous": "iv",
                     "subcutaneous": "sc", "sublingual": "sl"}
        return route_map.get(v, v)


# ---------------------------------------------------------------------------
# Snapshot (one ingest event)
# ---------------------------------------------------------------------------

class MedicationSnapshot(BaseModel):
    snapshot_id: str                          # UUID
    source: SourceType
    ingested_at: datetime
    medications: List[MedicationItem]
    raw_payload_hash: Optional[str] = None    # SHA-256 of raw JSON for dedup
    ingested_by: Optional[str] = None         # user/system that submitted


# ---------------------------------------------------------------------------
# Patient document (stored in `patients` collection)
# ---------------------------------------------------------------------------

class Patient(BaseModel):
    patient_id: str
    name: str
    date_of_birth: Optional[str] = None
    clinic_id: str
    snapshots: List[MedicationSnapshot] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Conflict document (stored in `conflicts` collection)
# ---------------------------------------------------------------------------

class ConflictEvidence(BaseModel):
    source_a: str              # snapshot_id
    source_b: Optional[str]    # snapshot_id (None for single-source rules)
    drug_a: str
    drug_b: Optional[str]
    detail: str                # human-readable description


class ConflictResolution(BaseModel):
    resolved_by: str
    resolved_at: datetime
    reason: str
    chosen_source: Optional[SourceType] = None


class Conflict(BaseModel):
    conflict_id: str           # UUID
    patient_id: str
    clinic_id: str
    conflict_type: ConflictType
    severity: ConflictSeverity
    status: ConflictStatus = ConflictStatus.UNRESOLVED
    rule_id: Optional[str] = None
    evidence: ConflictEvidence
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    resolution: Optional[ConflictResolution] = None


# ---------------------------------------------------------------------------
# API request / response shapes
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    patient_id: str
    patient_name: str
    date_of_birth: Optional[str] = None
    clinic_id: str
    source: SourceType
    medications: List[MedicationItem]
    submitted_by: Optional[str] = None


class IngestResponse(BaseModel):
    patient_id: str
    snapshot_id: str
    new_conflicts: int
    message: str


class ResolveConflictRequest(BaseModel):
    resolved_by: str
    reason: str
    chosen_source: Optional[SourceType] = None


class ConflictOut(BaseModel):
    conflict_id: str
    patient_id: str
    clinic_id: str
    conflict_type: str
    severity: str
    status: str
    evidence: Dict[str, Any]
    detected_at: datetime
    resolution: Optional[Dict[str, Any]] = None


class PatientConflictSummary(BaseModel):
    patient_id: str
    patient_name: str
    clinic_id: str
    unresolved_count: int


class ClinicConflictReport(BaseModel):
    clinic_id: str
    period_days: int
    patients_with_2plus_conflicts: int
    patient_details: List[Dict[str, Any]]
