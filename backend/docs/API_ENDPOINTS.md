# Samni Labs — Backend API Endpoints

Reference for the Flutter developer (and any backend contributor). Every public endpoint, with example request/response and the order Flutter calls them in.

**Base URL:**
- Local dev: `http://localhost:8001` (or whatever port `uvicorn` runs on)
- Production: TBD — typically `https://api.samnilabs.com`

**Interactive docs:** while the app is running, visit `/docs` — FastAPI renders a live Swagger UI from the same code that serves these endpoints. The `/openapi.json` path returns the machine-readable OpenAPI 3 spec; Flutter can generate a Dart client directly from it.

---

## Authentication

Every endpoint except `GET /health` requires:

```
Authorization: Bearer <JWT>
```

The JWT is issued by AWS Cognito. Flutter authenticates the user **directly with Cognito** (using the `amazon_cognito_identity_dart_2` SDK) and includes the resulting JWT on every backend call. The backend verifies the signature, the issuer, the audience / client_id, and the expiry on each request.

### Dev bypass
When `ENVIRONMENT=dev`, the backend also accepts the literal token `dev-test-token` and returns a synthetic user. This is **rejected** outside dev. See [auth.py](../backend/auth.py).

---

## Common conventions

- **Patient ID = ACES ID.** Always 9 digits, e.g. `000333000`. Validated everywhere (request bodies, URL paths, SQS messages).
- **Async work returns 202.** `POST /intake` and `POST /reassessment` queue jobs and return immediately. Flutter polls `GET /patient/{id}/status` or waits for the SNS push.
- **Errors use a uniform envelope:**
  ```json
  {
    "error": "short machine-readable code",
    "message": "human-readable description",
    "trace_id": "uuid for correlating with backend logs"
  }
  ```
- **Common HTTP statuses:** `200 OK`, `201 Created`, `202 Accepted`, `401 Unauthorized`, `404 Not Found`, `409 Conflict`, `413 Payload Too Large`, `422 Unprocessable Entity`, `503 Service Unavailable`.

---

# Endpoint catalogue

## 1. `GET /health`

**Auth:** none.

**Purpose:** Liveness + readiness probe for load balancers and uptime monitors.

**Response 200:**
```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "dev"
}
```
`status` is `"degraded"` when DB or AWS connectivity is missing — the endpoint still returns 200 so the LB does not pull the container from service.

**curl:**
```bash
curl http://localhost:8001/health
```

---

## 2. `GET /auth/me`

**Auth:** JWT required.

**Purpose:** Return the authenticated caller's identity for UI display ("Logged in as Cynthia").

**Response 200:**
```json
{
  "id": "cognito-uuid",
  "email": "cynthia@facility.example",
  "role": "caregiver",
  "facility_id": "facility-001"
}
```

---

## 3. `POST /patients`

**Auth:** JWT required.

**Purpose:** Create a new resident record. Caregiver enters the ACES ID + display name. The `assessment` JSON starts empty; later it gets populated by an intake or reassessment run, or directly by `PATCH /patients/{id}`.

**Request body:**
```json
{
  "patient_id": "000333000",
  "preferred_name": "Peter",
  "facility_id": "facility-001"
}
```

`facility_id` is optional. Defaults to the caller's facility. A caller cannot create a patient in another facility.

**Response 201:** the newly created patient (same shape as `GET /patients/{patient_id}`).

**Errors:**
- `409 Conflict` — patient with that ACES ID already exists.
- `409 Conflict` — patient exists but is archived (must be un-archived through admin tooling, not re-created).
- `403 Forbidden` — caller tried to create in a different facility.

---

## 4. `GET /patients`

**Auth:** JWT required, scoped to caller's facility.

**Purpose:** Paginated list of residents — used to populate the home screen.

**Query parameters:**
- `limit` — page size, default 50, max 200.
- `offset` — pagination offset, default 0.

