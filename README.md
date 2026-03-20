# MedConflict 💊

> Medication conflict detection service for chronic-care patients.  
> Ingests medication lists from multiple sources, detects dangerous conflicts, and surfaces them for clinician review.

---

## Quick Start (under 5 minutes)

### Prerequisites
- Python 3.10+
- MongoDB running locally on port 27017

### Clone → Install → Run

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/medconflict.git
cd medconflict

# 2. Create virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# Mac/Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start MongoDB (if not already running)
# Windows:  net start MongoDB
# Mac:      brew services start mongodb-community
# Linux:    sudo systemctl start mongod

# 5. Run the server
uvicorn app.main:app --reload
```

Visit **http://localhost:8000/docs** — the interactive API is ready.

### Seed test data (optional but recommended)

Open a second terminal, activate `.venv`, then:

```bash
python scripts/seed.py
```

This creates **15 synthetic patients** across 3 clinics with **7 conflict scenarios**.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT / CALLER                          │
│              (Hospital Dashboard, EMR System, etc.)             │
└────────────────────────────┬────────────────────────────────────┘
                             │  HTTP requests
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                        FASTAPI SERVICE                          │
│                                                                 │
│   ┌─────────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│   │  POST /ingest   │  │ GET/PATCH    │  │  GET /reports   │  │
│   │  /{patient_id}  │  │ /conflicts   │  │  /clinic/...    │  │
│   └────────┬────────┘  └──────┬───────┘  └────────┬────────┘  │
│            │                  │                    │            │
│            ▼                  ▼                    ▼            │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │                    SERVICE LAYER                        │  │
│   │                                                         │  │
│   │  ┌──────────────────┐    ┌───────────────────────────┐ │  │
│   │  │  ingestion.py    │    │   conflict_detector.py    │ │  │
│   │  │                  │───▶│                           │ │  │
│   │  │ • Upsert patient │    │ • Dose mismatch           │ │  │
│   │  │ • Append snapshot│    │ • Stopped vs Active       │ │  │
│   │  │ • Dedup conflicts│    │ • Drug class combos       │ │  │
│   │  │ • Persist to DB  │    │ • Dose out of range       │ │  │
│   │  └──────────────────┘    └───────────────────────────┘ │  │
│   └─────────────────────────────────────────────────────────┘  │
│            │                                                     │
│            ▼                                                     │
│   ┌─────────────────────────┐   ┌──────────────────────────┐   │
│   │   conflict_rules.json   │   │       database.py        │   │
│   │                         │   │   (Motor async driver)   │   │
│   │ • Safe dose ranges      │   └────────────┬─────────────┘   │
│   │ • Blacklisted combos    │                │                   │
│   │ • Drug class mappings   │                │                   │
│   └─────────────────────────┘                │                   │
└──────────────────────────────────────────────┼──────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                          MONGODB                                │
│                                                                 │
│   ┌──────────────────────────┐  ┌───────────────────────────┐  │
│   │     patients             │  │       conflicts           │  │
│   │                          │  │                           │  │
│   │ • patient_id (unique)    │  │ • conflict_id (unique)    │  │
│   │ • clinic_id              │  │ • patient_id + status     │  │
│   │ • snapshots[] (versions) │  │ • clinic_id + detected_at │  │
│   │   └─ source              │  │ • evidence {}             │  │
│   │   └─ ingested_at         │  │ • resolution {}           │  │
│   │   └─ medications[]       │  │ • status: unresolved/     │  │
│   │                          │  │          resolved/        │  │
│   │                          │  │          dismissed        │  │
│   └──────────────────────────┘  └───────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow — Single Ingest

```
Caller sends medication list
        │
        ▼
Normalize all fields
(lowercase names, mg units, QD→once daily)
        │
        ▼
Upsert patient document
Append new snapshot (always — never overwrite)
        │
        ▼
Load all snapshots for this patient
        │
        ▼
Run 4 conflict detection rules
        │
   ┌────┴────┐
   │         │
New?       Exists in DB
   │       (skip — dedup)
   ▼
Insert conflict document
        │
        ▼
