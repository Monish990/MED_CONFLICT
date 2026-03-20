"""
Ingestion service – persists snapshots and triggers conflict detection.

Versioning policy
-----------------
A **new snapshot is always appended** for every ingest call, even if the
payload is byte-for-byte identical to the last one from the same source.

Rationale:
  - We cannot know whether "no change" is intentional reconciliation or a
    re-submission bug; keeping all submissions preserves the full audit log.
  - Deduplication of *conflicts* (not snapshots) prevents noise from
    repeated identical payloads.
  - The trade-off is document growth; in production, snapshots older than
    N months can be archived to a cold collection via a background job.

Conflict deduplication
----------------------
Before inserting a detected conflict we check the `conflicts` collection for
an existing UNRESOLVED record with the same
(patient_id, conflict_type, drug_a, drug_b) fingerprint.  If one exists we
skip the insert to avoid duplicates.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import List

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.schemas import (
    Conflict, ConflictStatus, IngestRequest, MedicationSnapshot, Patient,
)
from app.services.conflict_detector import detect_conflicts


async def ingest_medication_list(
    db: AsyncIOMotorDatabase,
    req: IngestRequest,
) -> tuple[str, int]:
    """
    Upsert patient, append snapshot, detect + persist new conflicts.

    Returns (snapshot_id, new_conflict_count).
    """
    snapshot_id = str(uuid.uuid4())
    raw_hash = _hash_payload(req)
    ingested_at = datetime.utcnow()

    snapshot = MedicationSnapshot(
        snapshot_id=snapshot_id,
        source=req.source,
        ingested_at=ingested_at,
        medications=req.medications,
        raw_payload_hash=raw_hash,
        ingested_by=req.submitted_by,
    )

    # Upsert patient document; push new snapshot into the versions array
    snap_doc = snapshot.model_dump()
    snap_doc["ingested_at"] = ingested_at   # keep as datetime for Mongo

    result = await db.patients.update_one(
        {"patient_id": req.patient_id},
        {
            "$setOnInsert": {
                "patient_id": req.patient_id,
                "name": req.patient_name,
                "date_of_birth": req.date_of_birth,
                "clinic_id": req.clinic_id,
                "created_at": ingested_at,
            },
            "$push": {"snapshots": snap_doc},
            "$set": {"updated_at": ingested_at},
        },
        upsert=True,
    )

    # Load full patient to run conflict detection against all snapshots
    patient_doc = await db.patients.find_one({"patient_id": req.patient_id})
    patient = _doc_to_patient(patient_doc)

    new_conflicts = detect_conflicts(
        patient.patient_id,
        patient.clinic_id,
        patient.snapshots,
    )

    # Persist only genuinely new conflicts (dedup by fingerprint)
    inserted_count = 0
    for c in new_conflicts:
        existing = await db.conflicts.find_one({
            "patient_id": c.patient_id,
            "conflict_type": c.conflict_type.value,
            "evidence.drug_a": c.evidence.drug_a,
            "evidence.drug_b": c.evidence.drug_b,
            "rule_id": c.rule_id,
            "status": ConflictStatus.UNRESOLVED.value,
        })
        if existing is None:
            doc = c.model_dump()
            doc["detected_at"] = c.detected_at
            await db.conflicts.insert_one(doc)
            inserted_count += 1

    return snapshot_id, inserted_count


def _hash_payload(req: IngestRequest) -> str:
    payload = json.dumps(
        [m.model_dump() for m in req.medications],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _doc_to_patient(doc: dict) -> Patient:
    """Convert a raw MongoDB document back to a Patient model."""
    snaps = []
    for s in doc.get("snapshots", []):
        from app.models.schemas import MedicationItem
        meds = [MedicationItem(**m) for m in s.get("medications", [])]
        snaps.append(MedicationSnapshot(
            snapshot_id=s["snapshot_id"],
            source=s["source"],
            ingested_at=s["ingested_at"],
            medications=meds,
            raw_payload_hash=s.get("raw_payload_hash"),
            ingested_by=s.get("ingested_by"),
        ))
    return Patient(
        patient_id=doc["patient_id"],
        name=doc["name"],
        date_of_birth=doc.get("date_of_birth"),
        clinic_id=doc["clinic_id"],
        snapshots=snaps,
        created_at=doc.get("created_at", datetime.utcnow()),
        updated_at=doc.get("updated_at", datetime.utcnow()),
    )
