"""
MongoDB connection management and index initialisation.

Index rationale
---------------
patients:
  - patient_id (unique)          – primary lookup by patient
  - clinic_id                    – filter patients by clinic (reporting)
  - snapshots.source + patient_id – locate snapshots by source without full scan

conflicts:
  - patient_id + status          – "all unresolved conflicts for patient X"
  - clinic_id + status + detected_at
                                 – "unresolved conflicts in clinic X, date range"
  - detected_at                  – range queries for the 30-day reporting window
  - conflict_id (unique)         – direct fetch by ID
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "medconflict")

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URL)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client()[DB_NAME]


async def init_indexes(db: AsyncIOMotorDatabase) -> None:
    from pymongo import ASCENDING, DESCENDING, IndexModel

    # ── patients ────────────────────────────────────────────────────────────
    await db.patients.create_indexes([
        IndexModel([("patient_id", ASCENDING)], unique=True),
        IndexModel([("clinic_id", ASCENDING)]),
    ])

    # ── conflicts ───────────────────────────────────────────────────────────
    await db.conflicts.create_indexes([
        IndexModel([("conflict_id", ASCENDING)], unique=True),
        IndexModel([("patient_id", ASCENDING), ("status", ASCENDING)]),
        IndexModel([("clinic_id", ASCENDING), ("status", ASCENDING),
                    ("detected_at", DESCENDING)]),
        IndexModel([("detected_at", DESCENDING)]),
    ])


async def close_connection() -> None:
    global _client
    if _client:
        _client.close()
        _client = None
