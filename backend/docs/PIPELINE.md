# Samni Labs — LangGraph Pipeline

This is the deepest internal document. Read it when you need to understand how a single voice dictation becomes a row update in PostgreSQL.

For the high-level system context, see [ARCHITECTURE.md](ARCHITECTURE.md).
For step-by-step user scenarios, see [SYSTEM_FLOW.md](SYSTEM_FLOW.md).
For the REST API contract, see [API_ENDPOINTS.md](API_ENDPOINTS.md).

---

## What is LangGraph and why we use it

LangGraph is a Python library for building **stateful, branching workflows** out of plain Python functions ("nodes"). Each node receives the current workflow state, does something, and returns updates that get merged back into the state. The library handles wiring (which node runs after which), conditional branches, and — most importantly for us — **persistent checkpoints**.

We use LangGraph for three things plain Python could not give us:

1. **Resumable workflows** — the reassessment graph can pause for human review and resume hours or days later from the exact same state, on a different worker, after a process restart. The checkpointer makes this work.
2. **Conditional routing** — after the confidence node, the graph either continues straight to merge (everything was high-confidence) or detours through human review (some things weren't). We declare the rule once; LangGraph picks the path.
3. **Failure recovery** — if a worker crashes mid-graph, SQS redelivers the message. The graph picks up from the last successful checkpoint instead of redoing everything (no re-transcribing 5 minutes of audio because of a transient Bedrock error).

---

## The state object — `PipelineState`

Every node reads from and writes to the same shared dict. Defined in [backend/state.py](../backend/state.py) as a `TypedDict`.

| Key | Type | Set by | Meaning |
|---|---|---|---|
| `run_id` | str | API at job creation | Unique ID for this pipeline run |
| `patient_id` | str (9-digit ACES) | API at job creation | Resident this run belongs to |
| `user_id` | str | API at job creation | Caregiver who started the run |
| `pipeline_type` | str | API at job creation | `"intake"` or `"reassessment"` |
| `pdf_s3_key` | str? | API (intake only) | Where the uploaded PDF lives in S3 |
| `audio_s3_key` | str? | API (reassessment only) | Where the uploaded audio lives in S3 |
| `master_json` | dict | `parse_pdf` (intake) or `load_patient_json` (reassessment) | Resident's structured data at the start of the run |
| `transcript` | str? | `transcribe` | Audio → text |
| `transcript_s3_key` | str? | `transcribe` | Where the cached transcript JSON lives |
| `proposed_updates` | list | `llm_map` | LLM's first-pass field updates |
| `verified_updates` | list | `llm_critic` | Same list with confidence scores |
| `auto_approved` | list | `confidence` | Updates that bypass human review |
| `flagged_updates` | list | `confidence` | Updates the caregiver must approve |
| `decisions` | list | API (resume) | Caregiver's approve/reject/edit choices |
| `final_json` | dict | `merge` | Merged final state of the assessment |
| `audit_entries` | list | `merge` | One entry per applied change |

**PHI safety rule:** `summarize_state()` returns only counts and IDs. No node ever logs raw state contents directly.

---

## The two graphs

### Intake graph — runs once per new resident

```
START → parse_pdf → save_json → audit → END
```

When a caregiver uploads a filled DSHS Assessment Details PDF, this graph runs once. It uses **Bedrock Claude Sonnet 4.5** (multimodal — accepts the PDF directly) to extract every field. No human review at this stage — the caregiver reviews in the Flutter form view afterward and corrects via `PATCH /patients/{id}` if needed.

### Reassessment graph — runs every voice update

```
START → load_patient_json → transcribe → llm_map → llm_critic → confidence
                                                                    │
                                              ┌─────────────────────┴─────────────────┐
                                              │                                       │
                                  (any flagged?)                              (all auto-approved?)
                                              │                                       │
                                              ▼                                       ▼
                                       human_review                                  merge
                                              │                                       │
                                              └────────► merge ◄──────────────────────┘
                                                          │
                                                          ▼
                                               save_json → audit → END
```

The conditional edge after `confidence` is the key feature. The graph either runs straight through, or detours through `human_review` (which pauses the entire pipeline mid-flight until the caregiver responds via `POST /review/{patient_id}`).

Both graphs end with `save_json` and `audit`. **Neither graph generates a PDF** — Flutter renders the WAC-388-76-615 template on-device.

---

## Every node in detail

Each node lives in [backend/nodes/](../backend/nodes/) as its own module.

---

### `parse_pdf` *(intake only)*

**File:** [backend/nodes/parse_pdf.py](../backend/nodes/parse_pdf.py)

**Job:** Read the uploaded DSHS PDF from S3 and extract every field into structured JSON.

**Input:** `pdf_s3_key`, `patient_id`.

**Output:** `master_json` — a fully populated assessment dict.

**Approach:** Calls **Bedrock Claude Sonnet 4.5** with the PDF as multimodal input. Claude is constrained by a strict output JSON schema so the response can be validated by Pydantic immediately. The schema is the same one Flutter uses to render the template — single source of truth.

**Why an LLM and not a deterministic parser:** The DSHS form mixes structured fields (checkboxes, table cells), narrative free-text ("describe behaviors"), and visual cues. Writing a deterministic parser would take weeks and break the moment DSHS adjusts the layout. An LLM handles all of this in one call.

**Status today:** This node is **placeholder** until we ship the real Bedrock call. The wiring (S3 read, run row, audit) all works; only the parser body is stubbed.

**Security:**
- The S3 key must start with `uploads/` (validated).
- PDF size and page count are bounded so a malicious 10 GB upload cannot OOM the worker.
- Claude is run with `temperature=0` for determinism. The output is re-validated against our Pydantic schema; anything not in the schema is rejected.
- The PDF and Claude's response are PHI — never logged.

---

### `load_patient_json` *(reassessment only)*

**File:** [backend/nodes/load_patient_json.py](../backend/nodes/load_patient_json.py)

**Job:** Fetch the resident's current `assessment` JSON from PostgreSQL so subsequent nodes can compare against it.

**Input:** `patient_id`.

**Output:** `master_json` — the resident's current state.

**Side effects:** One read of the `patients` row. No writes.

**Why this exists:** The LLM needs the existing context to decide whether a transcript is talking about an *update* (BP changed) versus a *first-time entry* (BP wasn't recorded before). Without this node, every reassessment would treat the resident as new.

**Security:** Refuses to load if the row is missing — the API should have rejected the request earlier, but defense in depth. Validates the ACES ID format before querying.

---

### `transcribe` *(reassessment only)*

**File:** [backend/nodes/transcribe.py](../backend/nodes/transcribe.py)

**Job:** Take the audio file from S3 and turn it into text by calling **Amazon Transcribe Medical**.

**Input:** `audio_s3_key`, `run_id`.

**Output:** `transcript` (the text), `transcript_s3_key` (where the cached result JSON lives).

**Side effects:**
- Starts an Amazon Transcribe Medical async batch job pointing at the audio S3 object. The job name is `samni-{run_id}-{short-uuid}` (the suffix prevents collisions if SQS redelivers and triggers a retry).
- Tells Transcribe to write its result back into our own S3 bucket at `transcripts/{run_id}.json`. We never use Amazon-managed output (would put PHI in a bucket outside our KMS scope).
- Polls the job status every `TRANSCRIBE_POLL_INTERVAL_SECONDS` (default 5 s) up to `TRANSCRIBE_TIMEOUT_SECONDS` (default 300 s = 5 min). If it doesn't complete, the run goes to `failed`.
- Reads the result JSON from S3, extracts the transcript text, and places it on `state.transcript`.

**Idempotency:** If a previous worker run already produced a transcript at `transcripts/{run_id}.json`, this node short-circuits and reuses it instead of re-running Transcribe (Transcribe charges per minute of audio).

**Configuration:**
- `TRANSCRIBE_LANGUAGE_CODE` — `"en-US"` by default.
- `TRANSCRIBE_SPECIALTY` — `"PRIMARYCARE"`. Significantly more accurate than generic Transcribe for caregiver dictations.
- `TRANSCRIBE_TYPE` — `"DICTATION"`. Single-speaker model — beats conversation mode for voice notes.

**Security:**
- The audio file and transcript are PHI. Never logged. Only byte sizes, durations, and identifiers reach the logs.
- The S3 key validation ensures only `audio/` keys can be sent to Transcribe.
- Transcribe job names use only alphanumerics, dots, dashes, underscores — no injection vector.
- Transcript size is capped at 4 MiB before reading.

---

### `llm_map` *(reassessment only)*

**File:** [backend/nodes/llm_map.py](../backend/nodes/llm_map.py)

**Job:** Send the transcript + the current JSON to **Bedrock Claude Sonnet 4.5** and receive a list of proposed field updates.

**Input:** `transcript`, `master_json`.

**Output:** `proposed_updates` — list of `{field_path, new_value, source_phrase, reasoning}`.

**The prompt is the heart of the system.** Claude is told:
- The schema of valid field paths (it can only update these).
- The current state of the assessment.
- The transcript.
- It MUST return JSON matching `UpdateObject`. Any field path outside the schema is forbidden.
- For each proposed update, include the exact source phrase from the transcript that justifies it.

**Output validation:** Every proposal goes through `pydantic.UpdateObject` before being added to state. Malformed → ValidationError → run fails. Hallucinated field path → schema mismatch → run fails.

**Why structured JSON output and not free text:** Free-text LLM outputs are nondeterministic to parse. Structured output (Bedrock's `response_format` with a JSON schema) gives us a typed contract. If Claude can't produce the schema, that's a clear error, not a guess.

**Security:**
- `BEDROCK_TEMPERATURE = 0.1` — deterministic outputs to minimize hallucination.
- `BEDROCK_MAX_TOKENS` is bounded so a runaway response cannot fill the DB.
- The prompt explicitly forbids inventing field paths — the critic catches anything that leaks through.
- The Bedrock invocation runs in our own AWS account; data is **never used for model training**.

---

### `llm_critic` *(reassessment only)*

**File:** [backend/nodes/llm_critic.py](../backend/nodes/llm_critic.py)

**Job:** A **second** Bedrock Claude call that reviews the first one's proposed updates. Returns the same list with a `confidence` score (0.0 – 1.0) added per item.

**Input:** `proposed_updates`, `transcript`, `master_json`.

**Output:** `verified_updates` — same shape as proposed but with confidence scored.

**Why a critic step exists:** The first LLM is biased toward making *some* update for every transcript. The critic is prompted explicitly to be skeptical:
- "Is this field path actually justified by the transcript?"
- "Does the new value match the source phrase precisely?"
- "Is the proposed change reasonable given the resident's current state?"

The critic can also mark items as `AMBIGUOUS` (a sentinel field path) — meaning *"the transcript suggests an update but I can't pick a clean target field"*. AMBIGUOUS items always go to human review regardless of confidence.

**Security:** Same posture as `llm_map`. Output is re-validated against the schema. The critic CANNOT add new updates the mapper didn't propose — it only scores or rejects.

---

### `confidence` *(reassessment only)*

**File:** [backend/nodes/confidence.py](../backend/nodes/confidence.py)

**Job:** Split `verified_updates` into two buckets based on the configured `CONFIDENCE_THRESHOLD` (default 0.85).

**Input:** `verified_updates`.

**Output:**
- `auto_approved` — confidence ≥ threshold AND field_path is not AMBIGUOUS.
- `flagged_updates` — everything else.

**No external calls.** Pure routing logic in Python.

**The conditional edge after this node** decides:
- If `flagged_updates` is non-empty → go through `human_review` (which interrupts the graph).
- If `flagged_updates` is empty → skip straight to `merge`.

**Why threshold isn't hardcoded:** The confidence threshold is read from settings on every run. Ops can adjust it without restarting workers. We expect to tune it as we collect real-world data.

**Security:** AMBIGUOUS items are *always* flagged regardless of confidence. Any item that fails Pydantic re-validation goes to `flagged`, never silently dropped (a quiet drop would mean a caregiver's statement never reaches the record).

---

### `human_review` *(reassessment, conditional)*

**File:** [backend/nodes/human_review.py](../backend/nodes/human_review.py)

**Job:** Pause the entire pipeline mid-flight and wait for the caregiver's decisions to arrive via `POST /review/{patient_id}`.

**How it works:**
1. The node calls `langgraph.types.interrupt(...)` with the flagged list and a hash of the run state.
2. LangGraph **saves a checkpoint to PostgreSQL** and raises a special exception that bubbles all the way out of `graph.ainvoke()`.
3. The worker catches this, marks the run as `waiting_review`, fires an SNS notification, and exits cleanly.
4. The SQS message is deleted (no point in redelivering while we wait).
5. **Hours or days later**, the caregiver opens the Flutter app and approves / rejects each item.
6. The API enqueues a "resume" SQS message containing the decisions.
7. A worker picks up the resume message, calls `graph.ainvoke(Command(resume={"decisions": [...]}))`.
8. LangGraph loads the checkpoint, fast-forwards through every completed node, lands inside `human_review` again, and the `interrupt(...)` call now *returns* the decisions instead of raising.
9. Execution continues into `merge`.

**Why this beats a simple flag-and-poll:** No in-flight worker holding a DB connection or process slot for hours. No "I forgot what state we were in" reconstruction logic. The graph is the same paused or running.

**Security:** The hash check ensures the resumed decisions match the run state at the time of pause. If `flagged_updates` is tampered with mid-pause (or the run started over), the hash mismatch causes a hard failure rather than applying decisions to the wrong state.

---

### `merge` *(reassessment only)*

**File:** [backend/nodes/merge.py](../backend/nodes/merge.py)

**Job:** Apply approved updates to `master_json`, producing `final_json`. Build the audit trail entries.

**Input:** `master_json`, `auto_approved`, `flagged_updates`, `decisions`.

**Output:** `final_json`, `audit_entries`.

**Logic:**
1. Start with `master_json` deep-copied.
2. Apply every item in `auto_approved` directly.
3. For each `flagged_updates` item, look up the caregiver's decision:
   - `approve` → apply with the LLM's proposed value.
   - `reject` → skip; record the rejection in audit so we know the proposal was reviewed.
   - `edit` → apply with the caregiver's `edited_value`.
4. For each applied change, build an audit entry with old_value, new_value, source_phrase, confidence, user_id, approval_method (`"auto"` or `"human"`).

**Side effects:** None — pure transformation. Persistence happens in `save_json` and `audit`.

**Security:** Uses `set_nested(...)` which refuses to traverse paths containing `..`, `__`, or other prototype-pollution-style tricks. Every value is bounded in size.

---

### `save_json` *(both graphs)*

**File:** [backend/nodes/save_json.py](../backend/nodes/save_json.py)

**Job:** Write `final_json` (or `master_json` for intake) into the `patients.assessment` column.

**Input:** `final_json` or `master_json`.

**Side effects:** One UPSERT into `patients`:
- For intake → `INSERT ... ON CONFLICT (patient_id) DO UPDATE`.
- For reassessment → `UPDATE patients SET assessment = ..., updated_at = NOW(), updated_by = user_id`.

**Concurrency:** The merge step computed deltas based on `master_json` loaded earlier. If another run modified the row in the meantime, this UPDATE would overwrite their changes. We mitigate by allowing only one pipeline run per resident in `running` status at a time.

**Security:** No raw SQL. Parameterized SQLAlchemy statements. Validates `patient_id` format before query.

---

### `audit` *(both graphs)*

**File:** [backend/nodes/audit.py](../backend/nodes/audit.py)

**Job:** Insert one row per applied change into `audit_trail`.

**Input:** `audit_entries`.

**Side effects:** Bulk INSERT into `audit_trail`.

**Why audit comes AFTER save_json:** If `save_json` fails, no audit entries are written — we don't want a record claiming "we updated field X" when in reality the update never persisted. If `audit` fails after `save_json` succeeds, the run is marked `failed`, but the JSON change is real and visible. We accept this rare ordering anomaly because the alternative (auditing first, then saving) would be worse.

**Append-only invariant:** Application code must NEVER UPDATE or DELETE rows in this table. In production, the DB role used by the app should lack those grants. The append-only property is what makes the audit trail trustworthy to regulators.

**Security:** Records old + new value, source phrase, confidence, user_id, and approval method. Source of truth for "who changed what when".

---

## The PostgreSQL checkpointer

LangGraph's persistence is provided by `langgraph-checkpoint-postgres`. It creates four tables in the same database (alongside our app tables):

- `checkpoints` — one row per saved state snapshot.
- `checkpoint_blobs` — large binary state values stored separately.
- `checkpoint_writes` — per-channel writes since the last full checkpoint.
- `checkpoint_migrations` — version tracking for the checkpointer's own schema.

These are managed automatically by `AsyncPostgresSaver.setup()` in [backend/pipeline.py](../backend/pipeline.py). **Do not include them in our Alembic migrations** — they're owned by the checkpointer library, not by us. Our migrations explicitly exclude them.

**Connection pooling:** The checkpointer uses a separate `psycopg_pool.AsyncConnectionPool` sized to `WORKER_CONCURRENCY * 2`, distinct from the app's SQLAlchemy pool. A stuck checkpointer connection should not exhaust the app's pool, and vice versa.

**Thread ID:** Every run uses `run_id` as its thread ID. Checkpoints are keyed by run; resumes always land on the same run. Multiple residents can run pipelines in parallel without colliding because their `run_id` values differ.

---

## Failure modes and what happens

| Failure | Where | What happens |
|---|---|---|
| PDF parse error | `parse_pdf` | Run → `failed` with bounded error message. SQS message deleted. |
| Transcribe Medical timeout | `transcribe` | Run → `failed`. SQS redelivers if it's a transient AWS issue. |
| Bedrock returns invalid JSON | `llm_map` / `llm_critic` | Pydantic ValidationError. Run → `failed`. The transcript is still in S3 — a human can investigate. |
| Bedrock API timeout / 5xx | `llm_map` / `llm_critic` | Graph raises. SQS redelivers up to 3 times, then DLQ. The checkpoint preserves progress. |
| Caregiver never responds to review | After `human_review` interrupt | Nothing breaks — the checkpoint sits indefinitely. Ops can run a sweep to mark abandoned reviews after N days. |
| Worker process killed mid-graph | Anywhere | SQS visibility timeout (default 900 s) expires. Another worker picks up. The checkpointer resumes from the last successful node. |
| Database goes down | `save_json` / `audit` | Run → `failed`. SQS redelivers. Once DB is back, the run completes from checkpoint. |
| Decisions tampered between pause and resume | `human_review` | Hash check fails. Run → `failed`. No partial application. |

---

## Glossary

| Term | Meaning |
|---|---|
| **Node** | A Python function that takes the pipeline state and returns updates to merge in. |
| **Edge** | A directed link from one node to another. Conditional edges have a routing function. |
| **State** | A `TypedDict` shared across the run. Each node receives the current state and returns a dict of updates. |
| **Checkpoint** | A persisted snapshot of state taken after every node. Enables resume + crash recovery. |
| **Interrupt** | A LangGraph mechanism for pausing mid-graph and waiting for external input (here: caregiver decisions). |
| **Thread ID** | The key under which checkpoints are stored. We use `run_id`. |
| **AMBIGUOUS** | Sentinel field path the critic uses when it can't pick a clean target — always routed to human review. |

---

## Related docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — overall system architecture
- [SYSTEM_FLOW.md](SYSTEM_FLOW.md) — plain-language scenarios
- [API_ENDPOINTS.md](API_ENDPOINTS.md) — REST API reference
- [../backend/pipeline.py](../backend/pipeline.py) — graph construction code
- [../backend/state.py](../backend/state.py) — `PipelineState` definition
- [../backend/nodes/](../backend/nodes/) — every node's source
