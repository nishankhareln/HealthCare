# MASTER PROMPT — Give This to Claude in VS Code

Copy everything below this line and paste it into Claude (VS Code / Claude Code).

---

## Role

You are a senior Python backend engineer. You are building the backend for a healthcare document automation system called Samni Labs. You will write production-ready Python code with proper error handling, logging, and type hints.

## Project Context

This system automates patient assessment updates for assisted living facilities. A caregiver uploads a 36-page state-issued patient assessment PDF once per patient. The system parses it into a structured JSON object (this part is handled by another team — NOT your responsibility). Later, the caregiver records an audio dictation of their reassessment observations. The system transcribes the audio, uses an LLM to map the spoken updates to the correct JSON fields, and generates an updated PDF.

## Your Responsibility (ONLY these parts)

You are building:

1. **FastAPI application** with 5 REST endpoints
2. **LangGraph pipeline** with 10 nodes that orchestrates the entire backend workflow
3. **Integration with Amazon Transcribe Medical** (speech-to-text API calls)
4. **Integration with Amazon Bedrock + Claude** (LLM API calls for field mapping and critic verification)
5. **Confidence-based routing logic** (auto-approve if >= 0.85, route to human review if < 0.85)
6. **Human review interrupt/resume mechanism** using LangGraph's interrupt()
7. **JSON merge logic** (apply approved updates to the master patient JSON)
8. **Audit trail** (record every change with field_path, old_value, new_value, source_phrase, confidence, timestamp)
9. **Integration with AWS services** via boto3 (S3, DynamoDB, Cognito token validation, SNS push notifications)

## NOT Your Responsibility (another team handles these)

- PDF parsing (pdfplumber + PyMuPDF) — they give you a clean JSON object
- PDF generation (Jinja2 + WeasyPrint) — they take a JSON object and produce a PDF
- Flutter mobile app — they consume your API endpoints
- The Pydantic schema definition — they define the PatientAssessment model

You will receive these as imports/interfaces. Assume they exist and work correctly. Create placeholder/mock versions for testing.

## Architecture Decisions (Follow these exactly)

### Tech Stack
- **Framework:** FastAPI (Python 3.11+)
- **Pipeline orchestration:** LangGraph (with DynamoDB checkpoint backend)
- **Speech-to-text:** Amazon Transcribe Medical (via boto3, NOT Whisper)
- **LLM:** Amazon Bedrock + Claude Sonnet (via boto3, NOT local models, NOT OpenAI)
- **File storage:** Amazon S3 (via boto3)
- **Database:** Amazon DynamoDB (via boto3) — NOT PostgreSQL, NOT SQLite
- **Authentication:** Amazon Cognito (validate JWT tokens from the Flutter app)
- **Push notifications:** Amazon SNS (via boto3)
- **PDF parsing:** NOT your job — assume a function `parse_pdf(pdf_path) -> dict` exists
- **PDF generation:** NOT your job — assume a function `generate_pdf(patient_json, changes, output_path) -> str` exists

### Key Rules
- All config (AWS region, S3 bucket names, DynamoDB table names, confidence threshold) must come from environment variables via a config.py file. Never hardcode AWS resource names.
- Temperature for all Bedrock Claude calls: 0.1 (deterministic, not creative)
- Confidence threshold: 0.85 (configurable via env var)
- All patient data is PHI — never log patient names, SSN, DOB, or any identifiable information. Log only patient_id, field_paths, confidence scores, and pipeline status.
- Use async where possible (FastAPI is async-native)
- Every function must have type hints and a docstring
- Use Pydantic models for all request/response schemas

## Project Structure (Create this exact structure)

