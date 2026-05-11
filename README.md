For your workload — scanned, multi-page, state-issued clinical assessment packets with handwritten marks, tables, and “must not silently fail” integrity requirements — I would strongly recommend:

# Final Recommendation

## Rank #1 — Option B (Textract-first → Claude-second)

**Recommended architecture:**

S3 PDF
→ Textract async document analysis
→ normalized intermediate representation (IR)
→ schema-aware extraction/orchestration
→ Claude only for semantic mapping + ambiguity resolution
→ Postgres + confidence/audit metadata

This is the safer architecture operationally, economically, and clinically.

---

# Why Option B Wins

Your real problem is **not OCR**.
Your real problem is:

> “How do I minimize silent structured-data corruption across long scanned clinical packets?”

That changes the architecture decision completely.

LLMs are excellent semantic normalizers.
They are not reliable primary OCR engines for long scanned healthcare forms.

Textract is purpose-built for:

* forms
* tables
* checkboxes
* page geometry
* confidence scoring
* multi-page documents

Claude is purpose-built for:

* reasoning
* schema mapping
* normalization
* semantic reconciliation
* fuzzy interpretation

That separation of responsibilities matters enormously in healthcare workflows.

---

# 1. Accuracy — Which Is Better for Scanned State PDFs?

## Winner: Option B

### Why

With Option A:
Claude is doing:

1. OCR
2. layout understanding
3. checkbox interpretation
4. table reconstruction
5. schema mapping

…all inside one generative inference step.

That creates a large “silent hallucination surface.”

---

With Option B:
Textract handles:

* OCR
* geometric structure
* table extraction
* checkbox detection
* key/value pairing

Claude only handles:

* semantic normalization
* schema alignment
* ambiguity handling

This reduces hallucination opportunities dramatically.

---

# Critical Point

Textract returns:

* confidence scores
* bounding boxes
* explicit table cell relationships
* key-value relationships
* selection elements (checkboxes)

Claude vision does not expose this structure deterministically.

That alone is a major reliability advantage.

---

# AWS Documentation Evidence

Textract `AnalyzeDocument` explicitly supports:

* FORMS
* TABLES
* QUERIES
* SIGNATURES
* LAYOUT ([AWS Documentation][1])

AWS positions Textract specifically for:

* structured document extraction
* OCR on scanned PDFs
* form parsing
* table extraction

Claude-on-PDF is fundamentally a multimodal reasoning path, not a dedicated OCR pipeline.

---

# Handwritten Checkboxes

Textract is not perfect on handwriting, but:

* handwritten checkmarks
* printed forms
* typed text
* tables

…are exactly the domain it was built for.

Claude vision can sometimes outperform on messy semantic interpretation, but:

* consistency is lower
* reproducibility is lower
* confidence visibility is weaker
* failure analysis is harder

For HIPAA-adjacent workflows:
**observable confidence + deterministic structure beats raw multimodal cleverness.**

---

# 2. Real Failure Modes of Option A (Chunking + Merge)

This is where Option A becomes dangerous.

## Failure Mode 1 — Multi-page Tables

Example:
Medication table spans pages 14–17.

Chunking:

* chunk 1 = pages 11–15
* chunk 2 = pages 16–20

Problems:

* repeated headers
* partial rows
* continuation rows
* dosage split from medication name
* merged cells interpreted differently
* page-local reasoning without global context

Claude may:

* duplicate medications
* drop medications
* merge two rows incorrectly
* overwrite dosage frequency

These are clinically dangerous silent failures.

---

# Failure Mode 2 — Scalar Conflict

Example:
Chunk 1:

```json
{
  "mobility_level": "Assist x1"
}
```

Chunk 2:

```json
{
  "mobility_level": "Independent"
}
```

Now what?

You must build:

* provenance tracking
* confidence arbitration
* page-priority rules
* recency logic
* section ownership

Otherwise merge becomes nondeterministic.

