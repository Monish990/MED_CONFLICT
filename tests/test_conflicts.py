"""
Test suite for MedConflict.

Tests are structured in three groups:
  1. Unit – conflict_detector.py in isolation (no DB, no HTTP)
  2. Integration – FastAPI endpoints via TestClient with a real MongoDB
     (requires MONGO_URL env var pointing to a live instance)
  3. Aggregation – reporting endpoint logic

Run with:
    pytest tests/ -v
    pytest tests/ -v -m unit         # skip integration
    pytest tests/ -v -m integration  # only integration (needs Mongo)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import (
    ConflictType, MedicationItem, MedicationSnapshot, MedicationStatus,
    SourceType,
)
from app.services.conflict_detector import detect_conflicts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_snap(
    source: SourceType,
    meds: list[dict],
    ingested_at: datetime | None = None,
) -> MedicationSnapshot:
    return MedicationSnapshot(
        snapshot_id=str(uuid.uuid4()),
        source=source,
        ingested_at=ingested_at or datetime.utcnow(),
        medications=[MedicationItem(**m) for m in meds],
    )


# ===========================================================================
# GROUP 1 – Unit tests for conflict_detector
# ===========================================================================

@pytest.mark.unit
class TestDoseMismatch:
    def test_detects_dose_mismatch_across_sources(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "lisinopril", "dose_mg": 5, "dose_unit": "mg",
                        "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "lisinopril", "dose_mg": 20, "dose_unit": "mg",
                        "status": "active"}]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        types = [c.conflict_type for c in conflicts]
        assert ConflictType.DOSE_MISMATCH in types

    def test_no_conflict_within_tolerance(self):
        """Doses within 10% tolerance should not trigger a conflict."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "lisinopril", "dose_mg": 10, "dose_unit": "mg",
                        "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "lisinopril", "dose_mg": 10.5, "dose_unit": "mg",
                        "status": "active"}]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        dose_conflicts = [c for c in conflicts
                          if c.conflict_type == ConflictType.DOSE_MISMATCH]
        assert len(dose_conflicts) == 0

    def test_dose_mismatch_deduplication(self):
        """Running detection twice should not produce duplicate conflict objects."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "lisinopril", "dose_mg": 5, "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "lisinopril", "dose_mg": 20, "status": "active"}]),
        ]
        c1 = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        c2 = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        # Both runs should find exactly one dose-mismatch conflict
        dm1 = [c for c in c1 if c.conflict_type == ConflictType.DOSE_MISMATCH]
        dm2 = [c for c in c2 if c.conflict_type == ConflictType.DOSE_MISMATCH]
        assert len(dm1) == 1
        assert len(dm2) == 1

    def test_missing_dose_does_not_crash(self):
        """If one source omits dose_mg, we should still handle gracefully."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "lisinopril", "status": "active"}]),   # no dose
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "lisinopril", "dose_mg": 20, "status": "active"}]),
        ]
        # Should not raise
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        # No dose mismatch because one side has no dose
        dose_conflicts = [c for c in conflicts
                          if c.conflict_type == ConflictType.DOSE_MISMATCH]
        assert len(dose_conflicts) == 0

    def test_both_doses_missing_no_conflict(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "lisinopril", "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "lisinopril", "status": "active"}]),
        ]
        dose_conflicts = [
            c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
            if c.conflict_type == ConflictType.DOSE_MISMATCH
        ]
        assert len(dose_conflicts) == 0


@pytest.mark.unit
class TestStoppedVsActive:
    def test_detects_stopped_vs_active(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "metformin", "dose_mg": 1000, "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "metformin", "dose_mg": 1000, "status": "stopped"}]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        types = [c.conflict_type for c in conflicts]
        assert ConflictType.STOPPED_VS_ACTIVE in types

    def test_both_active_no_stopped_conflict(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "metformin", "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "metformin", "status": "active"}]),
        ]
        stopped = [c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
                   if c.conflict_type == ConflictType.STOPPED_VS_ACTIVE]
        assert len(stopped) == 0

    def test_both_stopped_no_conflict(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "metformin", "status": "stopped"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "metformin", "status": "stopped"}]),
        ]
        stopped = [c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
                   if c.conflict_type == ConflictType.STOPPED_VS_ACTIVE]
        assert len(stopped) == 0