Return { snapshot_id, new_conflicts_count }
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/ingest/{patient_id}` | Ingest a medication list from one source |
| `GET` | `/conflicts/patient/{patient_id}` | List all conflicts for a patient |
| `PATCH` | `/conflicts/{conflict_id}/resolve` | Mark a conflict as resolved |
| `PATCH` | `/conflicts/{conflict_id}/dismiss` | Dismiss a conflict |
| `GET` | `/reports/clinic/{clinic_id}/unresolved` | Patients with ≥1 unresolved conflict |
| `GET` | `/reports/clinic/{clinic_id}/multi-conflict` | Patients with ≥N conflicts in N days |
| `GET` | `/reports/summary` | Cross-clinic conflict summary |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Interactive Swagger UI |

---

## Conflict Detection Rules

### 1. Dose Mismatch
Same drug appears in 2+ sources with doses differing by more than **10%**.

```
Clinic EMR:          lisinopril 5mg   ←─┐
Hospital Discharge:  lisinopril 20mg  ←─┘  CONFLICT: 300% difference
```

### 2. Stopped vs Active
Drug marked as `stopped` in one source but `active` in another.

```
Clinic EMR:          metformin → active   ←─┐
Hospital Discharge:  metformin → stopped  ←─┘  CONFLICT
```

### 3. Drug Class Combination
Active medications contain a blacklisted class pairing (defined in `conflict_rules.json`).

| Rule | Classes | Severity |
|------|---------|----------|
| COMBO_001 | ACE Inhibitor + ARB | High |
| COMBO_002 | Anticoagulant + Antiplatelet | High |
| COMBO_003 | NSAID + Anticoagulant | High |
| COMBO_004 | NSAID + ACE Inhibitor | Medium |
| COMBO_005 | NSAID + Loop Diuretic | Medium |
| COMBO_006 | Warfarin + Aspirin (specific) | High |

### 4. Dose Out of Range
Dose falls outside the known-safe range for that drug.

```
furosemide 1000mg  →  CONFLICT: safe range is [20–600mg]
```

---

## Project Structure

```
medconflict/
├── app/
│   ├── main.py                  # FastAPI app entry point
│   ├── core/database.py         # MongoDB connection + indexes
│   ├── models/schemas.py        # Pydantic models
│   ├── services/
│   │   ├── conflict_detector.py # All 4 conflict rules
│   │   └── ingestion.py         # Snapshot persistence
│   └── api/
│       ├── ingestion.py         # POST /ingest
│       ├── conflicts.py         # Conflict endpoints
│       └── reports.py           # Reporting/aggregation
├── data/
│   └── conflict_rules.json      # Static rules (dose ranges, combos)
├── scripts/
│   └── seed.py                  # 15 synthetic patients
├── tests/
│   ├── run_tests.py             # Stdlib runner — no deps needed
│   └── test_conflicts.py        # pytest suite
├── requirements.txt
└── SCHEMA.md                    # Full DB schema documentation
```

---

## Running Tests

### No dependencies needed (recommended)
```bash
python tests/run_tests.py
```
Expected: `Results: 38/38 passed`

### With pytest (after pip install)
```bash
pytest tests/test_conflicts.py -v -m unit
```

### What is tested
| Group | Tests |
|-------|-------|
| Dose Mismatch | 6 tests — detection, tolerance, missing fields, dedup |
| Stopped vs Active | 3 tests — both directions, both stopped |
| Drug Class Combos | 5 tests — each combo rule, stopped drug exclusion |
| Dose Out of Range | 4 tests — above max, below min, within range, unknown drug |
| Edge Cases | 5 tests — empty list, single source, zero dose, latest snapshot logic |
| Normalisation | 5 tests — name, unit, frequency, route |
| Aggregation Logic | 10 tests — unresolved filter, 30-day window, cross-clinic summary |

---

## Assumptions and Trade-offs

### Assumptions
- **No single truth source** — conflicts are flagged for clinician review. The system never automatically picks a winner.
- **Clinic does not change** — a patient belongs to one clinic. Cross-clinic transfers would require a new ingest with the new clinic_id.
- **Drug names are stable after normalization** — `"Lisinopril"` and `"lisinopril"` are the same drug. Brand names vs generics are not resolved (e.g., `"Prinivil"` would not match `"lisinopril"`).
- **10% dose tolerance** is a reasonable clinical threshold to avoid noise from rounding differences.

### Trade-offs

| Decision | Choice | Trade-off |
|----------|--------|-----------|
| Snapshot storage | Embedded in patient document | Fast single-fetch history vs. document growth over years |
| Conflicts storage | Separate collection | Clean aggregation queries vs. requires `$lookup` for patient name |
| `clinic_id` on conflicts | Denormalized (repeated) | Avoids join on hot reporting queries vs. small redundancy |
| Versioning | Always append | Full audit trail vs. storage growth |
| Conflict dedup key | Includes `rule_id` | Multiple rules can fire on same drug pair vs. slightly more conflicts surfaced |

---

## Known Limitations

| Limitation | What I Would Do Next |
|------------|----------------------|
| Brand name ≠ generic not resolved | Integrate RxNorm API to map brand names to canonical drug names |
| No authentication | Add JWT / API key middleware |
| No alerting | Add webhook or email trigger when high-severity conflict is created |
| Single clinic per patient | Support clinic transfer events with history preserved |
| No frontend | Build a React dashboard showing conflict queue per clinic |
| Snapshots grow unbounded | Add background archival job — move snapshots older than 1 year to cold collection |
| Drug class rules are static JSON | Replace with live drug interaction API (e.g., DrugBank, OpenFDA) |
| No pagination on list endpoints | Add `skip` / `limit` query parameters |

---

## Seed Data — Conflict Scenarios

| Patient | Conflict Type | Drugs Involved |
|---------|--------------|----------------|
| Alice Fernandez | Dose mismatch | Lisinopril 5mg vs 20mg |
| Bernard Okafor | Stopped vs Active | Metformin stopped in hospital, active in clinic |
| Carmen Osei | ACE + ARB combo | Lisinopril + Losartan |
| David Nguyen | Warfarin + Aspirin | COMBO_006 |
| Esther Makinde | NSAID + Anticoagulant | Ibuprofen + Warfarin |
| Frank Adeyemi | Dose out of range | Furosemide 1000mg |
| Grace Huang | Three-way dose mismatch | Amlodipine 5 / 10 / 7.5mg |
| Henry Larsson | Clean patient | No conflicts |
| + 7 more | Mixed / realistic | Various combinations |
