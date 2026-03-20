"""
Reporting & aggregation endpoints.

All aggregations are performed via MongoDB aggregation pipelines so that the
work happens server-side rather than pulling documents into Python.

Endpoints
---------
GET /reports/clinic/{clinic_id}/unresolved
    List all patients in a clinic with ≥1 unresolved conflict.

GET /reports/clinic/{clinic_id}/multi-conflict?days=30&min_conflicts=2
    Count (and list) patients with ≥N conflicts in the past D days.

GET /reports/summary?days=30
    Cross-clinic summary: total conflicts, top clinics by unresolved count.
"""

from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.models.schemas import ClinicConflictReport, PatientConflictSummary

router = APIRouter(prefix="/reports", tags=["Reporting"])


@router.get(
    "/clinic/{clinic_id}/unresolved",
    response_model=List[PatientConflictSummary],
    summary="List patients in a clinic with ≥1 unresolved conflict",
)
async def patients_with_unresolved(
    clinic_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> List[PatientConflictSummary]:
    """
    Aggregation pipeline:
      1. Filter conflicts by clinic + unresolved status.
      2. Group by patient_id, count conflicts.
      3. Filter groups with count ≥ 1.
      4. Join patient name from patients collection.
    """
    pipeline = [
        {"$match": {"clinic_id": clinic_id, "status": "unresolved"}},
        {"$group": {"_id": "$patient_id", "unresolved_count": {"$sum": 1}}},
        {"$match": {"unresolved_count": {"$gte": 1}}},
        {
            "$lookup": {
                "from": "patients",
                "localField": "_id",
                "foreignField": "patient_id",
                "as": "patient_info",
            }
        },
        {"$unwind": {"path": "$patient_info", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "patient_id": "$_id",
                "patient_name": {"$ifNull": ["$patient_info.name", "Unknown"]},
                "clinic_id": {"$ifNull": ["$patient_info.clinic_id", clinic_id]},
                "unresolved_count": 1,
            }
        },
        {"$sort": {"unresolved_count": -1}},
    ]
    cursor = db.conflicts.aggregate(pipeline)
    results = []
    async for doc in cursor:
        results.append(PatientConflictSummary(**doc))
    return results


@router.get(
    "/clinic/{clinic_id}/multi-conflict",
    response_model=ClinicConflictReport,
    summary="Count patients with ≥N conflicts in the past D days",
)
async def multi_conflict_report(
    clinic_id: str,
    days: int = Query(30, ge=1, le=365, description="Look-back window in days"),
    min_conflicts: int = Query(2, ge=1, description="Minimum conflict threshold"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ClinicConflictReport:
    since = datetime.utcnow() - timedelta(days=days)

    pipeline = [
        {
            "$match": {
                "clinic_id": clinic_id,
                "detected_at": {"$gte": since},
            }
        },
        {"$group": {"_id": "$patient_id", "total_conflicts": {"$sum": 1}}},
        {"$match": {"total_conflicts": {"$gte": min_conflicts}}},
        {
            "$lookup": {
                "from": "patients",
                "localField": "_id",
                "foreignField": "patient_id",
                "as": "patient_info",
            }
        },
        {"$unwind": {"path": "$patient_info", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "patient_id": "$_id",
                "patient_name": {"$ifNull": ["$patient_info.name", "Unknown"]},
                "total_conflicts": 1,
            }
        },
        {"$sort": {"total_conflicts": -1}},
    ]

    cursor = db.conflicts.aggregate(pipeline)
    patient_details = [doc async for doc in cursor]

    return ClinicConflictReport(
        clinic_id=clinic_id,
        period_days=days,
        patients_with_2plus_conflicts=len(patient_details),
        patient_details=patient_details,
    )


@router.get(
    "/summary",
    summary="Cross-clinic conflict summary for the past D days",
)
async def global_summary(
    days: int = Query(30, ge=1, le=365),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    since = datetime.utcnow() - timedelta(days=days)

    pipeline = [
        {"$match": {"detected_at": {"$gte": since}}},
        {
            "$group": {
                "_id": {"clinic_id": "$clinic_id", "status": "$status"},
                "count": {"$sum": 1},
            }
        },
        {
            "$group": {
                "_id": "$_id.clinic_id",
                "stats": {
                    "$push": {"status": "$_id.status", "count": "$count"}
                },
                "total": {"$sum": "$count"},
            }
        },
        {"$sort": {"total": -1}},
    ]

    cursor = db.conflicts.aggregate(pipeline)
    clinics = []
    async for doc in cursor:
        status_breakdown = {s["status"]: s["count"] for s in doc["stats"]}
        clinics.append({
            "clinic_id": doc["_id"],
            "total_conflicts": doc["total"],
            "unresolved": status_breakdown.get("unresolved", 0),
            "resolved": status_breakdown.get("resolved", 0),
            "dismissed": status_breakdown.get("dismissed", 0),
        })

    total_all = sum(c["total_conflicts"] for c in clinics)
    return {
        "period_days": days,
        "generated_at": datetime.utcnow().isoformat(),
        "total_conflicts": total_all,
        "clinics": clinics,
    }