```
samni-backend/
├── main.py                    # FastAPI app, endpoint definitions, startup
├── config.py                  # All configuration from environment variables
├── pipeline.py                # LangGraph graph definition, node wiring, conditional edges
├── state.py                   # PipelineState TypedDict (shared state between nodes)
├── nodes/
│   ├── __init__.py
│   ├── parse_pdf.py           # PLACEHOLDER — calls team's parser, returns JSON
│   ├── save_json.py           # Save patient JSON to DynamoDB
│   ├── transcribe.py          # Call Amazon Transcribe Medical via boto3
│   ├── llm_map.py             # Call Bedrock + Claude for field mapping
│   ├── llm_critic.py          # Call Bedrock + Claude for self-verification
│   ├── confidence.py          # Check scores, split into auto_apply vs flagged
│   ├── human_review.py        # LangGraph interrupt() for human review
│   ├── merge.py               # Apply approved updates to master JSON, record audit
│   ├── generate_pdf.py        # PLACEHOLDER — calls team's PDF generator
│   └── audit.py               # Write audit trail entries to DynamoDB
├── prompts.py                 # System prompts and user prompt builders for Claude
├── models.py                  # Pydantic models for API requests/responses
├── auth.py                    # Cognito JWT token validation middleware
├── aws_clients.py             # Centralized boto3 client initialization
├── utils.py                   # Helper functions (get_nested, set_nested, etc.)
├── tests/
│   ├── test_dictations.json   # 30+ test sentences with expected outputs
│   ├── test_pipeline.py       # End-to-end pipeline test
│   ├── test_nodes.py          # Unit tests for individual nodes
│   ├── test_merge.py          # Unit tests for merge logic
│   └── conftest.py            # Pytest fixtures (mock AWS clients, sample data)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml         # For local development with localstack (optional)
├── .env.example               # Example environment variables
└── README.md
```

## Detailed Specifications

### config.py

Read all config from environment variables with sensible defaults for local development:

- AWS_REGION (default: us-east-1)
- S3_BUCKET (default: samni-phi-documents-dev)
- DYNAMO_PATIENTS_TABLE (default: Patients)
- DYNAMO_AUDIT_TABLE (default: AuditTrail)
- DYNAMO_PIPELINE_TABLE (default: PipelineState)
- COGNITO_USER_POOL_ID (no default, required in production)
- COGNITO_APP_CLIENT_ID (no default, required in production)
- SNS_TOPIC_ARN (no default, required in production)
- BEDROCK_MODEL_ID (default: anthropic.claude-sonnet-4-20250514)
- CONFIDENCE_THRESHOLD (default: 0.85)
- LOG_LEVEL (default: INFO)
- ENVIRONMENT (default: dev)

### state.py — Pipeline State

```python
class PipelineState(TypedDict):
    # Inputs
    patient_id: str
    user_id: str
    pdf_path: Optional[str]          # S3 key, only for intake
    audio_path: Optional[str]        # S3 key, only for reassessment
    
    # From parser (intake only)
    raw_pages: Optional[list]
    
    # Master JSON
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
    output_pdf_path: Optional[str]
    audit_entries: Optional[list]
    
    # Pipeline metadata
    pipeline_type: str               # "intake" or "reassessment"
    status: str                      # "running", "waiting_review", "complete", "failed"
    error: Optional[str]
```

### main.py — FastAPI Endpoints

5 endpoints:

**POST /intake**
- Auth: required (Cognito token)
- Input: patient_id (form field) + pdf file (multipart upload)
- Action: save PDF to S3, trigger LangGraph intake pipeline (parse PDF → save JSON)
- Response: { run_id, status: "processing" }

**POST /reassessment**
- Auth: required
- Input: patient_id (form field) + audio file (multipart upload)
- Action: save audio to S3, load existing patient JSON from DynamoDB, trigger LangGraph reassessment pipeline (transcribe → LLM map → critic → confidence → merge → generate PDF)
- Response: { run_id, status: "processing" }

**GET /review/{patient_id}**
- Auth: required
- Action: check if there are flagged items pending review for this patient
- Response: { pending: true/false, flagged_updates: [...], patient_context: {...} }

**POST /review/{patient_id}**
- Auth: required
- Input: list of decisions, each with { update_id, action: "approve" | "reject" | "edit", edited_value?: ... }
- Action: resume the paused LangGraph pipeline with the human's decisions
- Response: { status: "resumed" }

**GET /download/{patient_id}**
- Auth: required
- Action: get the latest generated PDF for this patient from S3
- Response: streaming file response (PDF)

Also add:
- **GET /health** — health check endpoint, no auth
- **GET /patient/{patient_id}/status** — pipeline status check