---

# Failure Mode 3 — Header Scope Leakage

Example:
“Current Medications” appears once on page 14.

Pages 15–17 contain only rows.

Chunk 2 may lose semantic context and reinterpret rows incorrectly.

---

# Failure Mode 4 — Checkbox Drift

LLMs frequently:

* normalize “unchecked” into null
* infer likely values
* silently fill missing structure

This is catastrophic in clinical extraction.

---

# Merge Logic You Would Need

You would effectively need a mini document database.

## You need:

* page provenance
* section provenance
* field confidence
* conflict resolution policy
* list deduplication
* semantic entity reconciliation
* continuation-page detection

This becomes an entire subsystem.

---

# Recommended Merge Model (if you still choose Option A)

Use:

```json
{
  "value": "...",
  "source_pages": [14,15],
  "confidence": 0.92,
  "extractor": "claude_chunk_3"
}
```

Then:

* scalars → confidence arbitration
* lists → append + fuzzy dedupe
* tables → stable row IDs
* conflicts → loud failure queue

But at this point:
you are reinventing document intelligence infrastructure.

---

# Known Design Pattern

Yes.

This is effectively:

## “Schema-aware document reconciliation”

Patterns commonly used:

* provenance-aware extraction
* event sourcing
* confidence-weighted merge
* document IR (intermediate representation)
* human-in-the-loop escalation

Most production systems do NOT rely on naive JSON merge.

---

# 3. Cost Comparison

## Option A — 6 Claude Calls

Assumptions:

* 12K input tokens × 6
* ~72K input total
* ~8K output total

Claude Sonnet pricing:

* ~$3 / 1M input
* ~$15 / 1M output ([Claude][2])

Approx:

* input: 72K × $3/M = ~$0.22
* output: 8K × $15/M = ~$0.12

Total:

# ~$0.34–0.45 per intake

But this ignores retries and merge failures.

---

## Option B — Textract + Claude

Textract:

* 36 pages
* AnalyzeDocument FORMS+TABLES

Typical:

# ~$0.50–0.70

Claude:

* ~10–15K cleaned extracted text
* ~4K output

Approx:

* input: ~$0.03–0.05
* output: ~$0.06

Total:

# ~$0.60–0.80 per intake

---

# Key Insight

Option B costs maybe:

* 1.5×–2× more

But massively reduces:

* hallucination risk
* merge complexity
* token explosion
* truncation failures
* audit ambiguity

That tradeoff is worth it in healthcare.

---

# 4. Latency Comparison

## Option A

Sequential:
6 Claude calls:

* ~4–10 sec each

Total:

# ~30–60 sec

Parallelized:
Could become:

# ~8–15 sec

BUT:

* Bedrock account quotas matter
* token-per-minute quotas matter
* burst concurrency matters

You may hit:

* throttling
* retry storms
* queue amplification

---

## Option B

Textract async:

* usually 5–20 sec for 36 pages

Claude mapping:

* ~3–8 sec

Total:

# ~10–30 sec typical

More importantly:
latency variance is lower.

---

# 5. Other Options

## Amazon Textract Queries

Very promising for:

* fixed state forms
* known field locations
* deterministic extraction

Example:

* “What is patient DOB?”
* “Mobility assistance level?”
* “Primary diagnosis?”

Great for:

* scalar fields

Less ideal for:

* large medication tables
* freeform notes

Recommendation:
Use Queries selectively for critical fields.

---

## Amazon Comprehend Medical

Useful AFTER extraction.

Good for:

* medication normalization
* ICD concepts
* Rx extraction
* entity linking

Not ideal as primary extractor.

Use it downstream.

---

## Bedrock Data Automation (BDA)

Interesting emerging option.

Pros:

* managed workflow
* orchestration

Cons:

* less mature
* less controllable
* harder debugging
* weaker deterministic guarantees

I would not use BDA yet for high-integrity clinical extraction.