**Response 200:**
```json
{
  "items": [
    {
      "patient_id": "000333000",
      "preferred_name": "Peter",
      "facility_id": "facility-001",
      "updated_at": "2026-04-25T10:05:00Z",
      "latest_run_status": "complete"
    }
  ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

The list is **slim** by design — no full assessment JSON. Flutter calls `GET /patients/{patient_id}` to load the full record.

Archived residents are excluded.

---

## 5. `GET /patients/{patient_id}`

**Auth:** JWT required, scoped to caller's facility.

**Purpose:** Full resident record — used by Flutter when the caregiver opens a patient.

**Response 200:**
```json
{
  "patient_id": "000333000",
  "preferred_name": "Peter",
  "facility_id": "facility-001",
  "assessment": {
    "demographics": { "client_name": "Freeman, Peter", "dob": "5/9/1965" },
    "vitals": { "blood_pressure": "130/85" },
    "medications": { "lisinopril": { "dosage_mg": 20 } }
  },
  "created_at": "2026-04-25T10:00:00Z",
  "updated_at": "2026-04-25T10:05:00Z",
  "updated_by": "user-uuid",
  "template_version": "WAC-388-76-615-v2024"
}
```

Returns 404 if the patient is archived or in another facility — we do not distinguish "doesn't exist" from "you can't see it".

---

## 6. `PATCH /patients/{patient_id}`

**Auth:** JWT required, scoped to caller's facility.

**Purpose:** Caregiver-driven correction. Three things can ride on a single PATCH:

- `preferred_name` — string update.
- `facility_id` — only allowed within the caller's own facility.
- `assessment_patch` — flat dict of `dotted.field.path -> new_value`.

**Request body:**
```json
{
  "preferred_name": "Pete",
  "assessment_patch": {
    "demographics.dob": "5/9/1965",
    "vitals.blood_pressure": "130/85"
  }
}
```

**What happens:**
- Each `assessment_patch` entry is applied in-place via `set_nested(...)` (defends against prototype-pollution-style paths).
- For each changed field, one row is written to `audit_trail` with `approval_method = "human"`.
- A synthetic `run_id` is generated for this PATCH so all changes from one PATCH operation are queryable as a unit.
- Unchanged values (where `old == new`) are silently skipped — no audit row.

**Response 200:** the updated patient (same shape as `GET /patients/{patient_id}`).

**Errors:**
- `422 Unprocessable Entity` — body has none of the three fields, or `assessment_patch` is empty / malformed.
- `403 Forbidden` — caller tried to move the patient into a different facility.

---

## 7. `POST /get-upload-url`

**Auth:** JWT required.

**Purpose:** Obtain a short-lived presigned S3 PUT URL so Flutter can upload a file **directly** to S3.

**Request body:**
```json
{
  "patient_id": "000333000",
  "file_type": "pdf"
}
```

`file_type` is one of:
- `"pdf"` — intake assessment PDF (writes under `uploads/`).
- `"audio"` — caregiver dictation (writes under `audio/`).
- `"signed_pdf"` — Flutter-rendered + caregiver-signed care plan (writes under `signed/`).

**Response 200:**
```json
{
  "upload_url": "https://s3.amazonaws.com/...?X-Amz-Signature=...",
  "s3_key": "uploads/000333000/abc-123.pdf",
  "expires_in": 600
}
```

The URL is valid for 10 minutes. The server chooses the `s3_key` — Flutter does not get to pick the path, which prevents cross-patient key injection.

---

## 8. `POST /intake`

**Auth:** JWT required.

**Purpose:** Start the intake pipeline for a patient. Returns immediately; the worker processes in the background.

**Request body:**
```json
{
  "patient_id": "000333000",
  "s3_key": "uploads/000333000/abc-123.pdf"
}
```

`s3_key` must be the exact value returned by `/get-upload-url` and must start with `uploads/`.

**Response 202 Accepted:**
```json
{
  "run_id": "run-2026-04-25-xyz",
  "status": "queued",
  "message": "Intake pipeline queued."
}
```

**Background processing:** worker downloads the PDF from S3 → Bedrock Sonnet 4.5 extracts the JSON → JSON saved to `patients.assessment` → audit entries written → SNS push notification.

---

## 9. `POST /reassessment`

**Auth:** JWT required, facility-scoped.

**Purpose:** Start the reassessment pipeline (audio dictation → AI-proposed field updates).

**Request body:**
```json
{
  "patient_id": "000333000",
  "audio_s3_key": "audio/000333000/xyz-456.m4a"
}
```

`audio_s3_key` must be the exact value returned by `/get-upload-url` and must start with `audio/`.

**Response 202 Accepted:**
```json
{
  "run_id": "run-2026-04-25-abc",
  "status": "queued",
  "message": "Reassessment pipeline queued."
}
```

**Background processing:** worker → `transcribe` (Amazon Transcribe Medical, async batch) → `llm_map` (Bedrock Claude proposes updates) → `llm_critic` (Bedrock scores confidence) → `confidence` (split high vs. low) → either `merge` directly OR pause for caregiver review → `merge` → `save_json` → `audit`.

---

## 10. `GET /patient/{patient_id}/status`

**Auth:** JWT required, facility-scoped.

**Purpose:** Get the latest pipeline run state for a resident. Drives the progress spinner and the "new intake / updates pending" branch in the UI.

**Response 200:**
```json
{
  "patient_id": "000333000",
  "run_id": "run-2026-04-25-abc",
  "status": "complete",
  "started_at": "2026-04-25T11:15:00Z",
  "completed_at": "2026-04-25T11:15:42Z",
  "error": null,
  "has_pending_review": false
}
```

`status` is one of: `"none"` (resident exists but no runs yet), `"queued"`, `"running"`, `"waiting_review"`, `"complete"`, `"failed"`.

---

## 11. `GET /review/{patient_id}`

**Auth:** JWT required, facility-scoped.

**Purpose:** Fetch the list of LLM-proposed updates waiting for caregiver approval.

**Response 200:**
```json
{
  "run_id": "run-2026-04-25-abc",
  "patient_id": "000333000",
  "status": "waiting_review",
  "flagged_updates": [
    {
      "update_id": "u-001",
      "field_path": "vitals.blood_pressure",
      "old_value": "120/80",
      "new_value": "130/85",
      "confidence": 0.72,
      "source_phrase": "blood pressure is one thirty over eighty five",
      "reasoning": "Direct numeric mention in the transcript."
    }
  ]
}
```

Returns 404 if the resident has no runs in `waiting_review`.

**Flutter UX:** render each item as a card with **Approve** / **Reject** buttons. See [SYSTEM_FLOW.md](SYSTEM_FLOW.md) Scenario 3.

---

## 12. `POST /review/{patient_id}`

**Auth:** JWT required, facility-scoped.

**Purpose:** Submit caregiver approve / reject / edit decisions. Resumes the paused pipeline.

**Request body:**
```json
{
  "run_id": "run-2026-04-25-abc",
  "decisions": [
    { "update_id": "u-001", "action": "approve" },
    { "update_id": "u-002", "action": "reject" },
    { "update_id": "u-003", "action": "edit", "edited_value": "walker" }
  ]
}
```

`action` is one of `"approve"`, `"reject"`, `"edit"`. When `"edit"`, `edited_value` replaces the LLM's proposed value.

**Response 202 Accepted:**
```json
{
  "run_id": "run-2026-04-25-abc",
  "status": "queued",
  "message": "Resume queued"
}
```

The worker picks up the resume job, merges the approved/edited values into the JSON, writes audit entries, and pushes the "Updates applied" notification.

---

## 13. `POST /signed-pdf/{run_id}`

**Auth:** JWT required, facility-scoped.

**Purpose:** Register a Flutter-rendered + caregiver-signed PDF against a pipeline run. This is how the legal archive artifact lands in S3.

**Flow Flutter follows:**
1. After a pipeline run completes (or after a manual PATCH), Flutter renders the WAC-388-76-615 template on-device from the resident's JSON.
2. Caregiver signs on-screen.
3. Flutter calls `POST /get-upload-url` with `file_type: "signed_pdf"` → gets the presigned URL + s3_key starting with `signed/`.
4. Flutter PUTs the rendered bytes to that URL.
5. Flutter calls **this endpoint** to record the key + template version.

**Request body:**
```json
{
  "s3_key": "signed/000333000/abc-789.pdf",
  "template_version": "WAC-388-76-615-v2024"
}
```

**Response 200:**
```json
{
  "run_id": "run-2026-04-25-abc",
  "patient_id": "000333000",
  "signed_at": "2026-04-25T11:16:00Z",
  "status": "registered"
}
```

**Idempotent:** uploading a new signed version replaces the previous `signed_pdf_s3_key` on the run and bumps `signed_at`.

---

## 14. `GET /get-download-url/{patient_id}`

**Auth:** JWT required, facility-scoped.

**Purpose:** Return a 15-minute presigned GET URL for the **latest signed PDF** of this resident.

**Response 200:**
```json
{
  "download_url": "https://s3.amazonaws.com/...?X-Amz-Signature=...",
  "expires_in": 900,
  "generated_at": "2026-04-25T11:20:00Z"
}
```

URL is valid for 15 minutes and scoped to one S3 object.

**Returns 404** if no signed PDF has been uploaded for this resident yet — the backend never generates PDFs itself.

---

# Typical end-to-end sequences

## A. Onboarding a brand-new resident

```
Flutter → POST /patients  { patient_id: "000333000", preferred_name: "Peter" }   → 201
Flutter → POST /get-upload-url  { file_type: "pdf" }                              → { upload_url, s3_key }
Flutter → PUT  <upload_url>  (PDF bytes)                                          → 200 (S3)
Flutter → POST /intake  { patient_id, s3_key }                                    → 202 { run_id }
... worker runs intake pipeline ...
Flutter (push) → GET /patient/{id}/status                                         → { status: "complete" }
Flutter → GET /patients/{id}                                                       → full JSON
Flutter renders template, caregiver corrects via PATCH if needed
Flutter → PATCH /patients/{id}  { assessment_patch: { ... } }                     → 200
Caregiver signs; Flutter renders final PDF
Flutter → POST /get-upload-url  { file_type: "signed_pdf" }                       → { upload_url, s3_key }
Flutter → PUT  <upload_url>  (signed PDF bytes)                                   → 200
Flutter → POST /signed-pdf/{run_id}  { s3_key, template_version }                 → 200
```

## B. Reassessment (voice update, all auto-applied)

```
Flutter → POST /get-upload-url  { file_type: "audio" }                            → { upload_url, s3_key }
Flutter → PUT  <upload_url>  (audio bytes)                                        → 200 (S3)
Flutter → POST /reassessment  { patient_id, audio_s3_key }                        → 202 { run_id }
... worker: transcribe → LLM map → critic → confidence → merge → save → audit ...
Flutter → GET /patient/{id}/status                                                → { status: "complete" }
Flutter → GET /patients/{id}                                                       → updated JSON
Flutter renders + caregiver signs + uploads signed PDF (same as A)
```

## C. Reassessment that pauses for caregiver review

```
Flutter → POST /reassessment  { ... }                                             → 202
... worker pauses in waiting_review ...
Flutter (push) → GET /review/{id}                                                  → { flagged_updates: [...] }
Caregiver approves / rejects in UI
Flutter → POST /review/{id}  { decisions: [...] }                                 → 202 { status: "queued" }
... worker resumes, merges, saves, audits ...
Flutter → GET /patient/{id}/status                                                → { status: "complete" }
Flutter renders + caregiver signs (same as A)
```

---

# Errors Flutter must handle

| Code | When | What Flutter should do |
|------|------|----|
| 401 | JWT invalid / expired | Re-authenticate via Cognito SDK |
| 404 | Patient or run not found, or in another facility | Show "not found" — do not reveal whether it exists elsewhere |
| 409 | Conflict (e.g. duplicate ACES ID, run not in `waiting_review`) | Refresh state — someone else may have acted |
| 413 | SQS body too large | Should never happen in normal flow; surface as a bug |
| 422 | Invalid request shape | Fix the request; do not retry |
| 5xx | Backend error | Retry with exponential backoff up to 3 times |

---

# What's intentionally NOT in this API

The Flutter dev's original spec proposed several endpoints we deliberately did not build, because they're better served elsewhere:

| Endpoint Flutter dev wanted | Why dropped |
|---|---|
| `POST /auth/login`, `/auth/refresh`, `/auth/logout` | Use the AWS Cognito Flutter SDK directly. Backend only verifies tokens. |
| `/transcripts/start`, `/transcripts/{job}` | Backend triggers Transcribe Medical inside the pipeline; Flutter never starts a transcription job directly. |
| `/audio/commit`, `/audio/discard` | Replaced by `POST /reassessment`. |
| `/recordings/*` (5 endpoints) | No "recording" entity — audio is just an input to a pipeline run. |
| `POST /care-plan/export-pdf` | Backend never renders PDFs. Flutter renders the template on-device. |
| `DELETE /patients/{id}` (hard) | Use soft-archive (`archived_at`) — HIPAA forbids hard deletes. |

---

# Related docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture diagram
- [SYSTEM_FLOW.md](SYSTEM_FLOW.md) — plain-language scenarios
- [PIPELINE.md](PIPELINE.md) — what every LangGraph node does
- [../backend/.env.example](../backend/.env.example) — config reference
- [../backend/main.py](../backend/main.py) — endpoint implementations
