# MedConflict — MongoDB Schema & Design Decisions

## Collections

### `patients`

Stores the longitudinal record for each patient, including every versioned
medication snapshot that has ever been ingested.

```json
{
  "_id": ObjectId,
  "patient_id": "P001",                      // application-level UUID
  "name": "Alice Fernandez",
  "date_of_birth": "1958-03-12",
  "clinic_id": "CLINIC_DIALYSIS_A",
  "created_at": ISODate,
  "updated_at": ISODate,

  "snapshots": [                              // append-only version array
    {
      "snapshot_id": "uuid-v4",
      "source": "clinic_emr",                 // enum: clinic_emr | hospital_discharge | patient_reported
      "ingested_at": ISODate,
      "ingested_by": "nurse_jones",
      "raw_payload_hash": "sha256hex",        // dedup aid

      "medications": [
        {
          "name":       "lisinopril",         // normalised to lowercase
          "dose_mg":    10.0,
          "dose_unit":  "mg",                 // normalised (Milligrams → mg)
          "frequency":  "once daily",         // normalised (QD → once daily)
          "route":      "oral",               // normalised (PO → oral)
          "status":     "active",             // active | stopped | hold
          "prescriber": "dr_patel",
          "raw_name":   "Lisinopril 10MG"     // original string preserved
        }
      ]
    }
    // … more snapshots, ordered by ingested_at ascending
  ]
}
```

**Indexes**

| Field(s)                   | Type   | Rationale |
|----------------------------|--------|-----------|
| `patient_id`               | unique | Primary lookup; every ingest and conflict query uses this. |
| `clinic_id`                | single | Filter all patients in a clinic for reporting. |

---

### `conflicts`

Stores every detected conflict as an independent, auditable document.
Resolution only touches this collection, never the patient record.

```json
{
  "_id": ObjectId,
  "conflict_id": "uuid-v4",

  "patient_id": "P001",
  "clinic_id":  "CLINIC_DIALYSIS_A",

  "conflict_type": "dose_mismatch",
  // dose_mismatch | drug_class_combination | stopped_vs_active | dose_out_of_range

  "severity": "medium",              // low | medium | high
  "status":   "unresolved",         // unresolved | resolved | dismissed
  "rule_id":  "COMBO_001",          // null for cross-source dose/stopped rules

  "evidence": {
    "source_a":  "snapshot-uuid-1",  // snapshot where drug_a was seen
    "source_b":  "snapshot-uuid-2",  // null for single-source rules
    "drug_a":    "lisinopril",
    "drug_b":    "losartan",         // null for single-drug rules
    "detail":    "ACE inhibitor + ARB dual blockade: increased risk of …"
  },

  "detected_at": ISODate,

  "resolution": null
  // OR when resolved / dismissed:
  // {
  //   "resolved_by":    "dr_chen",
  //   "resolved_at":    ISODate,
  //   "reason":         "Intentional combination, patient monitored weekly",
  //   "chosen_source":  "clinic_emr"   // optional: which source to trust
  // }
}
```

**Indexes**

| Field(s)                                       | Type     | Rationale |
|------------------------------------------------|----------|-----------|
| `conflict_id`                                  | unique   | Direct fetch by ID (resolve / dismiss endpoints). |
| `patient_id` + `status`                        | compound | "All unresolved conflicts for patient X" — the most common per-patient read. |
| `clinic_id` + `status` + `detected_at` (desc) | compound | "Unresolved in clinic X within date range" — primary reporting query. |
| `detected_at` (desc)                           | single   | 30-day window scan for the multi-conflict report. |

---

## Versioning Policy

**A new snapshot is always appended on every ingest**, even if the payload
is byte-for-byte identical to the previous submission from the same source.

_Rationale:_ We cannot distinguish an intentional re-confirmation ("yes, the
list is still the same") from an accidental re-submission at ingestion time.
Keeping all submissions creates a complete chronological log. If storage
growth is a concern, a background archival job can move snapshots older than
N months to a cold collection while leaving the latest-per-source in the hot
document.

---

## Conflict Deduplication

Conflicts are deduplicated **at write time** (not at detection time across
calls). The dedup key is:

```
(patient_id, conflict_type, evidence.drug_a, evidence.drug_b, rule_id)
    WHERE status = 'unresolved'
```

A new conflict document is only inserted if no matching UNRESOLVED record
already exists. `rule_id` is part of the key because a single drug pair can
legitimately trigger multiple rules (e.g., warfarin+aspirin triggers both
COMBO_002 "anticoagulant+antiplatelet" and COMBO_006 "warfarin+aspirin
specific" — both carry distinct clinical guidance and should appear
separately for clinician review).

---

## "Resolved" vs "Unresolved" Representation

| Field        | Values                      |
|--------------|-----------------------------|
| `status`     | `unresolved` / `resolved` / `dismissed` |
| `resolution` | `null` when unresolved; full object otherwise |

**`resolved`** — clinician reviewed the conflict and took action (e.g.,
changed the dose, stopped a drug, or explicitly chose a trusted source).
`chosen_source` records which source was deemed authoritative.

**`dismissed`** — clinician reviewed and decided no action is needed
(e.g., "ACE+ARB combination is intentional, patient on close monitoring").
Dismissed conflicts do not count in "unresolved" reporting queries.

There is **no single "truth source"**. Each conflict resolution carries a
`reason` and optionally a `chosen_source`, but the system does not
automatically update the medication record. Resolution intent is recorded
for audit; reconciliation of the underlying med list is a clinical workflow
responsibility.

---

## Denormalization vs References: Trade-offs

### Snapshots embedded in `patients`

✅ **Pro:** A single document fetch returns the full history. Snapshot reads
never require a join.

⚠️ **Con:** Document grows with every ingest. For a patient ingesting from 3
sources weekly over 5 years that is ~780 snapshots. Each snapshot can be
~2 KB → ~1.5 MB per patient, well inside MongoDB's 16 MB limit. Archival
jobs should be planned beyond the 3–5 year mark.

### Conflicts in a separate collection

✅ **Pro:** Conflict documents are independently writable (resolution updates
touch only the conflict). Aggregation queries over all conflicts in a clinic
do not require loading patient documents. The collection can be indexed
independently without widening patient documents.

⚠️ **Con:** Reporting queries that need both patient name and conflict counts
require a `$lookup` (join). Mitigated by keeping `clinic_id` denormalised
inside each conflict document so clinic-level reports never need to touch
`patients` at all.

### `clinic_id` denormalised onto `conflicts`

Repeating `clinic_id` (already available via `patient.clinic_id`) avoids the
`$lookup` to `patients` for the two primary reporting queries. The cost is a
small amount of redundancy; updates to a patient's clinic assignment would
require patching all their conflicts — acceptable since clinic transfers are
infrequent and should generate a fresh ingest anyway.

---

## API Surface Summary

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest/{patient_id}` | Ingest medication list from one source |
| `GET`  | `/conflicts/patient/{patient_id}` | List conflicts for a patient |
| `PATCH`| `/conflicts/{conflict_id}/resolve` | Mark conflict resolved |
| `PATCH`| `/conflicts/{conflict_id}/dismiss` | Dismiss a conflict |
| `GET`  | `/reports/clinic/{clinic_id}/unresolved` | Patients with ≥1 unresolved conflict |
| `GET`  | `/reports/clinic/{clinic_id}/multi-conflict?days=30&min_conflicts=2` | Patients with ≥N conflicts in window |
| `GET`  | `/reports/summary?days=30` | Cross-clinic conflict summary |