### prompts.py — LLM Prompts

**SYSTEM_PROMPT for field mapping (Node: llm_map):**

The system prompt must instruct Claude to:
- Act as a medical document update interpreter
- Extract every clinically relevant update from the transcript
- Map each update to the correct JSON field path from the provided schema
- Output ONLY a valid JSON array with objects containing: field_path, new_value, source_phrase, reasoning, confidence
- Never invent information not in the transcript
- Handle self-corrections (use only the final stated value)
- Map informal language to clinical terms (e.g., "can't walk" → mobility = dependent)
- Score confidence 0.0 for vague/ambiguous statements and set field_path to "AMBIGUOUS"
- For care level fields, only valid values are: "independent", "assistance", "dependent"

**SYSTEM_PROMPT for critic (Node: llm_critic):**

The critic prompt must instruct Claude to:
- Verify each proposed update from the mapping step
- Check if source_phrase actually appears in the transcript
- Check if field_path is correct for the type of information
- Check if new_value is the right interpretation
- Fix errors in-place
- Set confidence to 0.0 for hallucinated updates
- Output the corrected JSON array

**build_user_prompt() function:**

Must dynamically construct the user prompt containing:
- The transcript text
- The current patient JSON (only relevant sections, not the full 200+ fields — to save tokens)
- The list of valid field paths (auto-generated from the schema)

### nodes/transcribe.py — Amazon Transcribe Medical

- Upload audio file to S3 (if not already there)
- Start a Transcribe Medical batch job using boto3 client
- Poll for job completion (or use async waiting)
- Fetch the transcript from the output S3 location
- Return the full transcript text and per-segment data
- Handle errors gracefully (audio too short, unsupported format, service error)

Important: Use medical specialty "PRIMARYCARE", language "en-US"

### nodes/llm_map.py — Bedrock + Claude Field Mapping

- Load the current patient JSON from state
- Build the prompt using prompts.py
- Call Bedrock invoke_model() with:
  - modelId from config (default: anthropic.claude-sonnet-4-20250514)
  - temperature: 0.1
  - max_tokens: 4096
- Parse the response: strip markdown fences if present, extract JSON array
- Validate each update against valid field paths using Pydantic
- Return list of UpdateObject items
- If parsing fails, retry once with a simplified prompt
- Log the number of updates found (but NOT the content — PHI)

### nodes/llm_critic.py — Bedrock + Claude Verification

- Take proposed_updates from state
- Build critic prompt with the original transcript and proposed updates
- Call Bedrock invoke_model() with same settings
- Parse and validate the corrected updates
- Return verified_updates

### nodes/confidence.py — Confidence Router

- Iterate through verified_updates
- Split into two lists:
  - auto_approved: confidence >= CONFIDENCE_THRESHOLD and field_path != "AMBIGUOUS" and source_phrase is not empty
  - flagged_updates: everything else
- Return both lists
- Log counts (not content)

### nodes/human_review.py — LangGraph Interrupt

- If flagged_updates is not empty:
  - Call LangGraph interrupt() with the flagged items and patient context
  - This pauses the pipeline — execution stops here
  - When resumed (via POST /review), the human_decisions are available in state
  - Process decisions: approved items go to approved_updates, rejected items are dropped, edited items use the edited value
- If flagged_updates is empty:
  - This node should be skipped via conditional routing

### nodes/merge.py — JSON Deep Merge

- Take approved_updates (from auto + human combined)
- For each update:
  - Get the old value from master_json using get_nested()
  - Set the new value using set_nested()
  - Create an audit entry with: field_path, old_value, new_value, source_phrase, confidence, user_id, approval_method ("auto" or "human"), timestamp (UTC ISO format)
- Update document_meta.last_updated_by and last_updated_at
- Return the updated JSON and the list of audit entries

### nodes/audit.py — Audit Trail

- Write each audit entry as a separate item to the AuditTrail DynamoDB table
- Partition key: patient_id
- Sort key: timestamp
- Never modify or delete existing entries (append-only)
- Also update the Patients table with the final_json

### pipeline.py — LangGraph Graph Definition

Define two graphs:

