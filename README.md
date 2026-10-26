# Gnani Lecture-to-Learning Platform

Turn a classroom recording into structured study notes and a voice tutor that
answers only from that lecture.

- **Ingestion**: upload a recording, transcribe it with Gnani Prisma's batch
  STT, split it into timestamped topic segments, and generate a merged notes
  document with a summary, per-section explanations and key terms.
- **Doubt chat**: ask a question in Hindi, Hinglish, Gujarati or English and
  get an answer that cites the source segments it used — or a plain "this
  isn't covered in the lecture" when the lecture does not cover it.
- **Voice**: the live tutor runs on LiveKit Agents, with Gnani Prisma for
  streaming speech-to-text and Gnani Timbre for speech output.

Every claim about the external APIs in this README is backed by a measurement
recorded in [`docs/live-api-findings.md`](docs/live-api-findings.md). Where
something is not measured, the documentation says so.

## Repository layout

```
backend/app/          FastAPI application
  api/                HTTP routes: lectures, chat, flashcards, livekit, health
  pipeline/           ingest -> transcribe -> segment -> note -> merge
  retrieval/          FTS5 + embeddings (hybrid search), grounded reranking
  llm/                provider-neutral model access, error classification
backend/tests/        264 tests, all offline
agent/                the LiveKit agent worker and the STT subclass
  bench/              the latency benchmark harness
web/                  Vite + React + TypeScript front end
scripts/              the pipeline runner and the API probe scripts
data/                 SQLite database, cached transcripts, notes (gitignored)
docs/                 architecture, benchmark, security, demo script
```

## Requirements

| Requirement | Version | Why |
|---|---|---|
| Python | 3.12.x | `livekit-agents` pulls in `livekit-local-inference` and its bundled ONNX models, which publish cp310–cp314 wheels |
| ffmpeg + ffprobe | any recent build | transcoding, silence detection and duration probing during ingest |
| Node.js | 20 or newer | the web front end only |
| uv | any recent build | the virtual environment (a plain `venv` + `pip` also works) |

## Setup

### 1. Get the code and create the environment

```bash
cd "/Users/yuvraj/Desktop/Lecture-to-Learning Platform"
uv venv --python 3.12 .venv
```

### 2. Install the Python dependencies

**Use the PyPI mirror.** `files.pythonhosted.org` is throttled from this
network: an 11.8 MB wheel times out at 0 bytes with both `uv` and `curl`,
while the same wheel arrives in about 10–12 seconds from the Aliyun or
Tsinghua mirrors. The mirrors serve byte-identical artifacts (the sha256 of
`grpcio-1.84.0-cp312-cp312-macosx_11_0_universal2.whl` was compared and
matches), and `uv`/`pip` verify every download against the hash in the index
metadata, so a tampered file fails the install rather than landing in the
environment.

```bash
UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple/" \
  uv pip install --python .venv/bin/python -e ".[dev]"
```

This is an environment workaround, not a project dependency. The project
declares no mirror and installs against stock PyPI anywhere the network is
not throttled. Drop the `UV_DEFAULT_INDEX` prefix to use first-party PyPI,
and expect a slow install with possible timeouts.

### 3. Install the front-end dependencies

```bash
cd web && npm install && cd ..
```

### 4. Create the environment file

```bash
cp .env.example .env
```

Then fill in the five real credentials. `.env` is gitignored; `.env.example`
ships with dummy values only.

### 5. Initialise the database

The schema is created automatically on first use — the API does it during
startup — but you can do it explicitly:

```bash
PYTHONPATH=backend .venv/bin/python -c "from app.db import init_db; init_db(); print('database ready')"
```

This creates `data/app.sqlite3` with the full schema (lectures, transcripts,
segments, topic groupings, notes, stage checkpoints, flashcards and review
state, credit usage, and the FTS5 index over the corpus). It is idempotent:
running it again is a no-op. The schema is versioned (`schema_version`), and
a version bump rebuilds only the *derived* tables — the transcript, which
cost a Gnani job and real credits, is never dropped.

### 6. Check the installation

```bash
PYTHONPATH=backend .venv/bin/python -c "import app, agent; print('imports ok')"
.venv/bin/python -m pytest -q
```

