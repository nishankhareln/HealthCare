# Samni Labs — How the System Works

A plain-language walkthrough of what happens at each step, written so that anyone — caregivers, regulators, the CEO, the Flutter developer — can read it once and understand the system end-to-end. No prior technical knowledge is assumed.

For the architecture picture, see [ARCHITECTURE.md](ARCHITECTURE.md).
For the AI pipeline internals, see [PIPELINE.md](PIPELINE.md).
For the REST API contract, see [API_ENDPOINTS.md](API_ENDPOINTS.md).

---

## Who uses this system

| Person | What they do |
|---|---|
| **Caregiver** | Works at a Washington state Adult Family Home. Creates resident records, uploads the existing PDF assessment, records voice updates, reviews AI-proposed changes, signs the final care plan on their phone. |
| **Resident** | Receives care. Never directly interacts with the app. Their structured medical record is the data we protect. |
| **Case manager / facility admin** *(future)* | Receives signed care plans by email; reviews audit trails for compliance. |

---

## The four routine scenarios

We have four scenarios in the system. Reading these in order tells you the entire happy path.

1. Caregiver creates a new resident record (no PDF yet).
2. Caregiver onboards an existing resident by uploading the filled DSHS Assessment Details PDF.
3. Caregiver records a voice update for an existing resident.
4. Caregiver downloads the latest signed PDF.

Plus three special cases at the end (low-confidence AI suggestions, manual edits, failures).

---

## Scenario 1 — Caregiver creates a new resident

**When:** A new resident has just moved in and the caregiver wants to start tracking them in the app.

### Step-by-step

1. **Caregiver logs in** on the Flutter app. AWS Cognito verifies the password and returns a JWT token. The app stores it locally.

