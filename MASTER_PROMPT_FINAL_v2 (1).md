# MASTER PROMPT — Final Version v2 (PostgreSQL + All Fixes Applied)

Copy everything below this line and paste it into Claude (VS Code / Claude Code).
Give it in 3 parts as instructed at the bottom of this document.

---

## Role

You are a senior Python backend engineer. You are building the backend for a healthcare document automation system called Samni Labs. You will write production-ready Python code with proper error handling, logging, and type hints. No shortcuts, no TODO comments, no placeholder logic — every function must be fully implemented.

## Project Context

This system automates patient assessment updates for assisted living facilities. A caregiver uploads a 36-page state-issued patient assessment PDF once per patient. The system parses it into a structured JSON object (this part is handled by another team — NOT your responsibility). Later, the caregiver records an audio dictation of their reassessment observations. The system transcribes the audio, uses an LLM to map the spoken updates to the correct JSON fields, and generates an updated PDF.

The Flutter mobile app uploads files DIRECTLY to S3 using presigned URLs. Files never pass through the backend server. The backend only receives the S3 key (a string telling it where the file was uploaded).

## Your Responsibility (ONLY these parts)

You are building:

1. **FastAPI application** with 7 REST endpoints (NOT 5 — includes presigned URL endpoints)
2. **Presigned URL generation** for direct S3 uploads and downloads from the Flutter app
3. **SQS message queue integration** for async pipeline execution (the server does NOT run pipelines directly — it drops a message into SQS, a background worker picks it up)
4. **Background worker** that polls SQS and runs LangGraph pipelines
5. **LangGraph pipeline** with 10 nodes that orchestrates the entire backend workflow
6. **Integration with Amazon Transcribe Medical** (speech-to-text API calls)
7. **Integration with Amazon Bedrock + Claude** (LLM API calls for field mapping and critic verification)
8. **Confidence-based routing logic** (auto-approve if >= 0.85, route to human review if < 0.85)
9. **Human review interrupt/resume mechanism** using LangGraph's interrupt()
10. **JSON merge logic** (apply approved updates to the master patient JSON)
11. **Audit trail** (record every change with field_path, old_value, new_value, source_phrase, confidence, timestamp)
12. **Push notifications via SNS** at three specific moments: when human review is needed, when pipeline completes successfully, and when pipeline fails
13. **Integration with AWS services** via boto3 (S3, SQS, Cognito, SNS, Transcribe Medical, Bedrock) and **PostgreSQL via SQLAlchemy** for all database operations
14. **Database schema and migrations** using SQLAlchemy models and Alembic

## NOT Your Responsibility (another team handles these)

- PDF parsing (pdfplumber + PyMuPDF) — they give you a clean JSON object. Assume a function `parse_pdf(s3_key: str) -> dict` exists.
- PDF generation (Jinja2 + WeasyPrint) — they take a JSON object and produce a PDF. Assume a function `generate_pdf(patient_json: dict, changes: set, output_s3_key: str) -> str` exists.
- Flutter mobile app — they consume your API endpoints and handle file uploads to S3 using the presigned URLs you provide.
- The Pydantic schema definition for patient data — they define the PatientAssessment model. Assume it exists as an import.

Create placeholder/mock versions of the team's functions for testing.

## Architecture Decisions (Follow these exactly)

### Tech Stack
- **Framework:** FastAPI (Python 3.11+)
- **Pipeline orchestration:** LangGraph (with PostgreSQL checkpoint backend via langgraph-checkpoint-postgres — the official, most stable LangGraph checkpointer)
- **Job queue:** Amazon SQS (decouples API from pipeline execution)
- **Speech-to-text:** Amazon Transcribe Medical (via boto3, NOT Whisper)
- **LLM:** Amazon Bedrock + Claude Sonnet (via boto3, NOT local models, NOT OpenAI)
- **File storage:** Amazon S3 (via boto3) — Flutter uploads directly using presigned URLs
- **Database:** PostgreSQL on Amazon RDS (via SQLAlchemy + asyncpg) — NOT DynamoDB, NOT SQLite
- **Authentication:** Amazon Cognito (validate JWT tokens from the Flutter app)
- **Push notifications:** Amazon SNS (via boto3)
- **PDF parsing:** NOT your job — assume the function exists
- **PDF generation:** NOT your job — assume the function exists