If the first command fails with `ModuleNotFoundError: No module named 'app'`,
the editable install's `.pth` file was skipped by Python — see
[Troubleshooting](#troubleshooting).

## Environment variables

| Variable | Required | Default | What it is for |
|---|---|---|---|
| `GNANI_API_KEY` | for ingestion and voice | — | Gnani Prisma batch STT, Prisma streaming STT and Timbre TTS |
| `GNANI_BASE_URL` | no | `https://api.vachana.ai` | The Gnani API host. Note: `api.vachana.ai`, not `api.gnani.ai` |
| `GNANI_STT_MODEL` | no | `gnani-prisma-v2.5` | The Prisma model used for batch transcription |
| `LIVEKIT_URL` | for voice | — | LiveKit Cloud project URL, e.g. `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | for voice | — | LiveKit API key |
| `LIVEKIT_API_SECRET` | for voice | — | LiveKit API secret. Used server-side only, to mint room tokens |
| `GOOGLE_API_KEY` | for notes | — | Gemini, workload A (notes generation) and the embedding model |
| `GROQ_API_KEY` | for chat and for the notes fallback | — | Groq, workload B (conversational doubts) and the notes fallback provider |
| `LLM_MODEL_NOTES` | no | `gemini-3.5-flash` | Workload A model. The default is the one measured to work with this key, not the newest published one |
| `LLM_MODEL_NOTES_SEGMENTS` | no | `gemini-3.5-flash` | Model for the per-segment note calls, when it should differ from the merge call |
| `LLM_MODEL_CHAT` | no | `qwen/qwen3.8-27b` | Workload B model. Chosen for time-to-first-token and for streaming without a reasoning preamble |
| `DATA_DIR` | no | `data/` | Root for the database, the cache and the generated notes |
| `DB_PATH` | no | `data/app.sqlite3` | The SQLite file |
| `CACHE_DIR` | no | `data/cache` | Where the transcoded and chunked audio is kept |
| `DAILY_CREDIT_CEILING` | no | `300` | Refuse to start a Gnani job that would take the day's estimated credit use past this |
| `ESTIMATED_CREDITS_PER_AUDIO_HOUR` | no | `27` | The planning figure used for that estimate. Gnani publishes no rate card of its own |
| `MAX_UPLOAD_BYTES` | no | `524288000` (500 MB) | Hard cap on an uploaded recording |
| `MAX_AUDIO_DURATION_SECONDS` | no | `21600` (6 h) | Hard cap on a recording's duration |
| `GNANI_MAX_FILE_BYTES` | no | `10485760` (10 MB) | Gnani's per-file limit for a batch job. The chunker stays under it |
| `GNANI_CHUNK_TARGET_BYTES` | no | `8388608` (8 MB) | The size the chunker aims for, with margin under the 10 MB limit |
| `BATCH_POLL_INTERVAL_SECONDS` | no | `10` | The documented minimum poll interval for the batch STT job |
| `BATCH_POLL_MAX_INTERVAL_SECONDS` | no | `30` | The upper bound when backing off |
| `BATCH_JOB_TIMEOUT_SECONDS` | no | `3600` | Total budget for one batch job |
| `HTTP_CONNECT_TIMEOUT` | no | `15` | Connect timeout for every outbound HTTP call |
| `HTTP_READ_TIMEOUT` | no | `120` | Read timeout for every outbound HTTP call |
| `GNANI_POLL_TIMEOUT` | no | `60` | Timeout for a single batch poll request |

Every setting is read through `pydantic-settings`, so the environment
variable name is the field name in upper case. The two LLM workloads have
independent model settings, as the brief requires: the notes workload favours
long context and structured output, the conversational workload favours
time-to-first-token and streaming.

## Running it

### The API

```bash
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive API documentation is at `http://127.0.0.1:8000/docs`.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/health` | Liveness. Touches no external service on purpose |
| `GET` | `/api/ready` | Readiness, with each dependency's state reported as data rather than as a failure |
| `GET` | `/api/lectures` | The lecture list |
| `POST` | `/api/lectures/upload` | Accept a recording and start the pipeline in the background. Fields: `file`, `title`, `language_code` |
| `GET` | `/api/lectures/{id}` | Status, per-stage checkpoints, segment counts, recent credit usage |
| `GET` | `/api/lectures/{id}/notes` | The merged notes document plus the source segments |
| `POST` | `/api/chat` | A grounded answer. Body: `{"question": "...", "lecture_id": "..."}` |
| `GET` | `/api/chat/search?q=...` | Retrieval only, without calling the model |
| `GET` | `/api/flashcards` | The flashcards, optionally `?due_only=true` |
| `POST` | `/api/flashcards/lecture/{id}` | Generate flashcards from a lecture's notes |
| `POST` | `/api/flashcards/{id}/review` | Record a review and schedule the next one. Body: `{"quality": 0-5}` |
| `POST` | `/api/livekit/token` | Mint a room token for a lecture. Body: `{"lecture_id": "...", "participant_name": "..."}` |

### The front end

```bash
cd web && npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api` to
`http://127.0.0.1:8000`, so the browser talks to one origin in development
and in production alike.

For a single-process deployment, build the front end and let the API serve
it from the same origin:

```bash
cd web && npm run build && cd ..
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The API mounts `web/dist` when it exists, so the whole application is on
`http://127.0.0.1:8000` afterwards.

### The agent worker

The agent is a **separate process** from the API. It holds a long-lived
LiveKit room session, which does not belong in a request handler.

```bash
.venv/bin/python -m agent.worker
```

It needs `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` from
`.env`, plus `GNANI_API_KEY` for Prisma streaming STT and Timbre TTS, and
`GROQ_API_KEY` for the conversational model. The `agent` package puts
`backend/` on the import path itself, so this command needs no `PYTHONPATH`.

Each voice session joins its own room (`lecture-<id>-<session>`), named
after the lecture so the agent knows what to ground its answers in. The
token carries the room configuration that dispatches the tutor agent
when the room is created, so the agent joins with the student every
time. The browser gets its room token from `POST /api/livekit/token`,
which is why the LiveKit API secret never reaches the client.

### The notes pipeline from the command line

```bash
PYTHONPATH=backend .venv/bin/python scripts/run_pipeline.py \
  data/fixtures/telugu_lecture_ch5.mp3 --lang ta-IN \
  --title "Bhagavad Gita Chapter 5 with Shankara Bhashya"
```

It prints the per-stage timings, the credit estimate, the segment counts and
the first 4,000 characters of the notes, and writes the document to
`data/notes/<lecture_id>.md`. Re-running it on the same file is a **cache
hit**: the transcript is looked up by content hash, no Gnani job is created
and no credits are spent.

`data/fixtures/telugu_lecture_ch5.mp3` is the demo lecture: *Bhagavad Gita
Chapter 5 with Shankara Bhashya* by Swami Tattvividananda Saraswati
(Ramakrishna Math, 2007), from the Internet Archive item
`BhagvadGitaChapter5WithShankaraBhashya`, CC BY-NC-ND 4.0, creator
`rparimi`. The whole of `data/` is gitignored, so the file is a local
artefact — download it again from the Internet Archive if it is missing.
Any recording in a supported format works in its place.

### The API probes

These are the scripts that produced `docs/live-api-findings.md`. They call
the live APIs and consume a small number of Gnani credits, so run them
deliberately.

```bash
PYTHONPATH=backend .venv/bin/python scripts/probe_stt.py         # streaming STT
PYTHONPATH=backend .venv/bin/python scripts/probe_batch.py       # batch STT
PYTHONPATH=backend .venv/bin/python scripts/probe_llm_models.py  # LLM reachability
PYTHONPATH=backend .venv/bin/python scripts/probe_tuned_stt.py   # turn-taking sweep
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

264 tests, all passing, in about a minute. **No test calls a remote API.**
The Gnani and LLM clients are replaced with fakes, every test gets its own
temporary SQLite file and cache directory, and the retry delays are shortened.
A test that does exercise the live API is marked `@pytest.mark.live` and is
skipped unless `GNANI_LIVE_TESTS=1` is set, so it is always obvious which
kind of test ran.

```bash
# everything except the live-API tests (the default)
.venv/bin/python -m pytest -q

# the live tests as well -- spends Gnani credits
GNANI_LIVE_TESTS=1 .venv/bin/python -m pytest -q -m live
```

Lint:

```bash
.venv/bin/ruff check .
```

## The benchmark

The end-to-end voice benchmark drives scripted question turns through a real
LiveKit room and records per-stage timestamps for every turn:

```bash
.venv/bin/python -m agent.bench.harness --help
```

The per-stage measurements that do not need a room — the streaming STT
latency, the turn-taking sweep, the TTS time-to-first-byte, the reachability
of each model — are in the probe scripts, which call the live APIs:

```bash
PYTHONPATH=backend .venv/bin/python scripts/probe_stt.py         # streaming STT
PYTHONPATH=backend .venv/bin/python scripts/probe_tuned_stt.py   # turn-taking sweep
PYTHONPATH=backend .venv/bin/python scripts/probe_batch.py       # batch STT
PYTHONPATH=backend .venv/bin/python scripts/probe_llm_models.py  # LLM reachability
```

The methodology, the measured results, and the two deployment configurations
that could not be run are in
[`docs/BENCHMARK.md`](docs/BENCHMARK.md).

## Documentation

| Document | What is in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The component diagram and the data flow through every stage |
| [`docs/BENCHMARK.md`](docs/BENCHMARK.md) | Methodology, measured latency, and the deployment comparison |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Secrets, upload validation, the credit ceiling, rate limits, retention, dependencies |
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | The 60-second demo, with exact commands |
| [`docs/live-api-findings.md`](docs/live-api-findings.md) | Every measured fact about the live Gnani, Google and Groq APIs |
| [`docs/PHASE1_REPORT.md`](docs/PHASE1_REPORT.md) | The Phase 1 report: what was built, what was measured, what did not work |
| [`RECONNAISSANCE.md`](RECONNAISSANCE.md) | The Phase 0 report and its assumptions table |
| [`ENVIRONMENT.md`](ENVIRONMENT.md) | The PyPI mirror workaround and the LiveKit deployment constraint |

## Troubleshooting

**`ModuleNotFoundError: No module named 'app'` after installing.**

The editable install writes a `.pth` file into the virtual environment, and
Python's `site` module skips `.pth` files whose directory carries the macOS
`hidden` flag. Check with `ls -lO .venv`; if it shows `hidden`, either run
every command with `PYTHONPATH=backend` (as the commands above do) or clear
the flag:

```bash
chflags -R nohidden .venv
```

**`ffprobe is not available on this system`.**

Install ffmpeg (`brew install ffmpeg`) and make sure `ffprobe` is on the
path. The ingest stage needs it for the duration, the sample rate and the
codec, and `ffmpeg` for the transcode, the silence detection and the chunk
encoding.

**The upload returns 503 with "GNANI_API_KEY is not configured".**

The `.env` file is missing the key, or the API process was started from a
different directory. The settings object reads `PROJECT_ROOT/.env`, where
`PROJECT_ROOT` is derived from the file location, so the working directory
does not matter — but the file must exist.

**The upload is accepted but the lecture stays in `uploaded` or `failed`.**

Read the stage detail:

```bash
curl -s http://127.0.0.1:8000/api/lectures/<id> | python3 -m json.tool
```

`failed_stage` names the stage that failed. A `transcribe` failure is
usually a credit ceiling (the message says so) or a Gnani outage; a `note`
failure leaves the transcript intact and can be retried without transcribing
again.

**"This transcription would exceed the daily credit ceiling".**

The ceiling is `DAILY_CREDIT_CEILING` (default 300 estimated credits per
day). Raise it deliberately, or wait for the next UTC day.

**The chat answers something the lecture never said.**

It should not: the answer is generated only from retrieved segments, and a
citation that names an excerpt which was not in the prompt is rejected. If
you see a wrong answer, the likely cause is that the retrieved segment was
relevant but incomplete. `GET /api/chat/search?q=...` shows exactly what
retrieval returned, without involving the model.

**The voice path does not connect.**

`GET /api/ready` reports the LiveKit configuration state. The agent worker
must be running in a separate terminal, and it needs the same `.env` as the
API.
