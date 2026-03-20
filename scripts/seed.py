#!/usr/bin/env python3
"""
Seed script – generates 15 synthetic patients with varied conflicts and
POSTs them to the running MedConflict service.

Usage:
    python scripts/seed.py [--base-url http://localhost:8000]

Conflict scenarios seeded
--------------------------
 1. Dose mismatch       – lisinopril 5 mg (clinic) vs 20 mg (hospital)
 2. Stopped-vs-active   – metformin stopped in hospital, active in clinic
 3. ACE + ARB combo     – lisinopril + losartan (COMBO_001)
 4. Anticoag + anti-plt – warfarin + aspirin (COMBO_006)
 5. NSAID + anticoag    – ibuprofen + warfarin (COMBO_003)
 6. Dose out of range   – furosemide 1000 mg (above 600 max)
 7. Three-way mismatch  – amlodipine 5/10/7.5 mg across 3 sources
 8. Clean patient       – no conflicts
 9-15: Mixed / realistic chronic-care profiles
"""

import argparse
import json
import sys
import time
from typing import Any

try:
    import httpx
except ImportError:
    print("httpx not installed; run: pip install httpx")
    sys.exit(1)

BASE_URL = "http://localhost:8000"


def post_ingest(client: httpx.Client, patient_id: str, payload: dict) -> dict:
    r = client.post(f"{BASE_URL}/ingest/{patient_id}", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


PATIENTS: list[dict[str, Any]] = [
    # ── 1 Dose mismatch: lisinopril ────────────────────────────────────────
    {
        "id": "P001", "name": "Alice Fernandez", "dob": "1958-03-12",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_jones",
                "medications": [
                    {"name": "lisinopril", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "furosemide", "dose_mg": 40, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "hospital_discharge", "submitted_by": "dr_patel",
                "medications": [
                    {"name": "lisinopril", "dose_mg": 20, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "furosemide", "dose_mg": 40, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 2 Stopped-vs-active: metformin ─────────────────────────────────────
    {
        "id": "P002", "name": "Bernard Okafor", "dob": "1965-07-22",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_jones",
                "medications": [
                    {"name": "metformin", "dose_mg": 1000, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "active"},
                    {"name": "atorvastatin", "dose_mg": 40, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "hospital_discharge", "submitted_by": "dr_kim",
                "medications": [
                    {"name": "metformin", "dose_mg": 1000, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "stopped"},
                    {"name": "atorvastatin", "dose_mg": 40, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 3 ACE inhibitor + ARB (COMBO_001) ──────────────────────────────────
    {
        "id": "P003", "name": "Carmen Osei", "dob": "1972-11-05",
        "clinic": "CLINIC_DIALYSIS_B",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "dr_chen",
                "medications": [
                    {"name": "lisinopril", "dose_mg": 10, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "losartan", "dose_mg": 50, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "amlodipine", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 4 Warfarin + aspirin (COMBO_006) ───────────────────────────────────
    {
        "id": "P004", "name": "David Nguyen", "dob": "1950-01-30",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_jones",
                "medications": [
                    {"name": "warfarin", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "aspirin", "dose_mg": 81, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "patient_reported", "submitted_by": "intake_clerk",
                "medications": [
                    {"name": "warfarin", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "aspirin", "dose_mg": 81, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "omeprazole", "dose_mg": 20, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 5 NSAID + anticoagulant (COMBO_003) ────────────────────────────────
    {
        "id": "P005", "name": "Esther Makinde", "dob": "1960-09-18",
        "clinic": "CLINIC_DIALYSIS_B",
        "sources": [
            {
                "source": "hospital_discharge", "submitted_by": "dr_white",
                "medications": [
                    {"name": "warfarin", "dose_mg": 3, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "ibuprofen", "dose_mg": 400, "dose_unit": "mg",
                     "frequency": "three times daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 6 Dose out of range: furosemide 1000 mg ────────────────────────────
    {
        "id": "P006", "name": "Frank Adeyemi", "dob": "1955-05-04",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_lee",
                "medications": [
                    {"name": "furosemide", "dose_mg": 1000, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "spironolactone", "dose_mg": 25, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 7 Three-way dose mismatch: amlodipine ──────────────────────────────
    {
        "id": "P007", "name": "Grace Huang", "dob": "1963-12-27",
        "clinic": "CLINIC_DIALYSIS_C",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "dr_kim",
                "medications": [
                    {"name": "amlodipine", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "hospital_discharge", "submitted_by": "dr_brown",
                "medications": [
                    {"name": "amlodipine", "dose_mg": 10, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "patient_reported", "submitted_by": "intake_clerk",
                "medications": [
                    {"name": "amlodipine", "dose_mg": 7.5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 8 Clean patient (no conflicts) ─────────────────────────────────────
    {
        "id": "P008", "name": "Henry Larsson", "dob": "1980-06-15",
        "clinic": "CLINIC_DIALYSIS_C",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_jones",
                "medications": [
                    {"name": "omeprazole", "dose_mg": 20, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "atorvastatin", "dose_mg": 20, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 9 Multiple conflicts: ACE+ARB + dose mismatch ──────────────────────
    {
        "id": "P009", "name": "Isabelle Dupont", "dob": "1953-04-08",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "dr_chen",
                "medications": [
                    {"name": "lisinopril", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "losartan", "dose_mg": 25, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "carvedilol", "dose_mg": 6.25, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "hospital_discharge", "submitted_by": "dr_patel",
                "medications": [
                    {"name": "lisinopril", "dose_mg": 20, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "losartan", "dose_mg": 50, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "carvedilol", "dose_mg": 6.25, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 10 NSAID + ACE inhibitor + loop diuretic ───────────────────────────
    {
        "id": "P010", "name": "James Omondi", "dob": "1969-08-14",
        "clinic": "CLINIC_DIALYSIS_B",
        "sources": [
            {
                "source": "patient_reported", "submitted_by": "intake_clerk",
                "medications": [
                    {"name": "naproxen", "dose_mg": 500, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "active"},
                    {"name": "lisinopril", "dose_mg": 10, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "furosemide", "dose_mg": 80, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 11 Dose mismatch + stopped-vs-active ───────────────────────────────
    {
        "id": "P011", "name": "Keiko Yamamoto", "dob": "1948-02-19",
        "clinic": "CLINIC_DIALYSIS_C",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_lee",
                "medications": [
                    {"name": "warfarin", "dose_mg": 2, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "cinacalcet", "dose_mg": 30, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "hospital_discharge", "submitted_by": "dr_white",
                "medications": [
                    {"name": "warfarin", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "cinacalcet", "dose_mg": 30, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "stopped"},
                ]
            },
        ]
    },
    # ── 12 Dialysis-specific combo (sevelamer, cinacalcet) – clean ─────────
    {
        "id": "P012", "name": "Liam O'Brien", "dob": "1961-10-31",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "dr_chen",
                "medications": [
                    {"name": "sevelamer", "dose_mg": 800, "dose_unit": "mg",
                     "frequency": "three times daily", "route": "oral", "status": "active"},
                    {"name": "cinacalcet", "dose_mg": 60, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "omeprazole", "dose_mg": 20, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 13 Anticoag + antiplatelet (apixaban + clopidogrel) ────────────────
    {
        "id": "P013", "name": "Maria Santos", "dob": "1956-03-25",
        "clinic": "CLINIC_DIALYSIS_B",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "nurse_jones",
                "medications": [
                    {"name": "apixaban", "dose_mg": 5, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "active"},
                    {"name": "clopidogrel", "dose_mg": 75, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "atorvastatin", "dose_mg": 40, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
        ]
    },
    # ── 14 Insulin dose mismatch ───────────────────────────────────────────
    {
        "id": "P014", "name": "Nadia Petrov", "dob": "1967-07-07",
        "clinic": "CLINIC_DIALYSIS_C",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "dr_kim",
                "medications": [
                    {"name": "insulin glargine", "dose_mg": 20, "dose_unit": "units",
                     "frequency": "once daily", "route": "sc", "status": "active"},
                ]
            },
            {
                "source": "patient_reported", "submitted_by": "intake_clerk",
                "medications": [
                    {"name": "insulin glargine", "dose_mg": 40, "dose_unit": "units",
                     "frequency": "once daily", "route": "sc", "status": "active"},
                ]
            },
        ]
    },
    # ── 15 Complex multi-conflict patient ──────────────────────────────────
    {
        "id": "P015", "name": "Omar Al-Rashid", "dob": "1945-12-01",
        "clinic": "CLINIC_DIALYSIS_A",
        "sources": [
            {
                "source": "clinic_emr", "submitted_by": "dr_chen",
                "medications": [
                    {"name": "warfarin", "dose_mg": 4, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "aspirin", "dose_mg": 81, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "ibuprofen", "dose_mg": 400, "dose_unit": "mg",
                     "frequency": "twice daily", "route": "oral", "status": "active"},
                    {"name": "lisinopril", "dose_mg": 10, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                ]
            },
            {
                "source": "hospital_discharge", "submitted_by": "dr_patel",
                "medications": [
                    {"name": "warfarin", "dose_mg": 6, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "aspirin", "dose_mg": 81, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "active"},
                    {"name": "lisinopril", "dose_mg": 10, "dose_unit": "mg",
                     "frequency": "once daily", "route": "oral", "status": "stopped"},
                ]
            },
        ]
    },
]


def build_payload(patient: dict, source_entry: dict) -> dict:
    return {
        "patient_id": patient["id"],
        "patient_name": patient["name"],
        "date_of_birth": patient.get("dob"),
        "clinic_id": patient["clinic"],
        "source": source_entry["source"],
        "submitted_by": source_entry.get("submitted_by"),
        "medications": source_entry["medications"],
    }


def main():
    global BASE_URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    BASE_URL = args.base_url

    with httpx.Client(base_url=BASE_URL) as client:
        total_snapshots = 0
        total_conflicts = 0
        for patient in PATIENTS:
            for src in patient["sources"]:
                payload = build_payload(patient, src)
                try:
                    result = post_ingest(client, patient["id"], payload)
                    total_snapshots += 1
                    total_conflicts += result.get("new_conflicts", 0)
                    print(
                        f"[OK] {patient['id']} {patient['name']:<20} "
                        f"{src['source']:<25} "
                        f"+{result['new_conflicts']} conflicts"
                    )
                except httpx.HTTPStatusError as e:
                    print(f"[ERR] {patient['id']} {src['source']}: {e.response.text}")
                time.sleep(0.05)

        print(f"\n✓ Seeded {total_snapshots} snapshots, {total_conflicts} total conflicts.")


if __name__ == "__main__":
    main()