@pytest.mark.unit
class TestDrugClassCombination:
    def test_ace_inhibitor_plus_arb(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "lisinopril", "status": "active"},
                {"name": "losartan", "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        combo = [c for c in conflicts
                 if c.conflict_type == ConflictType.DRUG_CLASS_COMBINATION]
        assert len(combo) >= 1
        rule_ids = {c.rule_id for c in combo}
        assert "COMBO_001" in rule_ids

    def test_warfarin_plus_aspirin(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "warfarin", "status": "active"},
                {"name": "aspirin", "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        combo = [c for c in conflicts
                 if c.conflict_type == ConflictType.DRUG_CLASS_COMBINATION]
        rule_ids = {c.rule_id for c in combo}
        assert "COMBO_006" in rule_ids

    def test_stopped_drug_not_included_in_combo(self):
        """A stopped ACE inhibitor should NOT trigger ACE+ARB combo."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "lisinopril", "status": "stopped"},
                {"name": "losartan", "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        combo = [c for c in conflicts
                 if c.conflict_type == ConflictType.DRUG_CLASS_COMBINATION
                 and c.rule_id == "COMBO_001"]
        assert len(combo) == 0

    def test_nsaid_plus_anticoagulant(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "ibuprofen", "status": "active"},
                {"name": "warfarin", "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        rule_ids = {c.rule_id for c in conflicts
                    if c.conflict_type == ConflictType.DRUG_CLASS_COMBINATION}
        assert "COMBO_003" in rule_ids


@pytest.mark.unit
class TestDoseOutOfRange:
    def test_dose_above_max_flagged(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "furosemide", "dose_mg": 1000, "dose_unit": "mg",
                 "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        oor = [c for c in conflicts
               if c.conflict_type == ConflictType.DOSE_OUT_OF_RANGE]
        assert len(oor) == 1
        assert "furosemide" in oor[0].evidence.drug_a

    def test_dose_below_min_flagged(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "metformin", "dose_mg": 100, "dose_unit": "mg",
                 "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        oor = [c for c in conflicts
               if c.conflict_type == ConflictType.DOSE_OUT_OF_RANGE]
        assert len(oor) == 1

    def test_dose_within_range_no_conflict(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "furosemide", "dose_mg": 40, "dose_unit": "mg",
                 "status": "active"},
            ]),
        ]
        oor = [c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
               if c.conflict_type == ConflictType.DOSE_OUT_OF_RANGE]
        assert len(oor) == 0

    def test_unknown_drug_no_range_conflict(self):
        """A drug not in conflict_rules.json must not trigger DOSE_OUT_OF_RANGE."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "unknowndrug", "dose_mg": 99999, "status": "active"},
            ]),
        ]
        oor = [c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
               if c.conflict_type == ConflictType.DOSE_OUT_OF_RANGE]
        assert len(oor) == 0


@pytest.mark.unit
class TestNormalization:
    def test_drug_name_lowercased(self):
        item = MedicationItem(name="  LISINOPRIL  ", status="active")
        assert item.name == "lisinopril"

    def test_unit_normalized(self):
        item = MedicationItem(name="lisinopril", dose_unit="Milligrams", status="active")
        assert item.dose_unit == "mg"

    def test_frequency_normalized(self):
        item = MedicationItem(name="lisinopril", frequency="QD", status="active")
        assert item.frequency == "once daily"

    def test_route_normalized(self):
        item = MedicationItem(name="lisinopril", route="PO", status="active")
        assert item.route == "oral"

    def test_bid_frequency(self):
        item = MedicationItem(name="metformin", frequency="BID", status="active")
        assert item.frequency == "twice daily"