2. **Caregiver taps "Add resident"** and enters two things:
   - The **9-digit ACES ID** (Washington state's Automated Client Eligibility System ID — e.g. `000333000`). This is on the resident's intake paperwork.
   - A **preferred name** for the UI (e.g. "Peter").

3. **Flutter calls `POST /patients`** with that ACES ID and name.

4. **Backend creates a new row** in the `patients` table:
   - `patient_id` = the ACES ID
   - `preferred_name` = "Peter"
   - `assessment` = `{}` (empty for now — gets populated later)
   - `facility_id` = the caregiver's facility (cross-facility creation is refused)
   - `created_at` = now

5. **The new resident appears on the home screen list** the next time Flutter refreshes (`GET /patients`).

⏱️ **Duration:** under 1 second.

---

## Scenario 2 — Caregiver onboards an existing resident (PDF intake)

**When:** The resident already has a completed DSHS care-plan PDF, and the caregiver wants to digitize it. This is a **one-time** operation per resident.

### Step-by-step

1. Caregiver opens the resident's page and taps **"Upload Assessment PDF"**.

2. Flutter asks the backend for an upload URL: `POST /get-upload-url` with `{patient_id, file_type: "pdf"}`.

3. Backend returns a one-time **presigned S3 URL** valid for 10 minutes, plus the S3 key the file will live under (e.g. `uploads/000333000/<uuid>.pdf`).

4. Flutter uploads the PDF directly to S3 using that URL. **Our backend never sees the file content** — only the URL is involved.

5. Once the upload finishes, Flutter calls `POST /intake` with the resident's ACES ID and the S3 key.

6. Backend:
   - Inserts a row into `pipeline_runs` with status `queued`.
   - Pushes a job to AWS SQS.
   - Returns `202 Accepted` immediately. The caregiver is **not** kept waiting.

7. **A separate worker process** picks the job up from SQS and runs the LangGraph pipeline:

   **a. parse_pdf** — Reads the PDF from S3 and sends it to **AWS Bedrock Claude Sonnet 4.5**. Claude reads the 16-page DSHS form and returns every field as structured JSON.

   **b. save_json** — Writes the JSON into the resident's row in `patients.assessment`.

   **c. audit** — For every field that was extracted, writes one row to `audit_trail` so we have a complete change history (≈200 rows for a typical intake).

8. Worker sends a push notification: *"Assessment ready for Peter Freeman"*.

9. **Caregiver fetches the parsed JSON** by calling `GET /patients/{aces_id}`. The Flutter app renders the WAC-388-76-615 template on-device with every field pre-populated and shows it to the caregiver.

10. **Caregiver reviews the form, fixes anything Bedrock got wrong** by calling `PATCH /patients/{aces_id}` with corrections (each correction writes one row to `audit_trail` with `approval_method = "human"`).

11. **Caregiver signs on-device.** Flutter captures the ink signature, finalizes the rendered PDF, and uploads it to S3 (`signed/` prefix). Flutter then calls `POST /signed-pdf/{run_id}` to register the key on that pipeline run — that is now the **legal archive artifact**.

⏱️ **Duration:** the upload is instant; AI parsing takes 30 – 60 seconds in the background. Caregiver review and signing happen whenever they're ready.

---

## Scenario 3 — Caregiver records a voice update (reassessment)

**When:** Days or weeks after intake, the caregiver wants to update the resident's record — they observed a change, gave new medication, or noticed a new concern. Instead of typing into a form, they **dictate**.

### Step-by-step (happy path — all updates auto-applied)

1. Caregiver opens the resident's page and taps the **microphone**.

2. They dictate freely: *"Peter's blood pressure is one thirty over eighty five this morning. He's now using a walker for mobility and I doubled his lisinopril to twenty milligrams."*

3. Flutter records the audio locally as an `.m4a` file.

4. Flutter requests an upload URL: `POST /get-upload-url` with `{patient_id, file_type: "audio"}`.

5. Flutter uploads the audio file to S3 (under `audio/<aces_id>/<uuid>.m4a`) directly via the presigned URL.

6. Flutter calls `POST /reassessment` with `{patient_id, audio_s3_key}`.

7. Backend creates a `pipeline_runs` row with status `queued`, queues a job in SQS, returns `202 Accepted`.

8. Worker picks up the job and runs the **reassessment pipeline**:

   **a. load_patient_json** — Reads the resident's current JSON from `patients.assessment`. Without this context the LLM can't tell "update" from "first-time entry".

   **b. transcribe** — Sends the audio S3 key to **Amazon Transcribe Medical** (async batch job). Polls until complete. The result is saved at `transcripts/{run_id}.json` (so subsequent retries don't re-transcribe). The transcript text is loaded into pipeline state.

   **c. llm_map** — Sends the transcript + the current JSON to **Bedrock Claude Sonnet 4.5**. Claude returns a list of proposed updates:
   - `vitals.blood_pressure` → `"130/85"`
   - `mobility.assistive_device` → `"walker"`
   - `medications.lisinopril.dosage_mg` → `20`

   **d. llm_critic** — A second Claude call reviews the first one's proposals. Each gets a **confidence score** (0.0 – 1.0).

   **e. confidence** — Splits proposals into two buckets:
   - ≥ 0.85 → auto-applied
   - < 0.85 → flagged for caregiver review

   **f. (happy path) all proposals are high-confidence** → straight to merge.

   **g. merge** — Applies each proposal to the JSON. Records each change.

   **h. save_json** — Writes the updated JSON back to Postgres.

   **i. audit** — Writes one `audit_trail` row per applied update (with `approval_method = "auto"`, the AI's confidence, and the source phrase from the transcript).

9. Worker sends push: *"3 updates applied to Peter Freeman's record"*.

10. Caregiver opens the app, sees the updated form, signs, uploads the new signed PDF — same flow as Scenario 2 steps 9–11.

⏱️ **Duration:** typically 60 – 120 seconds end-to-end (longer audio = longer transcription).

---

## Scenario 3 (variant) — Some AI suggestions need caregiver approval

Steps 1–8e are identical. Then:

9. **Confidence step finds some low-confidence proposals.** High-confidence ones are auto-applied. Low-confidence ones are flagged and saved to `pipeline_runs.flagged_updates`. The pipeline **pauses** — LangGraph saves a checkpoint to Postgres.

10. Worker marks the run as `waiting_review` and sends a different push: *"3 updates need your review"*.

11. Caregiver taps the notification. Flutter calls `GET /review/{patient_id}` and shows each flagged update as a card with **Before** / **After** values, the original transcript phrase, and **Approve** / **Reject** buttons.

12. Caregiver reviews and submits decisions: `POST /review/{patient_id}` with the list of approve / reject / edit actions.

13. Backend validates the decisions, marks the run `queued` again, and pushes a "resume" job to SQS.

14. Worker picks up the resume, calls `graph.ainvoke(Command(resume=...))`. LangGraph fast-forwards to the paused point and continues:

   - merge → save_json → audit → END

15. Worker sends a final push: *"Updates applied"*. Flutter re-renders, caregiver signs, signed PDF goes to S3 — same as the happy path.

⏱️ **The pipeline can stay paused indefinitely** waiting for the caregiver — there is no timeout pressure on them.

---

## Scenario 4 — Downloading the signed PDF

**When:** Anytime after the caregiver has signed an updated plan and the case manager (or admin) needs the official copy.

### Step-by-step

1. Caregiver taps **"Download PDF"**.

2. Flutter calls `GET /get-download-url/{patient_id}`.

3. Backend:
   - Verifies the caregiver belongs to this resident's facility.
   - Looks up the most recent signed PDF in Postgres (`pipeline_runs.signed_pdf_s3_key`, ordered by `signed_at`).
   - Generates a 15-minute presigned download URL.
   - Returns it.

4. Flutter downloads the PDF directly from S3 and the caregiver shares / prints / emails it.

⚠️ **Important:** This endpoint only works **after** the caregiver has signed at least once. If they haven't, the endpoint returns `404 Not Found`. Flutter should fall back to rendering from JSON on-device.

⚠️ **Security note:** The presigned URL expires in 15 minutes and is scoped to one specific S3 object. Even if a forwarded email gets intercepted, the link will not work past 15 minutes and cannot be used to browse other patients' files.

---

## Failure handling — what happens when things go wrong

The system is designed to **fail safely** without losing data. Common failures:

| Failure | What the system does |
|---|---|
| **PDF parse fails (Bedrock unhappy)** | Run → `failed` with a bounded error message. SQS message deleted. Caregiver sees a "processing failed" notification and can retry. |
| **Transcribe Medical times out** | Run → `failed`. SQS redelivery up to 3 times. After that, the message goes to the dead-letter queue for manual review. |
| **Bedrock LLM call fails mid-run** | LangGraph checkpoint preserves progress. Retry picks up from the last completed step — no re-transcribing the audio. |
| **Worker crashes mid-pipeline** | The SQS visibility timeout expires; another worker picks up the job. The checkpoint resumes from the exact last step. |
| **Database goes down** | API returns `503 Service Unavailable`. Worker pauses. Once DB is back, in-flight runs continue from checkpoint. Nothing is silently lost. |
| **JWT expired** | API returns `401`. Flutter prompts the caregiver to log in again. |
| **Caregiver tries to access another facility's resident** | API returns `404` deliberately. We don't reveal whether the resident exists in another facility. |
| **Caregiver never responds to a review** | The pipeline sits paused indefinitely. Ops can run a periodic sweep to mark reviews "abandoned" after N days if needed. |
| **SQS delivers the same message twice** | The terminal-state guard in the worker detects the run is already running/complete and discards the duplicate. |

---

## What data lives where

| Data | Storage | Why there |
|---|---|---|
| Resident's full structured medical record (200+ fields) | PostgreSQL `patients.assessment` (JSONB) | Queryable, transactional, with constraints. |
| Resident's preferred display name | PostgreSQL `patients.preferred_name` | Used in the home-screen list without loading the full assessment. |
| Soft-delete marker | PostgreSQL `patients.archived_at` | HIPAA forbids hard deletes; non-null hides the row from queries. |
| Template version used for the latest signed PDF | PostgreSQL `patients.template_version` | So the exact signed artifact can be re-rendered forensically. |
| Every change ever made to any resident | PostgreSQL `audit_trail` (append-only) | HIPAA requires a complete change history. |
| Status of every processing job | PostgreSQL `pipeline_runs` (+ LangGraph checkpoint tables) | Drives progress UI and resume-after-pause. |
| Signed PDF archive key + timestamp | PostgreSQL `pipeline_runs.signed_pdf_s3_key` and `signed_at` | Points to the legal artifact for that run. |
| Uploaded intake PDFs | S3 `uploads/` | Large binary; not for a DB. |
| Uploaded audio dictations | S3 `audio/` | Same. |
| Transcripts (Transcribe Medical output) | S3 `transcripts/` | Cached so retries don't re-transcribe. |
| Signed final PDFs | S3 `signed/` | The legal archive. |
| Blank WAC-388-76-615 template | Bundled inside the Flutter app | Fixed; ships with each app release. |
| User accounts | AWS Cognito | Managed auth, password reset, MFA. |

---

## Security guarantees the system promises

1. **All resident data is encrypted at rest.** S3 objects use SSE-KMS. The PostgreSQL database (Amazon RDS) is encrypted at rest.
2. **All resident data is encrypted in transit.** Every connection uses TLS — phone↔backend, backend↔database, backend↔AWS services.
3. **No resident data is ever logged.** Logs only contain identifiers, status codes, and timing — never patient names, transcript text, or field values.
4. **Every change is fully audited.** The `audit_trail` table is append-only; rows are never updated or deleted by application code. We can answer "who changed Peter's medication on April 22nd?" in one query.
5. **Caregivers can only see residents in their own facility.** Enforced on every endpoint — not a UI-only check.
6. **All access links are short-lived.** Upload URLs expire in 10 minutes; download URLs in 15 minutes; JWTs typically in 1 hour.
7. **No AI hallucination ever lands in a record without review.** Either the AI's confidence is high enough to auto-apply (and the confidence is recorded in the audit trail), or a human caregiver explicitly approves the change.

---

## Two-sentence executive summary

> A caregiver records a voice note or uploads a PDF from their phone. Our backend uses AWS Transcribe Medical and AWS Bedrock Claude to turn that into structured updates, applies a human-in-the-loop safety check, saves everything to PostgreSQL with a complete audit trail, and the caregiver signs the final care plan on-device — the signed PDF is archived in AWS S3 for compliance. Every regulator question — "who changed what, when, and why?" — has an answer in one query.

---

## Related docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture diagram
- [PIPELINE.md](PIPELINE.md) — what every LangGraph node does
- [API_ENDPOINTS.md](API_ENDPOINTS.md) — full REST API reference
- [../backend/.env.example](../backend/.env.example) — every config knob
- [../backend/Dockerfile](../backend/Dockerfile) — how the backend container is built
