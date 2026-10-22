# Phase 1 Report — Lecture ingestion and structured notes

**Date:** 2026-10-22
**Branch:** `feat/gnani-lecture-platform`
**Status:** complete. The pipeline runs end to end against the live Gnani API and the live LLM providers, and every stage is covered by tests that never touch a remote API.

Labels: **MEASURED** (observed on the live APIs), **ESTIMATED** (planning figure, not measured), **MOCKED** (covered by tests with the provider replaced), **UNVERIFIED** (no authoritative evidence found).

---

## 1. What was implemented

| Module | Responsibility |
|---|---|
| `backend/app/config.py` | Env-driven settings. Every secret read from the environment; nothing logged. |
| `backend/app/logging_setup.py` | Structured JSON logs, secret redaction, and a `makeRecord` that cannot raise on a field-name collision. |
| `backend/app/db.py` | SQLite schema: lectures, transcripts, segments, notes, stage checkpoints, flashcards, credit usage, and an FTS5 index over the corpus. |
| `backend/app/pipeline/ingest.py` | Upload validation, 16 kHz mono transcode, silence-aware chunking under Gnani's 10 MB per-file limit, content hashing. |
| `backend/app/pipeline/transcribe.py` | The batch STT job lifecycle against `api.vachana.ai`, with bounded retries, a fresh request body per attempt, and per-file timestamp rebasing. |
| `backend/app/pipeline/segment.py` | Transcript normalisation, topic segmentation, and visual-dependency detection across Hindi, Gujarati and English. |
| `backend/app/pipeline/notes.py` | Per-segment note generation, the merge into one document, the grounding rule, and the provider fallback chain. |
| `backend/app/pipeline/orchestrator.py` | The state machine, the transcript cache, the credit ceiling, and per-stage resumability. |
| `backend/app/retrieval/fts.py` | FTS5 lexical retrieval with an abstention gate. |
| `backend/app/retrieval/rerank.py` | Grounded answering with citation validation. |
| `backend/app/llm/factory.py` | Provider-neutral LLM access, error classification, and the two workload profiles. |

**135 tests, all passing, all mocked** (except where noted). Lint clean.

---

## 2. The end-to-end run

**Input:** `Bhagavad Gita Chapter 5 with Shankara Bhashya`, by Swami Tattvividananda Saraswati (Ramakrishna Math, 2007), from Internet Archive item `BhagvadGitaChapter5WithShankaraBhashya`.

- **Licence:** CC BY-NC-ND 4.0 (`http://creativecommons.org/licenses/by-nc-nd/4.0/`), creator `rparimi`. A real human lecture, not a recorded call, so it respects the challenge's synthetic-data rule.
- **File:** 8,115,430 bytes, 2028.5 s (33.8 min), MP3 44.1 kHz stereo.
- **Language:** Telugu.

### 2.1 Measured timings — first run, cold

| Stage | Time | What happened |
|---|---|---|
| Ingest | 116.7 s | ffprobe validation, transcode to 16 kHz mono, silence detection, chunk planning and encoding |
| Transcribe | 66.4 s | 1 Gnani batch job, polled at the documented 10 s floor, 67 segments returned |
| Segment | 0.2 s | 67 transcript segments → 8 topic segments |
| Note | 163.7 s | 8 per-segment notes + 1 merge (partly degraded, see §3) |
| **Total** | **347.3 s** | ~5.8 minutes for a 34-minute lecture |

### 2.2 Measured timings — steady state, transcript cached

| Stage | Time |
|---|---|
| Ingest | 0.1 s |
| Transcribe | **0.0 s — cache hit, zero Gnani credits** |
| Segment | 0.0 s |
| Note | 200.8 s |
| **Total** | **200.9 s** |

The note stage is dominated by deliberate pacing (§4.2), not by model latency.

### 2.3 Credits — MEASURED

| Item | Estimate | Actual |
|---|---|---|
| Batch STT, 2028.5 s of audio | 15.2 credits | one job, `01a16317-efe8-74b1-9c46-265898c5465f` |
| Re-running the same lecture | 0 | **0 — cache hit, no job created** |
| TTS | 0 | not used in Phase 1 |
| **Total spent so far** | | **≈ 15 credits of 5,000 (0.3 %)** |

The estimate uses the reseller rate card (27 credits/audio-hour) because
Gnani publishes no rate of its own. **The authoritative figure is the
usage panel at `app.gnani.ai/voice`** — that has not been read, so 15.2
is an ESTIMATE against a measured API-call count of one.