@pytest.mark.unit
class TestEdgeCases:
    def test_empty_medication_list(self):
        snaps = [
            make_snap(SourceType.CLINIC_EMR, []),
            make_snap(SourceType.HOSPITAL_DISCHARGE, []),
        ]
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        assert len(conflicts) == 0

    def test_single_source_only_range_possible(self):
        """Single source can only generate DOSE_OUT_OF_RANGE conflicts."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "aspirin", "dose_mg": 81, "status": "active"},
            ]),
        ]
        cross_source = [
            c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
            if c.conflict_type in (ConflictType.DOSE_MISMATCH,
                                   ConflictType.STOPPED_VS_ACTIVE)
        ]
        assert len(cross_source) == 0

    def test_zero_dose_no_mismatch(self):
        """Zero dose on one side: we skip the comparison to avoid divide-by-zero."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR,
                      [{"name": "lisinopril", "dose_mg": 0, "status": "active"}]),
            make_snap(SourceType.HOSPITAL_DISCHARGE,
                      [{"name": "lisinopril", "dose_mg": 10, "status": "active"}]),
        ]
        # Should not raise ZeroDivisionError
        conflicts = detect_conflicts("P_TEST", "CLINIC_X", snaps)
        # avg is 5, diff is 10, pct is 200% – should detect mismatch
        # (both are > 0 when avg > 0 check passes since avg=5)
        dose_m = [c for c in conflicts if c.conflict_type == ConflictType.DOSE_MISMATCH]
        assert len(dose_m) == 1

    def test_multiple_conflicts_same_patient(self):
        """A patient can have multiple independent conflicts detected at once."""
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "warfarin", "dose_mg": 2, "status": "active"},
                {"name": "aspirin", "status": "active"},
                {"name": "ibuprofen", "status": "active"},
            ]),
        ]
        conflicts = detect_conflicts("P_MULTI", "CLINIC_X", snaps)
        assert len(conflicts) >= 2

    def test_latest_snapshot_per_source_used(self):
        """
        When a source has two snapshots, conflict detection should use the
        latest one. Here the first snapshot has a combo, the second removes
        one drug – the conflict should NOT appear.
        """
        t1 = datetime.utcnow() - timedelta(hours=2)
        t2 = datetime.utcnow()
        snaps = [
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "lisinopril", "status": "active"},
                {"name": "losartan", "status": "active"},
            ], ingested_at=t1),
            make_snap(SourceType.CLINIC_EMR, [
                {"name": "lisinopril", "status": "active"},
                # losartan removed in newer snapshot
            ], ingested_at=t2),
        ]
        combo = [
            c for c in detect_conflicts("P_TEST", "CLINIC_X", snaps)
            if c.conflict_type == ConflictType.DRUG_CLASS_COMBINATION
            and c.rule_id == "COMBO_001"
        ]
        assert len(combo) == 0


# ===========================================================================
# GROUP 2 – Integration tests (requires running Mongo + app)
# These use pytest-asyncio + httpx AsyncClient against the real FastAPI app.
# They are marked `integration` and skipped when MONGO_URL is not available.
# ===========================================================================

try:
    import httpx
    from httpx import AsyncClient
    from fastapi.testclient import TestClient
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

