"""Conflict management endpoints."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.models.schemas import (
    ConflictOut, ConflictResolution, ConflictStatus,
    ResolveConflictRequest,
)

router = APIRouter(prefix="/conflicts", tags=["Conflicts"])


def _serialise(doc: dict) -> ConflictOut:
    return ConflictOut(
        conflict_id=doc["conflict_id"],
        patient_id=doc["patient_id"],
        clinic_id=doc["clinic_id"],
        conflict_type=doc["conflict_type"],
        severity=doc["severity"],
        status=doc["status"],
        evidence=doc["evidence"],
        detected_at=doc["detected_at"],
        resolution=doc.get("resolution"),
    )


@router.get(
    "/patient/{patient_id}",
    response_model=List[ConflictOut],
    summary="List all conflicts for a patient, optionally filtered by status",
)
async def list_patient_conflicts(
    patient_id: str,
    conflict_status: Optional[str] = Query(None, alias="status"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> List[ConflictOut]:
    query: dict = {"patient_id": patient_id}
    if conflict_status:
        query["status"] = conflict_status
    cursor = db.conflicts.find(query).sort("detected_at", -1)
    return [_serialise(d) async for d in cursor]


@router.patch(
    "/{conflict_id}/resolve",
    response_model=ConflictOut,
    summary="Mark a conflict as resolved with a reason",
)
async def resolve_conflict(
    conflict_id: str,
    body: ResolveConflictRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ConflictOut:
    resolution = ConflictResolution(
        resolved_by=body.resolved_by,
        resolved_at=datetime.utcnow(),
        reason=body.reason,
        chosen_source=body.chosen_source,
    )
    updated = await db.conflicts.find_one_and_update(
        {"conflict_id": conflict_id},
        {
            "$set": {
                "status": ConflictStatus.RESOLVED.value,
                "resolution": resolution.model_dump(),
            }
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return _serialise(updated)


@router.patch(
    "/{conflict_id}/dismiss",
    response_model=ConflictOut,
    summary="Dismiss a conflict (clinically reviewed, no action needed)",
)
async def dismiss_conflict(
    conflict_id: str,
    body: ResolveConflictRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ConflictOut:
    resolution = ConflictResolution(
        resolved_by=body.resolved_by,
        resolved_at=datetime.utcnow(),
        reason=body.reason,
        chosen_source=body.chosen_source,
    )
    updated = await db.conflicts.find_one_and_update(
        {"conflict_id": conflict_id},
        {
            "$set": {
                "status": ConflictStatus.DISMISSED.value,
                "resolution": resolution.model_dump(),
            }
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return _serialise(updated)