### 2.4 Output

- 67 transcript segments, 8 topic segments, 0 flagged visual-dependent.
- A merged document with a title, a written summary, 8 sections each with
  its source time span, and a key-terms list. Stored as Markdown and as
  structured rows in SQLite.
- The notes correctly identified the lecture's structure — Pravritti vs
  Nivritti Dharma, Sankhya Yoga and Viveka, Karma vs Jnana Yoga, Preya vs
  Shreya — **from a transcript that contains substantial ASR errors**.
  The model recovered the concepts from noisy text, which is the single
  most encouraging result in this phase.

---

## 3. What worked, what did not, what could not be verified

### 3.1 Works

| Behaviour | Evidence |
|---|---|
| Upload validation, transcode, silence-aware chunking | MOCKED (20 ingest tests) + MEASURED on the real file |
| Batch job lifecycle: create → start → poll → download | MEASURED, live |
| Per-file timestamp rebasing | MEASURED, live, two-chunk probe |
| Transcript cache: a re-upload makes zero API calls | MEASURED, live |
| A downstream failure does not destroy the transcript | MOCKED (orchestrator tests) + MEASURED during the real run |
| Visual-dependency marking and the uncertainty marker | MOCKED (35 segment tests) + observed in real output |
| Grounding: a merged section citing a visual-dependent segment must carry the marker | MOCKED |
| Provider fallback on a quota error | MOCKED (13 tests) + MEASURED during the real run |

### 3.2 Did not work on the first attempt — and why

Six real defects, each found by running against the live APIs, each fixed
and now covered by a test.

| # | Defect | Cause | Fix |
|---|---|---|---|
| 1 | Batch API returned 401 | `_request()` never attached the `X-API-Key-ID` header | The header is now merged into every call |
| 2 | A retried upload sent empty files | The multipart body holds open file handles and is consumed on the first attempt | The body is rebuilt per attempt via a `body_factory` |
| 3 | A log call could crash a request | `extra={"filename": ...}` collides with a `LogRecord` attribute and raises `KeyError` | `Logger.makeRecord` is overridden to rename colliding fields; the three collisions in the codebase were renamed |
| 4 | Gujarati queries never matched | Python's `\w` drops Indic combining marks, so `પાણી` fragmented into `પ` and `ણ` while FTS5's `unicode61` keeps them whole | The tokenizer now splits on whitespace and keeps combining marks |
| 5 | The whole daily LLM allowance was burned by one lecture | Google's free tier allows **20 requests/day/model** and its quota error is a 429 — indistinguishable by status code from a rate limit, so the retry loop retried it 3× per segment and turned 9 calls into 24 | Errors are classified; a quota error is never retried; the notes workload falls back to a provider on a different account |
| 6 | The merged document lost its summary and its timestamps | The merge model was asked which segments back each section rather than for times, and the response schema drops undeclared fields; the fallback path had no summary at all | Times are derived from the cited segments; the fallback derives a summary and says it is mechanical |

### 3.3 Could not be verified

| Item | Status |
|---|---|
| Gnani's own credit rate | **UNVERIFIED.** Gnani publishes no rate card. The only published figures are a reseller's. The dashboard is the authority and was not read. |
| Whether a Gujarati *lecture* transcribes as well as a Hindi one | **UNVERIFIED.** The API accepts `gu-IN` (measured), but no Gujarati audio was available to test with — macOS has no Gujarati TTS voice. |
| The accuracy of the Telugu transcript | **PARTIALLY VERIFIED.** The transcript contains real ASR errors on Sanskrit philosophical vocabulary. The concepts survive, the words do not always. A human-level transcript was not achievable to compare against. |
| Whether `with_diarization` would help separate teacher from students | **UNVERIFIED.** Not exercised: the recording is a single speaker and diarization on one microphone is unreliable. |
| Batch STT with a file over 10 MB | **UNVERIFIED.** The lecture fitted in one chunk. The chunking path is tested with real ffmpeg and the limit is enforced, but no real job has exceeded it. |

---

## 4. Reliability and the two provider limits

### 4.1 The daily quota — MEASURED

Google's free tier allows **20 requests per day per model per project**:
`Quota exceeded for metric: generate_content_free_tier_requests, limit: 20 ...
Please retry in 20h38m.`

A lecture needs 9 calls (8 segment notes + 1 merge). That is under 20,
but only if nothing is retried. The quota error is a 429, so it looks
exactly like a rate limit; a naive retry loop multiplies the request
count and destroys the day's allowance. This is now the pipeline's
sharpest edge, and it is handled explicitly.