**Intake Graph** (for POST /intake):
```
parse_pdf → save_json → END
```

**Reassessment Graph** (for POST /reassessment):
```
transcribe → llm_map → llm_critic → confidence_check → [conditional]
  → if no flagged: merge → generate_pdf → audit → END
  → if flagged: human_review → merge → generate_pdf → audit → END
```

Both graphs must:
- Use DynamoDB as the checkpoint backend (for crash recovery)
- Have proper error handling (if any node fails, set status to "failed" with error message)
- Log node entry/exit for debugging

### aws_clients.py — Centralized AWS Clients

Create and export boto3 clients for:
- s3_client
- dynamodb_resource (use resource, not client, for simpler DynamoDB operations)
- transcribe_client
- bedrock_client (bedrock-runtime)
- sns_client
- cognito_client (cognito-idp)

All clients should use the region from config.py. In local dev, they should work with AWS credentials from environment or ~/.aws/credentials.

### auth.py — Cognito Token Validation

- Create a FastAPI dependency that validates the Authorization header
- Decode the JWT token from Cognito
- Verify the signature using Cognito's JWKS endpoint
- Extract user_id and roles from the token claims
- In dev mode (ENVIRONMENT=dev), allow a bypass with a hardcoded test token
- Return a User object with id, email, role

### models.py — Pydantic Models

Define these request/response models:

- IntakeRequest (patient_id)
- ReassessmentRequest (patient_id)  
- ReviewDecision (update_id, action: approve/reject/edit, edited_value)
- ReviewSubmission (decisions: list[ReviewDecision])
- UpdateObject (field_path, new_value, source_phrase, reasoning, confidence)
- PipelineResponse (run_id, status, message)
- ReviewResponse (pending, flagged_updates, patient_context)
- PatientStatusResponse (patient_id, status, last_updated, has_pending_review)

### Dockerfile

- Base image: python:3.11-slim
- Install system dependencies for WeasyPrint (libpango, libcairo, etc.)
- Copy requirements.txt and install
- Copy application code
- Expose port 8080
- CMD: uvicorn main:app --host 0.0.0.0 --port 8080

### tests/

- Use pytest with pytest-asyncio for async tests
- Mock all AWS services using moto or manual mocking
- test_dictations.json should contain 30+ test sentences covering:
  - Mobility changes ("he can't walk anymore, needs the Hoyer now")
  - Bathing changes ("totally dependent for bathing, bed bath twice a week")
  - Eating changes ("pureed diet, one-to-one feeder")
  - Medication additions ("started on metoprolol 25mg twice daily")
  - Toileting changes ("incontinent of bladder, briefs changed every 2 hours")
  - Self-corrections ("she's dependent for dressing — wait, actually she does it with setup help")
  - Vague statements ("he needs more help now" — should be flagged)
  - Equipment mentions ("fall risk, bed rails up at night")
  - Multiple updates in one sentence
  - Out-of-order updates (bathing, then mobility, then back to bathing)

## How to Start

Build the project in this order:

1. **config.py** and **aws_clients.py** first — everything depends on these
2. **state.py** and **models.py** — define all data structures
3. **utils.py** — get_nested, set_nested, and other helpers
4. **auth.py** — Cognito middleware (with dev bypass)
5. **prompts.py** — all LLM prompts
6. **nodes/** — build each node one at a time, test independently:
   - Start with merge.py (pure Python, no AWS calls, easy to test)
   - Then confidence.py (pure Python logic)
   - Then save_json.py and audit.py (DynamoDB operations)
   - Then transcribe.py (Transcribe Medical API)
   - Then llm_map.py and llm_critic.py (Bedrock API)
   - Then human_review.py (LangGraph interrupt)
   - parse_pdf.py and generate_pdf.py are PLACEHOLDERS — just return mock data
7. **pipeline.py** — wire all nodes into the LangGraph graph
8. **main.py** — wire FastAPI endpoints to trigger the graphs
9. **Dockerfile** — containerize
10. **tests/** — write tests for each component

Start with step 1. Create config.py and aws_clients.py. Then proceed step by step. Ask me before moving to the next step if you're unsure about anything.
