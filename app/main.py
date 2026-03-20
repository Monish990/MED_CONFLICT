"""MedConflict  Medication Conflict Detection Service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import ingestion, conflicts, reports
from app.core.database import get_db, init_indexes, close_connection


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    await init_indexes(db)
    yield
    await close_connection()


app = FastAPI(
    title="MedConflict",
    description=(
        "Medication conflict detection service for chronic-care patients. "
        "Ingests medication lists from multiple sources (clinic EMR, hospital "
        "discharge, patient-reported), detects conflicts, and provides "
        "aggregation / reporting endpoints."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(ingestion.router)
app.include_router(conflicts.router)
app.include_router(reports.router)


@app.get("/health", tags=["Meta"])
async def health():
    return {"status": "ok"}