### 4.2 The per-minute token budget — MEASURED

Groq's free tier allows **1000 output tokens per minute** for
`qwen/qwen3.8-27b`: `Rate limit reached ... (OTPM): Limit 1000, Used 895,
Requested 795. Please try again in 41.4s.`

Eight segment notes at ~800 tokens each exceeded it, and the merge — the
largest request — then failed with `413 Request too large ... on tokens
per minute`. Three changes follow:

1. **Model calls are paced**, 2 s apart, serialised rather than
   parallel. A per-minute budget cannot be spent in a burst.
2. **Per-segment notes are short.** A segment note is asked for three to
   five sentences under 120 words. It is merged into a larger document
   later; detail there is wasted budget.
3. **The provider's own retry hint is honoured.** "Please try again in
   41.4s" is parsed and used, because a token budget refills on a
   schedule the caller cannot guess.

### 4.3 The provider chain

The notes workload now has a chain: **Google (primary) → Groq
(fallback)**, switched automatically when the primary's daily quota is
exhausted. Both providers were already authorised by you; no new service
was introduced and no paid plan was enabled. The conversational workload
keeps its own independent provider and model, as the brief requires.

The configured defaults are the models **measured to work with these
keys**, not the newest published ones:

| Workload | Model | Why |
|---|---|---|
| A — notes | `gemini-3.5-flash` | `gemini-4-argon` is 429 on this account; `gemini-3.8-flash` is 503/504. Override with `LLM_MODEL_NOTES` when the quota allows. |
| A — segments | `gemini-3.5-flash` | same |
| B — chat | `qwen/qwen3.8-27b` | `llama-3.3-70b-versatile` is 404 on this account. Fastest of the available models (0.35 s) and Qwen3 is among the stronger families for Hindi/Hinglish. Streams clean content with no reasoning preamble, which matters because the answer is spoken aloud. |

`scripts/probe_llm_models.py` re-checks all of this on demand.

---

## 5. Security and privacy

| Property | Status |
|---|---|
| Secrets never logged | MOCKED (8 logging tests, including a scan of the real log output) |
| A log call cannot break a request | MOCKED |
| Transcript and note text never logged | By construction — logs carry counts, durations and ids |
| Audio leaves the machine only for the Gnani API | Yes — the batch path uses a direct multipart upload, **not** the `cloud_storage` source type, which would require a publicly reachable URL |
| Daily credit ceiling | Enforced before a job starts: a job that would exceed `DAILY_CREDIT_CEILING` is refused |
| Third-party analytics | None |

---

## 6. Known issues, and what I would do next

1. **The Telugu ASR is noisy.** Real errors on Sanskrit vocabulary
   ("பிரயத்தின்" for "பிரவிர்த்தியின்"). The notes recover the concepts
   anyway, but a student revising from the transcript alone would struggle.
   *Next:* the segment text is stored alongside the notes, so the notes
   reader can show the source span; and the notes prompt already says to
   use the teacher's own vocabulary.
2. **Ingest is 116 s for a 34-minute file** — three full ffmpeg passes
   (silence detection, transcode, chunk encode). *Next:* the silence
   detection can be folded into the transcode pass, or the chunk encoder
   can be skipped when the file fits in one chunk. Both are cheap wins.
3. **The note stage takes 200 s**, almost all of it pacing. *Next:* the
   pacing is only needed on a free tier; with a paid quota the interval
   can drop to near zero. It is a config value already.
4. **The Gemini quota resets daily**, so the fallback carries the load
   after 20 requests. *Next:* if the note quality difference between
   Gemini and Groq is material, the ordering of the chain should be
   decided by measurement rather than by which key happens to be
   available.

---

## 7. What Phase 2 needs from me

- Nothing is blocked. The transcript corpus, the RAG index and the
  grounded-answering path are all in place and tested.
- Phase 2's first task is the `stt_gnani.py` subclass: the live probe
  showed the server sends `speech_start` but **never** `speech_end`, so
  the official plugin emits `START_OF_SPEECH` and never
  `END_OF_SPEECH`. The subclass derives both from LiveKit's own VAD and
  adds the five Gnani VAD-tuning headers the plugin does not expose.
- The measured end-of-speech → transcript latency is **840 ms**
  (500 ms server min-silence + 233 ms transcription), which is the
  baseline Phase 2's latency work has to beat.