### Key Rules
- All config (AWS region, S3 bucket names, DATABASE_URL, SQS URL, confidence threshold) must come from environment variables via a config.py file. Never hardcode AWS resource names or database credentials.
- Temperature for all Bedrock Claude calls: 0.1 (deterministic, not creative)
- Confidence threshold: 0.85 (configurable via env var)
- All patient data is PHI — NEVER log patient names, SSN, DOB, or any identifiable information. Log only patient_id, field_paths, confidence scores, and pipeline status.
- FastAPI endpoints must return IMMEDIATELY. Pipeline execution happens asynchronously via SQS + background worker. The caregiver never waits for processing to complete.
- Use async where possible (FastAPI is async-native, use async SQLAlchemy sessions)
- Every function must have type hints and a docstring
- Use Pydantic models for all request/response schemas
- Use SQLAlchemy ORM models for all database tables
- Use Alembic for database schema migrations

### File Upload Architecture (IMPORTANT)
- The Flutter app NEVER sends files to FastAPI.
- Flutter calls `POST /get-upload-url` to get a presigned S3 URL.
- Flutter uploads the file directly to S3 using that URL.
- Flutter then calls `POST /intake` or `POST /reassessment` with just the S3 key (a string).
- For downloads, Flutter calls `GET /get-download-url/{patient_id}` to get a presigned download URL, then downloads directly from S3.
- This means: FastAPI never receives or sends file data. It only generates URLs and processes S3 keys.

### Async Pipeline Architecture (IMPORTANT)
- When FastAPI receives a `/intake` or `/reassessment` request, it does NOT run the LangGraph pipeline directly.
- Instead, it drops a message into an SQS queue with the job details (patient_id, s3_key, pipeline_type, user_id).
- A background worker process (running in the same container) polls SQS, picks up messages, and runs the LangGraph pipeline.
- This prevents 50 simultaneous uploads from crashing the server.
- The worker processes one job at a time (or a configurable number of concurrent jobs).

## Project Structure (Create this exact structure)

```
samni-backend/
├── main.py                    # FastAPI app, 7 endpoints, startup
├── worker.py                  # SQS background worker, polls queue, runs pipelines
├── config.py                  # All configuration from environment variables
├── database.py                # SQLAlchemy async engine, session factory, Base
├── db_models.py               # SQLAlchemy table models (patients, audit_trail, pipeline_runs)
├── pipeline.py                # LangGraph graph definitions (intake + reassessment)
├── state.py                   # PipelineState TypedDict (shared state between nodes)
├── nodes/
│   ├── __init__.py
|   |-- load_patient_json.py   # # Load existing patient JSON from PostgreSQL
│   ├── parse_pdf.py           # PLACEHOLDER — calls team's parser, returns JSON
│   ├── save_json.py           # Save patient JSON to PostgreSQL patients table
│   ├── transcribe.py          # Call Amazon Transcribe Medical via boto3
│   ├── llm_map.py             # Call Bedrock + Claude for field mapping
│   ├── llm_critic.py          # Call Bedrock + Claude for self-verification
│   ├── confidence.py          # Check scores, split into auto_apply vs flagged
│   ├── human_review.py        # LangGraph interrupt() for human review
│   ├── merge.py               # Apply approved updates to master JSON, record audit
│   ├── generate_pdf.py        # PLACEHOLDER — calls team's PDF generator
│   └── audit.py               # Write audit trail entries to PostgreSQL audit_trail table
├── notifications.py           # SNS push notification helper (3 triggers)
├── prompts.py                 # System prompts and user prompt builders for Claude
├── models.py                  # Pydantic models for API requests/responses
├── auth.py                    # Cognito JWT token validation middleware
├── aws_clients.py             # Centralized boto3 client initialization (S3, SQS, Transcribe, Bedrock, SNS, Cognito)
├── utils.py                   # Helper functions (get_nested, set_nested, etc.)
├── alembic.ini                # Alembic migration configuration
├── alembic/
│   ├── env.py                 # Alembic environment config
│   └── versions/              # Migration scripts (auto-generated)
├── tests/
│   ├── test_dictations.json   # 30+ test sentences with expected outputs
│   ├── test_pipeline.py       # End-to-end pipeline test
│   ├── test_nodes.py          # Unit tests for individual nodes
│   ├── test_merge.py          # Unit tests for merge logic
│   └── conftest.py            # Pytest fixtures (mock AWS clients, test database)
├── requirements.txt
├── Dockerfile
├── .env.example               # Example environment variables
└── README.md
```

## Detailed Specifications

### config.py

Read all config from environment variables with sensible defaults for local development:

- AWS_REGION (default: us-east-1)
- S3_BUCKET (default: samni-phi-documents-dev)
- S3_PRESIGNED_EXPIRY (default: 900 — 15 minutes in seconds)
- SQS_QUEUE_URL (no default, required in production)
- DATABASE_URL (default: postgresql+asyncpg://postgres:password@localhost:5432/samni_dev)
- DATABASE_URL_SYNC (default: postgresql://postgres:password@localhost:5432/samni_dev) — needed for Alembic migrations which run synchronously
- COGNITO_USER_POOL_ID (no default, required in production)
- COGNITO_APP_CLIENT_ID (no default, required in production)
- SNS_TOPIC_ARN (no default, required in production)
- BEDROCK_MODEL_ID (default: anthropic.claude-sonnet-4-20250514)
- CONFIDENCE_THRESHOLD (default: 0.85)
- WORKER_CONCURRENCY (default: 2 — how many pipeline jobs to run simultaneously)
- LOG_LEVEL (default: INFO)
- ENVIRONMENT (default: dev)

### database.py — SQLAlchemy Setup

- Create an async SQLAlchemy engine using `create_async_engine(DATABASE_URL)`
- Create an async session factory using `async_sessionmaker`
- Define a `Base` class using `declarative_base()` for all ORM models
- Provide a `get_db_session()` async context manager for use in FastAPI dependencies and node functions
- On FastAPI startup, verify database connectivity
- On FastAPI shutdown, dispose of the engine

### db_models.py — SQLAlchemy Table Models

Define THREE tables:

**Table 1: patients**
```python
class Patient(Base):
    __tablename__ = "patients"
    
    patient_id = Column(String, primary_key=True)
    facility_id = Column(String, nullable=True)
    assessment = Column(JSONB, nullable=False)       # the full patient JSON (200+ fields)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    updated_by = Column(String, nullable=True)
```

**Table 2: audit_trail**
```python
class AuditEntry(Base):
    __tablename__ = "audit_trail"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(String, ForeignKey("patients.patient_id"), nullable=False, index=True)
    run_id = Column(String, nullable=False, index=True)
    field_path = Column(String, nullable=False, index=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=False)
    source_phrase = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    user_id = Column(String, nullable=False)
    approval_method = Column(String, nullable=False)   # "auto" or "human"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
```

**Table 3: pipeline_runs**
```python
class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    
    run_id = Column(String, primary_key=True)
    patient_id = Column(String, ForeignKey("patients.patient_id"), nullable=False, index=True)
    pipeline_type = Column(String, nullable=False)     # "intake" or "reassessment"
    status = Column(String, nullable=False, default="queued")  # queued, running, waiting_review, complete, failed
    user_id = Column(String, nullable=False)
    s3_key = Column(String, nullable=True)
    output_pdf_s3_key = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    flagged_updates = Column(JSONB, nullable=True)     # stored when status = waiting_review
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

Important indexes:
- audit_trail: index on (patient_id, created_at) for per-patient history queries
- audit_trail: index on (field_path, created_at) for cross-patient analytics later
- pipeline_runs: index on (patient_id, created_at) for latest run lookup

### state.py — Pipeline State

```python
class PipelineState(TypedDict):
    # Pipeline identity
    run_id: str                      # unique ID for this pipeline run
    patient_id: str
    user_id: str
    pipeline_type: str               # "intake" or "reassessment"
    
    # Inputs (S3 keys — files are already in S3, uploaded by Flutter)
    pdf_s3_key: Optional[str]        # S3 key for PDF, only for intake
    audio_s3_key: Optional[str]      # S3 key for audio, only for reassessment
    
    # From parser (intake only)
    raw_pages: Optional[list]
    
    # Master JSON (loaded from PostgreSQL at start of reassessment)
    master_json: dict
    
    # From transcription
    transcript: Optional[str]
    transcript_segments: Optional[list]
    
    # From LLM
    proposed_updates: Optional[list]
    verified_updates: Optional[list]
    
    # From confidence routing
    auto_approved: Optional[list]
    flagged_updates: Optional[list]
    
    # From human review
    human_decisions: Optional[list]
    approved_updates: Optional[list]
    
    # Output
    final_json: Optional[dict]
    output_pdf_s3_key: Optional[str]
    audit_entries: Optional[list]
    
    # Status tracking
    status: str                      # "queued", "running", "waiting_review", "complete", "failed"
    error: Optional[str]
    started_at: Optional[str]        # ISO timestamp
    completed_at: Optional[str]      # ISO timestamp
```

### main.py — FastAPI Endpoints

7 endpoints + 2 utility endpoints:

**POST /get-upload-url**
- Auth: required (Cognito token)
- Input: { patient_id: str, file_type: "pdf" | "audio" }
- Action: generate a presigned S3 PUT URL
  - For PDF: s3_key = "uploads/{patient_id}/original.pdf"
  - For audio: s3_key = "audio/{patient_id}/{iso_timestamp}.wav"
  - Expiry: S3_PRESIGNED_EXPIRY seconds (default 15 minutes)
- Response: { upload_url: str, s3_key: str, expires_in: int }
- NOTE: This endpoint does NOT receive any file. It returns a URL that Flutter uses to upload directly to S3.

**POST /intake**
- Auth: required
- Input: { patient_id: str, s3_key: str }
- Action: 
  - Validate the s3_key points to a real object in S3 (head_object check)
  - Create a run_id (UUID)
  - Insert a new row into PostgreSQL pipeline_runs table (status: "queued")
  - Drop a message into SQS: { run_id, patient_id, s3_key, pipeline_type: "intake", user_id }
  - Return immediately — do NOT wait for processing
- Response: { run_id: str, status: "queued" }

**POST /reassessment**
- Auth: required
- Input: { patient_id: str, s3_key: str }
- Action:
  - Validate the s3_key points to a real object in S3
  - Verify the patient exists in PostgreSQL patients table (they must have completed intake first)
  - Create a run_id (UUID)
  - Insert a new row into PostgreSQL pipeline_runs table (status: "queued")
  - Drop a message into SQS: { run_id, patient_id, s3_key, pipeline_type: "reassessment", user_id }
  - Return immediately
- Response: { run_id: str, status: "queued" }

**GET /patient/{patient_id}/status**
- Auth: required
- Action: query PostgreSQL pipeline_runs table for the latest run for this patient (ORDER BY created_at DESC LIMIT 1)
- Response: { patient_id, run_id, status, started_at, completed_at, error, has_pending_review }

**GET /review/{patient_id}**
- Auth: required
- Action: query PostgreSQL pipeline_runs table for a run with status = "waiting_review" for this patient
- Response: { pending: true/false, run_id, flagged_updates: [...], patient_context: {...} }

**POST /review/{patient_id}**
- Auth: required
- Input: { run_id: str, decisions: [{ update_id: str, action: "approve" | "reject" | "edit", edited_value?: any }] }
- Action: 
  - Save the decisions to PostgreSQL pipeline_runs table (update the flagged_updates JSONB column with decisions)
  - Resume the paused LangGraph pipeline with the human's decisions
  - This triggers the worker to continue processing from where it paused
- Response: { status: "resumed" }

**GET /get-download-url/{patient_id}**
- Auth: required
- Action: query PostgreSQL pipeline_runs table for the latest completed run, get the output_pdf_s3_key, generate a presigned S3 GET URL
- Response: { download_url: str, expires_in: int, generated_at: str }
- NOTE: FastAPI does NOT stream the file. It returns a URL that Flutter uses to download directly from S3.

**GET /health**
- No auth required
- Response: { status: "healthy", version: "1.0.0", environment: "dev" }

### worker.py — Background SQS Worker

This is a separate process that runs alongside FastAPI in the same container (or as a separate container). It continuously polls SQS for new pipeline jobs.

The worker must:
- Poll SQS using long polling (wait_time_seconds=20 to reduce API calls)
- When a message arrives, parse the job details (run_id, patient_id, s3_key, pipeline_type, user_id)
- Update pipeline status in PostgreSQL pipeline_runs table to "running" and set started_at
- Run the appropriate LangGraph graph (intake or reassessment)
- On success: update status to "complete" and set completed_at, send SNS push notification "PDF is ready"
- On failure: update status to "failed" with error message, send SNS push notification "Processing failed"
- On human review needed: update status to "waiting_review" and store flagged_updates in JSONB column, send SNS push notification "Review needed"
- Delete the SQS message after successful processing
- If processing fails, the message returns to the queue after the visibility timeout (for retry)
- Respect WORKER_CONCURRENCY config — process N jobs at a time using asyncio or threading
- Graceful shutdown: finish current job before exiting

### notifications.py — Push Notification Helper

Three notification functions:

- `notify_review_needed(patient_id, user_id, flagged_count)` — sends "Review needed for patient {name}. {count} items need your attention."
- `notify_pipeline_complete(patient_id, user_id)` — sends "Patient assessment updated. Your PDF is ready to download."
- `notify_pipeline_failed(patient_id, user_id, error_summary)` — sends "Processing failed for patient assessment. Please try again or contact support."

All three use SNS publish() via boto3. The message payload should include enough data for the Flutter app to navigate to the right screen (patient_id, notification_type, run_id).

### aws_clients.py — Centralized AWS Clients

Create and export boto3 clients for:
- s3_client
- sqs_client
- transcribe_client
- bedrock_client (bedrock-runtime)
- sns_client
- cognito_client (cognito-idp)

NOTE: Database operations use SQLAlchemy sessions from database.py, NOT boto3. DynamoDB is not used in this project.

All boto3 clients should use the region from config.py. In local dev, they should work with AWS credentials from environment or ~/.aws/credentials.

### prompts.py — LLM Prompts

**SYSTEM_PROMPT for field mapping (Node: llm_map):**

The system prompt must instruct Claude to:
- Act as a medical document update interpreter for assisted living facilities
- Extract every clinically relevant update from the caregiver's transcript
- Map each update to the correct JSON field path from the provided schema
- Output ONLY a valid JSON array — no markdown, no explanation, no preamble
- Each object in the array must contain: field_path, new_value, source_phrase, reasoning, confidence
- NEVER invent information not in the transcript. Every update must have a source_phrase from the actual transcript.
- Handle self-corrections: if the caregiver says "wait, no, actually..." use ONLY the final corrected value
- Map informal caregiver language to clinical terms:
  - "can't walk" / "can't get around" → mobility = "dependent"
  - "needs some help" / "with assistance" → level = "assistance"
  - "does it on his own" / "independent" → level = "independent"
  - "Hoyer" / "Hoyer lift" → equipment = ["Hoyer lift"]
  - "briefs" / "diapers" → incontinence product reference
- Score confidence 0.0 for vague/ambiguous statements (like "he needs more help now" with no specifics) and set field_path to "AMBIGUOUS"
- For care level fields (mobility, bathing, eating, toileting, dressing, transfers), the ONLY valid values are: "independent", "assistance", "dependent"
- For equipment arrays, APPEND to existing list, do not replace
- For medication changes, include drug name, dosage, and frequency if mentioned

**SYSTEM_PROMPT for critic (Node: llm_critic):**

The critic prompt must instruct Claude to:
- Verify each proposed update from the mapping step
- For each update, check three things:
  1. Does the source_phrase actually appear (or closely match) in the transcript?
  2. Is the field_path the correct field for this type of clinical information?
  3. Is the new_value the right interpretation of the source_phrase?
- If an update is correct, keep it unchanged
- If field_path is wrong, fix it to the correct path
- If new_value is wrong, fix it
- If the update is hallucinated (source_phrase not in transcript), set confidence to 0.0
- If confidence seems too high or too low, adjust it
- Output ONLY the corrected JSON array — no explanation

**build_user_prompt(transcript, patient_json, schema_paths) function:**

Must dynamically construct the user prompt containing:
- The transcript text wrapped in triple quotes
- The current patient JSON (ONLY the relevant care sections — not the full 200+ fields, to save tokens and cost). If the transcript mentions "bathing" and "mobility", send only those sections plus document_meta.
- The complete list of valid field paths (auto-generated from the Pydantic schema)
- Clear instruction: "Extract all updates and output a JSON array"

### nodes/transcribe.py — Amazon Transcribe Medical

- The audio file is ALREADY in S3 (Flutter uploaded it directly). Do NOT try to upload it.
- Read the audio_s3_key from the pipeline state
- Start a Transcribe Medical batch job using boto3:
  - Job name: "samni-{run_id}" (must be unique)
  - Media URI: "s3://{bucket}/{audio_s3_key}"
  - Output bucket: same S3 bucket, key: "transcripts/{patient_id}/{run_id}.json"
  - Language: "en-US"
  - Specialty: "PRIMARYCARE"
  - Type: "DICTATION" (single speaker, not conversation)
- Poll for job completion (check status every 5 seconds, timeout after 5 minutes)
- When complete, fetch the transcript JSON from the output S3 location
- Extract the full transcript text and per-segment data (including confidence scores per segment)
- Return transcript text and segments to the pipeline state
- Handle errors: job failed, timeout, audio too short, unsupported format

### nodes/load_patient_json.py -Load patient JSON from PostgresSQL
- This node runs ONLY in the reassessment pipeline (never in intake)
- Get an async database session from database.py
- Query: SELECT assessment FROM patients WHERE patient_id = :patient_id
- If the patient exists: write the assessment JSONB to master_json in state
- If the patient does NOT exist: raise an error "Patient {patient_id} not found. Complete intake first."
- Log: "Loaded patient JSON for {patient_id}" (never log the JSON content — PHI)

### nodes/llm_map.py — Bedrock + Claude Field Mapping

- Load the current patient JSON from state (master_json)
- Build the user prompt using prompts.build_user_prompt()
- Call Bedrock invoke_model() via boto3:
  - modelId: from config (BEDROCK_MODEL_ID)
  - body: JSON with messages array (system + user), temperature: 0.1, max_tokens: 4096
  - The Bedrock request format for Claude is the Messages API format
- Parse the response:
  - Extract the text content from response body
  - Strip markdown fences (```json ... ```) if present
  - Try json.loads() on the cleaned text
  - If json.loads fails, try to find the first [ ... ] in the response using regex
  - If that also fails, retry ONCE with a simplified prompt that emphasizes "output ONLY JSON"
- Validate each update using the UpdateObject Pydantic model
  - Skip invalid updates (log a warning with the field_path, not the value)
- Return list of UpdateObject items to state as proposed_updates
- Log: number of updates found, average confidence score (never log the actual values — PHI)

### nodes/llm_critic.py — Bedrock + Claude Verification

- Take proposed_updates from state
- Build critic prompt with: original transcript + proposed updates JSON + valid field paths
- Call Bedrock invoke_model() with same settings as llm_map
- Parse and validate the corrected updates (same parsing logic as llm_map)
- Return verified_updates to state
- Log: number of updates changed by critic, number removed (set to 0.0 confidence)

### nodes/confidence.py — Confidence Router

- Iterate through verified_updates from state
- Split into two lists:
  - auto_approved: updates where ALL of these are true:
    - confidence >= CONFIDENCE_THRESHOLD (0.85)
    - field_path is NOT "AMBIGUOUS"
    - source_phrase is not empty/null
  - flagged_updates: everything else (any of the above conditions fails)
- Write both lists to state
- Log: "Auto-approved: {count}, Flagged for review: {count}" (counts only, never content)

### nodes/human_review.py — LangGraph Interrupt

- Check state: if flagged_updates is empty, this node should not have been reached (conditional routing should have skipped it). If somehow reached with empty flagged list, just pass through.
- If flagged_updates is not empty:
  - Update pipeline status in PostgreSQL pipeline_runs table to "waiting_review"
  - Store the flagged_updates in the pipeline_runs.flagged_updates JSONB column
  - Call notifications.notify_review_needed() — sends push to caregiver's phone
  - Call LangGraph interrupt() with payload:
    - flagged_updates list
    - patient_id
    - relevant patient context (current values of the flagged fields)
  - Pipeline execution STOPS here. Server is freed.
  - When POST /review/{patient_id} is called, LangGraph resumes this node
  - The human_decisions are now available in state
  - Process decisions:
    - action "approve" → move the update to approved_updates as-is
    - action "reject" → drop the update entirely
    - action "edit" → use the edited_value as the new_value, keep everything else
  - Combine: approved_updates = auto_approved + newly approved from human review

### nodes/merge.py — JSON Deep Merge

- Take approved_updates from state (combined auto + human approved)
- For each update:
  - Use utils.get_nested(master_json, field_path) to get the old value
  - If old_value equals new_value, skip (nothing changed)
  - Use utils.set_nested(master_json, field_path, new_value) to apply the change
  - Create an audit entry dict:
    - field_path: str
    - old_value: str(old_value) or null
    - new_value: str(new_value)
    - source_phrase: from the update
    - confidence: from the update
    - user_id: from state
    - approval_method: "auto" if confidence >= threshold else "human"
    - timestamp: current UTC time in ISO format
- Update master_json.document_meta.last_updated_by = user_id
- Update master_json.document_meta.last_updated_at = current UTC ISO timestamp
- Write final_json and audit_entries to state

### nodes/save_json.py — Save to PostgreSQL

- Take master_json (or final_json) from state
- Get an async database session from database.py
- Upsert to PostgreSQL patients table:
  - Use `INSERT ... ON CONFLICT (patient_id) DO UPDATE SET assessment = :json, updated_at = now(), updated_by = :user_id`
  - Or use SQLAlchemy `session.merge()` with the Patient model
  - The assessment column (JSONB) stores the complete patient JSON object
- Commit the transaction
- Handle integrity errors gracefully

### nodes/audit.py — Audit Trail

- Take audit_entries from state
- Get an async database session from database.py
- Write EACH entry as a separate row to the PostgreSQL audit_trail table:
  - Use `session.add_all([AuditEntry(...) for entry in audit_entries])`
  - Columns: patient_id, run_id, field_path, old_value, new_value, source_phrase, confidence, user_id, approval_method
  - created_at is auto-set by server_default
- NEVER update or delete existing rows — this table is append-only
- Also update the pipeline_runs row: set status = "complete", completed_at = now(), output_pdf_s3_key = the generated PDF path
- Commit the transaction (audit entries + pipeline status update in one transaction — this is a PostgreSQL advantage)

### nodes/parse_pdf.py — PLACEHOLDER

- This is the other team's code. Create a mock version for testing.
- The mock should:
  - Accept an s3_key string
  - Return a sample patient JSON dict with realistic structure (a few care sections populated)
  - Log: "PLACEHOLDER: parse_pdf called for {s3_key}"
- When the team delivers their parser, they replace this file.

### nodes/generate_pdf.py — PLACEHOLDER

- This is the other team's code. Create a mock version for testing.
- The mock should:
  - Accept patient_json dict, changes set (field paths that changed), output_s3_key string
  - Write a dummy text file to S3 at the output_s3_key (simulating a PDF)
  - Return the output_s3_key
  - Log: "PLACEHOLDER: generate_pdf called with {len(changes)} changes"
- When the team delivers their generator, they replace this file.

### pipeline.py — LangGraph Graph Definition

Define TWO separate graphs:

**Intake Graph** (triggered by pipeline_type == "intake"):
```
parse_pdf → save_json → END
```
Simple, linear. No LLM, no audio, no confidence check.

**Reassessment Graph** (triggered by pipeline_type == "reassessment"):
```
load_patient_json → transcribe → llm_map → llm_critic → confidence_check → [CONDITIONAL EDGE]
  → IF flagged_updates is empty: merge → generate_pdf → save_json → audit → END
  → IF flagged_updates is not empty: human_review → merge → generate_pdf → save_json → audit → END
```

Note: the reassessment graph starts with a "load_patient_json" node that reads the existing patient JSON from PostgreSQL patients table (SELECT assessment FROM patients WHERE patient_id = :id). This is separate from parse_pdf (which is intake-only).

Both graphs must:
- Use PostgreSQL as the checkpoint backend via langgraph-checkpoint-postgres (the official, most mature LangGraph checkpointer — for crash recovery and interrupt/resume)
- Have proper error handling: if any node throws an exception, catch it, set status to "failed" with error message in PostgreSQL pipeline_runs table, and stop the pipeline
- Log node entry and exit with the run_id for debugging
- Include the run_id in all state operations so pipeline runs are traceable

### auth.py — Cognito Token Validation

- Create a FastAPI dependency (using Depends()) that validates the Authorization header
- Extract the Bearer token from the header
- Download Cognito's JWKS (JSON Web Key Set) from the well-known URL (cache it, don't download on every request)
- Decode and verify the JWT token:
  - Verify signature against Cognito's public keys
  - Verify token is not expired
  - Verify the audience (aud) matches your COGNITO_APP_CLIENT_ID
  - Verify the issuer (iss) matches your Cognito User Pool URL
- Extract user_id (sub claim) and email from the token
- In dev mode (ENVIRONMENT=dev), allow a bypass: if the Authorization header is "Bearer dev-test-token", return a hardcoded test user without validating. This lets you test endpoints without setting up Cognito locally.
- Return a User object with id, email, role

### models.py — Pydantic Models

Define these request/response models:

```
# Requests
UploadUrlRequest:
  - patient_id: str
  - file_type: Literal["pdf", "audio"]

IntakeRequest:
  - patient_id: str
  - s3_key: str

ReassessmentRequest:
  - patient_id: str
  - s3_key: str

ReviewDecision:
  - update_id: str
  - action: Literal["approve", "reject", "edit"]
  - edited_value: Optional[Any] = None

ReviewSubmission:
  - run_id: str
  - decisions: list[ReviewDecision]

# Responses
UploadUrlResponse:
  - upload_url: str
  - s3_key: str
  - expires_in: int

PipelineResponse:
  - run_id: str
  - status: str
  - message: str

PatientStatusResponse:
  - patient_id: str
  - run_id: Optional[str]
  - status: str
  - started_at: Optional[str]
  - completed_at: Optional[str]
  - error: Optional[str]
  - has_pending_review: bool

ReviewResponse:
  - pending: bool
  - run_id: Optional[str]
  - flagged_updates: list[dict]
  - patient_context: Optional[dict]

DownloadUrlResponse:
  - download_url: str
  - expires_in: int
  - generated_at: str

# Internal models (not API responses)
UpdateObject:
  - field_path: str
  - new_value: Any
  - source_phrase: str
  - reasoning: str
  - confidence: float (0.0 to 1.0)

SQSMessage:
  - run_id: str
  - patient_id: str
  - s3_key: str
  - pipeline_type: Literal["intake", "reassessment"]
  - user_id: str

HealthResponse:
  - status: str
  - version: str
  - environment: str
```

### utils.py — Helper Functions

Must include:
- `get_nested(d: dict, path: str) -> Any` — traverse a dotted path like "care_sections.mobility.in_room" and return the value
- `set_nested(d: dict, path: str, value: Any) -> None` — set a value at a dotted path, creating intermediate dicts as needed
- `generate_run_id() -> str` — return a UUID4 string
- `utc_now_iso() -> str` — return current UTC time as ISO 8601 string
- `strip_json_fences(text: str) -> str` — remove ```json and ``` from LLM output
- `safe_json_parse(text: str) -> list | dict | None` — try json.loads, then try regex extraction, return None on failure

### Dockerfile

- Base image: python:3.11-slim
- Install system dependencies: only what FastAPI, asyncpg, and boto3 need (no WeasyPrint — that's the other team's concern)
- Install postgresql-client (needed for asyncpg to connect to PostgreSQL)
- Copy requirements.txt and pip install
- Copy application code
- Expose port 8080
- CMD: start BOTH the FastAPI server AND the SQS worker. Use a simple shell script:
  ```
  uvicorn main:app --host 0.0.0.0 --port 8080 &
  python worker.py &
  wait
  ```

### requirements.txt

```
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
langgraph>=0.2.0
langchain-core>=0.2.0
langgraph-checkpoint-postgres>=2.0.0
sqlalchemy[asyncio]>=2.0.0
asyncpg>=0.29.0
alembic>=1.13.0
boto3>=1.34.0
pydantic>=2.6.0
python-multipart>=0.0.9
httpx>=0.27.0
python-jose[cryptography]>=3.3.0
cachetools>=5.3.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
moto[all]>=5.0.0
```

### tests/

- Use pytest with pytest-asyncio for async tests
- Mock all AWS services using moto (S3, SQS, etc.)
- Use a test PostgreSQL database (create a samni_test database, or use testcontainers-python to spin up a temporary PostgreSQL in Docker for tests)
- test_dictations.json should contain 30+ test sentences covering:
  - Mobility changes ("he can't walk anymore, needs the Hoyer now")
  - Bathing changes ("totally dependent for bathing, bed bath twice a week")
  - Eating changes ("pureed diet, one-to-one feeder")
  - Medication additions ("started on metoprolol 25mg twice daily")
  - Toileting changes ("incontinent of bladder, briefs changed every 2 hours")
  - Dressing changes ("needs full help getting dressed")
  - Self-corrections ("she's dependent for dressing — wait, actually she does it with setup help")
  - Vague statements ("he needs more help now" — should be FLAGGED, not auto-approved)
  - Equipment mentions ("fall risk, bed rails up at night, Hoyer lift for transfers")
  - Multiple updates in one sentence ("can't walk and needs bed bath twice a week")
  - Out-of-order updates (bathing, then mobility, then back to bathing)
  - Medication with dosage ("metoprolol 25mg twice daily for blood pressure")
  - Resistive behavior ("resistive to showering, will accept bed bath only")

Each test case should have:
  - text: the dictation sentence
  - expected_updates: list of { field_path, new_value } that should be produced
  - expected_min_confidence: minimum confidence score expected

## How to Start (Build Order)

Build the project in this exact order. Complete each step before moving to the next.

1. **config.py** and **aws_clients.py** — everything depends on these
2. **database.py** and **db_models.py** — SQLAlchemy engine, session, and table models
3. **alembic init** — set up Alembic, create initial migration that generates all 3 tables
4. **state.py** and **models.py** — define pipeline state and Pydantic models
5. **utils.py** — get_nested, set_nested, and other helpers
6. **auth.py** — Cognito middleware (with dev bypass for testing)
7. **prompts.py** — all LLM prompts for Claude
8. **notifications.py** — SNS push notification helper
9. **nodes/** — build each node one at a time, test independently:
   - Start with **merge.py** (pure Python, no AWS calls, easiest to test)
   - Then **confidence.py** (pure Python logic)
   - Then **save_json.py** and **audit.py** (PostgreSQL operations via SQLAlchemy)
   - Then **transcribe.py** (Transcribe Medical API)
   - Then **llm_map.py** and **llm_critic.py** (Bedrock API)
   - Then **human_review.py** (LangGraph interrupt)
   - **parse_pdf.py** and **generate_pdf.py** are PLACEHOLDERS — just return mock data
10. **pipeline.py** — wire all nodes into the two LangGraph graphs with PostgreSQL checkpoint backend
11. **worker.py** — SQS background worker that runs pipelines
12. **main.py** — wire FastAPI endpoints (presigned URLs, intake, reassessment, review, download, status, health)
13. **Dockerfile** — containerize (runs both FastAPI and worker)
14. **tests/** — write tests for each component
15. **.env.example** and **README.md** — documentation

Start with step 1. Create config.py and aws_clients.py. Show me the complete production-ready code for both files. Then wait for my confirmation before proceeding to step 2.

---

## HOW TO USE THIS PROMPT

### Step 1: Paste PART 1 (from "Role" to "Architecture Decisions" including Key Rules and both IMPORTANT sections)
Say: "Acknowledge you understand this context. Don't write any code yet."

### Step 2: Paste PART 2 (from "Project Structure" to end of "Detailed Specifications")
Say: "Acknowledge you understand the specifications. Don't write any code yet."

### Step 3: Paste PART 3 (the "How to Start" section)
Say: "Now start with step 1. Create config.py and aws_clients.py. Show me the complete production-ready code for both files."

### After each file:
Review the code, then say: "Good. Now create [next file name]."

### If Claude generates skeleton/placeholder code:
Say: "This is placeholder code. I need the full production implementation with complete error handling, logging, and all edge cases covered. Rewrite it completely."

### If you're unsure about generated code:
Come back to this conversation and ask me. I know the full architecture and can verify if what Claude generated is correct.