---

## Larger-context multimodal models

Possible alternative:

* Gemini 2.5 Pro
* Claude Sonnet 4.x 1M context

Claude Sonnet supports very large context windows in some deployments. ([Reddit][3])

But:
large context does NOT solve:

* OCR determinism
* confidence scoring
* geometry
* silent hallucination

Bigger context ≠ safer extraction.

---

# 6. HIPAA / Clinical Integrity Perspective

## Strong Winner: Option B

Because:

* extraction becomes observable
* confidence becomes measurable
* provenance becomes trackable
* failures become auditable

---

# Silent Wrong Answer Risk Surface

## Option A Risk

Single multimodal inference step can:

* omit fields
* normalize incorrectly
* infer absent values
* collapse tables
* overwrite repeated values

WITHOUT visibility.

Dangerous.

---

## Option B Risk

Textract failures are usually:

* explicit
* localized
* confidence-scored
* geometrically traceable

That is much safer operationally.

---

# Most Important Principle

In healthcare:

# Loud failures > silent correctness illusions

Textract gives you:

* confidence thresholds
* escalation candidates
* page-level provenance

LLMs alone do not.

---

# 7. Exact Textract Recommendation

## Use:

# `StartDocumentAnalysis` (async)

NOT synchronous `AnalyzeDocument`.

Reason:

* 36-page PDFs
* better scalability
* production-safe async pipeline

---

# FeatureTypes

Use:

```python
FeatureTypes=[
    "FORMS",
    "TABLES",
    "LAYOUT",
    "SIGNATURES"
]
```

Add:

```python
"QUERIES"
```

for critical fields only.

---

# Recommended Pipeline

## Stage 1 — Textract

Extract:

* blocks
* KV pairs
* tables
* checkboxes
* geometry
* confidence

---

## Stage 2 — Normalize to IR

Create canonical structure:

```json
{
  "pages": [],
  "sections": [],
  "tables": [],
  "fields": []
}
```

Include:

* page number
* confidence
* bbox
* raw OCR text

---

## Stage 3 — Claude Mapping

Prompt Claude with:

* normalized IR
* schema
* extraction rules
* explicit “do not infer” instructions

Example:

```json
{
  "field": "mobility_level",
  "value": "Assist x1",
  "confidence": 98.2,
  "page": 12
}
```

---

# Critical Prompting Rule

Tell Claude:

> “If information is ambiguous, conflicting, or missing, return `UNRESOLVED` rather than inferring.”

This matters enormously.

---

# Architecture I Would Deploy

## Best Production Design

```text
S3
→ SQS
→ Textract StartDocumentAnalysis
→ Normalize blocks into IR
→ deterministic validators
→ Claude semantic mapper
→ conflict detector
→ human review queue (low confidence only)
→ Postgres
```

---

# Confidence Level

# High (8.5/10)

---

# What Would Flip My Recommendation?

I would reconsider toward Option A only if ALL are true:

1. PDFs are mostly digitally generated (not scanned)
2. Minimal tables/checklists
3. Small documents (<10 pages)
4. Low clinical risk
5. Human review always present
6. You can tolerate occasional silent omissions

Your workload is the opposite of that.

---

# Final Verdict

## Best engineering choice:

# Textract-first + Claude-second

Because your primary engineering constraint is not:

* cost
* token limits
* latency

It is:

# minimizing silent clinical extraction errors.

That changes the architecture completely.

[1]: https://docs.aws.amazon.com/textract/latest/dg/API_AnalyzeDocument.html?utm_source=chatgpt.com "AnalyzeDocument - Amazon Textract"
[2]: https://platform.claude.com/docs/en/about-claude/pricing?utm_source=chatgpt.com "Pricing - Claude API Docs"
[3]: https://www.reddit.com/r/Anthropic/comments/1mocu53?utm_source=chatgpt.com "Claude Sonnet 4 now supports 1M tokens of context"
