I'm building a healthcare document automation backend and need a second opinion
on a specific architectural choice. Please research and give me a concrete
recommendation with citations to AWS docs where possible.

CONTEXT (everything you need to know):

- Stack: FastAPI (Python 3.11) + LangGraph + PostgreSQL + AWS (S3, SQS, Bedrock).
- Use case: caregivers at assisted-living facilities upload a 36-page
  state-issued patient assessment PDF. The system must extract ~hundreds of
  structured fields (patient demographics, mobility level, bathing level,
  medications list, vitals, checkboxes for ADLs, etc.) into a JSON schema,
  store it in Postgres, and let a Flutter app render a filled PDF locally.
- Source PDFs are usually SCANNED (image-based, sometimes handwritten
  checkboxes and notes) — not digital text PDFs.
- Current pipeline: PDF lands in S3 -> SQS -> worker -> LangGraph node
  `parse_pdf` calls Amazon Bedrock Claude Sonnet 4.5 via the Converse API,
  passing the PDF as a `document` content block. Output is JSON filtered
  against a schema skeleton.
- Problem: Bedrock's Converse API caps a `document` content block at ~4.5 MB.
  Scanned 36-page PDFs routinely exceed this. Even when under the cap, Claude
  rasterizes each page to an image (~1.5–2K input tokens per page), so a 36-page
  doc consumes ~70K input tokens before the schema prompt — pushing latency,
  cost, and output-truncation risk.
- This is HIPAA-adjacent clinical data. Silent data loss or silent value
  conflicts (e.g. a medication dropped, a vitals value overwritten) are
  unacceptable. Loud failures are preferable to quiet wrong answers.

THE TWO OPTIONS I'M WEIGHING:

OPTION A — PDF chunking + merge:
   Split the 36-page PDF into 5–6-page sub-PDFs, call Bedrock Claude once per
   chunk, merge the JSON outputs in code. No new AWS service.

OPTION B — Textract-first, Claude-second:
   Run AWS Textract (AnalyzeDocument with FORMS + TABLES) once on the full
   PDF, get back text + key/value pairs + table structures, then send that
   structured output to Claude in a single call to map to my schema.

QUESTIONS I WANT ANSWERED:

1. For SCANNED state-form PDFs with handwritten checkboxes and tables, which
   option gives higher field-level accuracy? Cite AWS Textract accuracy
   benchmarks vs Claude vision OCR if you can find them.

2. What are the real failure modes of Option A on multi-page sections
   (e.g. a medications table spanning pages 14–17 with a chunk boundary at
   page 15)? How would correct merge logic handle:
     - scalar conflicts (same field with different values across chunks)
     - list fields (medications, allergies) split across chunks
     - section headers that appear once but apply to many pages
   Is there a known design pattern for "schema-aware merge with conflict
   resolution" or do I have to roll my own?

3. Cost comparison for a single 36-page scanned intake:
     - Option A: ~6 Bedrock Converse calls with ~12K input tokens each
       (Claude Sonnet 4.5 pricing)
     - Option B: 1 Textract AnalyzeDocument call (per-page pricing) +
       1 Bedrock Converse call with ~10–15K input tokens of extracted text
   Give me approximate $ per intake for each.

4. Latency comparison — 6 sequential Bedrock calls vs 1 Textract job + 1
   Bedrock call. Can the 6 Bedrock calls be parallelized within typical
   account-level Bedrock concurrency limits in us-east-1?

5. Are there other options I'm missing? Specifically:
     - Amazon Textract Queries (ask Textract for specific fields directly)
     - Amazon Comprehend Medical for medication/diagnosis extraction
     - Bedrock Data Automation (BDA) for document workflows
     - A multimodal model with a larger document window
   Pros/cons of each vs Options A and B for this specific use case.

6. From a HIPAA / clinical-data-integrity standpoint, which option has the
   smaller "silent wrong answer" risk surface? This is the question I care
   about most.

7. If you recommend Option B, give me the specific Textract API call
   (AnalyzeDocument vs AnalyzeExpense vs StartDocumentAnalysis async),
   which FeatureTypes to enable, and how to structure the resulting
   blocks/key-value pairs into a prompt for Claude.

Please give me a final ranked recommendation, your confidence level, and
the specific assumption(s) that would flip the recommendation the other way.
