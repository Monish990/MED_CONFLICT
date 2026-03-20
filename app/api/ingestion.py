"""Ingestion endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.models.schemas import IngestRequest, IngestResponse
from app.services.ingestion import ingest_medication_list

router = APIRouter(prefix="/ingest", tags=["Ingestion"])


@router.post(
    "/{patient_id}",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a medication list for a patient from a named source",
)
async def ingest(
    patient_id: str,
    body: IngestRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> IngestResponse:
    if body.patient_id != patient_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="patient_id in URL and body must match",
        )

    snapshot_id, new_conflicts = await ingest_medication_list(db, body)

    return IngestResponse(
        patient_id=patient_id,
        snapshot_id=snapshot_id,
        new_conflicts=new_conflicts,
        message=(
            f"Snapshot {snapshot_id} created. "
            f"{new_conflicts} new conflict(s) detected."
        ),
    )