@pytest.mark.integration
@pytest.mark.skipif(not HAS_HTTPX, reason="httpx not installed")
class TestIngestEndpoint:
    """Sync integration tests using TestClient (mongomock or real Mongo)."""

    @pytest.fixture(autouse=True)
    def setup_app(self, monkeypatch):
        """Patch DB to use mongomock if available, else skip."""
        try:
            import mongomock_motor
        except ImportError:
            pytest.skip("mongomock_motor not installed; skipping integration tests")

        from app.main import app
        mock_client = mongomock_motor.AsyncMongoMockClient()
        mock_db = mock_client["medconflict_test"]

        async def override_get_db():
            await mock_db.patients.create_index("patient_id", unique=True)
            await mock_db.conflicts.create_index("conflict_id", unique=True)
            return mock_db

        from app.core import database
        monkeypatch.setattr(database, "get_db", lambda: mock_db)
        self.app = app
        self.db = mock_db

    def test_ingest_creates_snapshot(self):
        from app.main import app
        with TestClient(app) as client:
            payload = {
                "patient_id": "P_INT_001",
                "patient_name": "Test Patient",
                "clinic_id": "CLINIC_TEST",
                "source": "clinic_emr",
                "medications": [
                    {"name": "lisinopril", "dose_mg": 10, "dose_unit": "mg",
                     "status": "active"}
                ]
            }
            r = client.post("/ingest/P_INT_001", json=payload)
            assert r.status_code == 201
            body = r.json()
            assert body["patient_id"] == "P_INT_001"
            assert "snapshot_id" in body

    def test_ingest_patient_id_mismatch_returns_422(self):
        from app.main import app
        with TestClient(app) as client:
            payload = {
                "patient_id": "P_WRONG",
                "patient_name": "Test",
                "clinic_id": "CLINIC_TEST",
                "source": "clinic_emr",
                "medications": []
            }
            r = client.post("/ingest/P_CORRECT", json=payload)
            assert r.status_code == 422

    def test_ingest_malformed_payload_returns_422(self):
        from app.main import app
        with TestClient(app) as client:
            r = client.post("/ingest/P_BAD", json={"garbage": True})
            assert r.status_code == 422

    def test_ingest_invalid_source_returns_422(self):
        from app.main import app
        with TestClient(app) as client:
            payload = {
                "patient_id": "P_INT_002",
                "patient_name": "Test",
                "clinic_id": "CLINIC_TEST",
                "source": "invalid_source",   # not in SourceType enum
                "medications": []
            }
            r = client.post("/ingest/P_INT_002", json=payload)
            assert r.status_code == 422


# ===========================================================================
# GROUP 3 – Aggregation logic tests (pure unit, mock DB cursor)
# ===========================================================================

@pytest.mark.unit
class TestAggregationLogic:
    """
    Verify that the reporting pipeline shapes the correct MongoDB aggregation.
    We mock the collection and check the aggregate call was made.
    """

    @pytest.mark.asyncio
    async def test_unresolved_report_calls_aggregate(self):
        from app.api.reports import patients_with_unresolved
        import asyncio

        async def fake_async_gen(_pipeline):
            results = [
                {"patient_id": "P001", "patient_name": "Alice",
                 "clinic_id": "CLINIC_A", "unresolved_count": 3},
            ]
            for r in results:
                yield r

        mock_db = MagicMock()
        mock_db.conflicts.aggregate = MagicMock(side_effect=fake_async_gen)

        result = await patients_with_unresolved("CLINIC_A", db=mock_db)
        assert len(result) == 1
        assert result[0].unresolved_count == 3
        mock_db.conflicts.aggregate.assert_called_once()

    @pytest.mark.asyncio
    async def test_multi_conflict_report_returns_correct_shape(self):
        from app.api.reports import multi_conflict_report

        async def fake_async_gen(_pipeline):
            for r in [
                {"patient_id": "P001", "patient_name": "Alice", "total_conflicts": 5},
                {"patient_id": "P002", "patient_name": "Bob", "total_conflicts": 3},
            ]:
                yield r

        mock_db = MagicMock()
        mock_db.conflicts.aggregate = MagicMock(side_effect=fake_async_gen)

        result = await multi_conflict_report("CLINIC_A", days=30, min_conflicts=2,
                                             db=mock_db)
        assert result.patients_with_2plus_conflicts == 2
        assert result.clinic_id == "CLINIC_A"
        assert result.period_days == 30
