# Security and privacy note

## Where the secrets live

| Secret | Environment variable | Used by | Where it can go |
|---|---|---|---|
| Gnani API key | `GNANI_API_KEY` | The batch STT client, the Prisma streaming STT, Timbre TTS | Into the `X-API-Key-ID` header of a Gnani request, or the plugin's own auth. Nowhere else |
| LiveKit API key | `LIVEKIT_API_KEY` | The LiveKit client, the token minter | Into the LiveKit SDK. Nowhere else |
| LiveKit API secret | `LIVEKIT_API_SECRET` | The token minter only | Into the JWT signing key. **Never** into a response body, a log record, or the browser |
| Google API key | `GOOGLE_API_KEY` | The notes model, the embedding model | Into the `x-goog-api-key` header. Nowhere else |
| Groq API key | `GROQ_API_KEY` | The chat model, the notes fallback model | Into the `Authorization: Bearer` header. Nowhere else |

They live in one file, `.env`, in the project root.

- `.env` is gitignored. `.gitignore` has `.env` and `.env.*`, with an explicit
  `!.env.example` so the template is tracked.
- `.env.example` ships with dummy values only (`dummy-gnani-api-key`,
  `dummy-livekit-api-key`, …). No real credential is in it.
- The settings object is built by `pydantic-settings` from the environment.
  Every secret field is declared with `repr=False`, so a secret cannot leak
  through a `repr()` of the settings object, an exception message, or a
  debugger's variable view.
- The settings are read once and frozen (`get_settings()` is
  `lru_cache`d), so a missing key fails at startup rather than at the first
  request that needs it.

### The secrets never reach a log

`backend/app/logging_setup.py` installs a `RedactingFilter` on the log
handler. It works in three layers:

1. A structured field whose name looks sensitive (`api_key`, `api_secret`,
   `authorization`, `token`, `password`, `secret`, `credential`) has its
   **value replaced** with `***redacted***`. The field itself is kept, so the
   record still shows that a key was involved.
2. Free-text messages are scanned for inline `key=value` and `key: value`
   pairs, and the value is masked. This catches the accidental case where a
   key is interpolated into a message string.
3. `Logger.makeRecord` is overridden so an `extra` key that collides with a
   built-in `LogRecord` attribute (for example `filename`) is renamed to
   `x_filename` instead of raising `KeyError`. That bug was found in
   production: a log call could turn into a 500 inside a request handler.

**The test that proves it** is `TestConfiguredLogging::test_no_secret_reaches_the_stream`
in `backend/tests/test_logging.py`. It configures the real logging setup,
emits a record with `extra={"gnani_api_key": "sk-live-abcdef", "chunk_count": 2}`,
captures the actual stream output, and asserts that `sk-live-abcdef` does not
appear in it while `chunk_count` does. The sibling tests cover the inline
`key=value` masking, the field-name masking, and the collision rename.

Transcript text, note text and student questions are never logged either. The
logs carry counts, durations, identifiers and states — for example
`{"stage": "transcribe", "detail": "3 chunk(s)"}` and
`{"size_bytes": 8115430, "language_code": "te-IN"}` — never the content
itself.

### What the browser can see

- The LiveKit API secret never leaves the backend. The browser asks
  `POST /api/livekit/token` and receives a JWT scoped to one room
  (`lecture-<id>-<session>`) with a one-hour TTL. The secret is not in
  the response, the URL, or the front-end bundle. The JWT's `sub` and
  participant metadata carry the opaque user id from the signed session —
  never a client-supplied name — and the room metadata carries the same id
  so the agent can attribute the session.
- The session cookie is `HttpOnly`, `SameSite=lax`, and carries `Secure`
  whenever `FRONTEND_URL` is an `https://` URL (or `SESSION_COOKIE_SECURE`
  is explicitly enabled).
- No provider key is in the front-end bundle. The web app talks only to this
  project's own API.
- There is no third-party analytics or telemetry. Nothing is sent anywhere
  except to the providers listed above, and only for the purpose of the
  request being made.

## Upload validation

A recording is checked at four levels before a single byte reaches Gnani.

| Check | Where | Rule |
|---|---|---|
| Extension | `api/lectures.py`, `pipeline/ingest.py` | One of `.wav .mp3 .mp4 .m4a .aac .flac .ogg .opus .webm .amr` |
| Magic bytes | `pipeline/ingest.py` | The container is sniffed from the file header (`RIFF`, `ID3`, `0xFFFB`, `OggS`, `fLaC`, `EBML`, `ftyp`), not trusted from the extension. The sniffed type is then checked against the formats Gnani's batch API accepts |
| Size | `api/lectures.py` | 500 MB (`MAX_UPLOAD_BYTES`). The upload is written in 1 MB chunks and aborted the moment it goes over, so an oversized file never fills the disk |
| Duration | `pipeline/ingest.py` | 6 hours (`MAX_AUDIO_DURATION_SECONDS`), read with `ffprobe` |
| Decodability | `pipeline/ingest.py` | `ffprobe` must return a decodable stream with a known codec, sample rate and channel count |
| Per-file size for Gnani | `pipeline/ingest.py` | Every chunk is checked against Gnani's 10 MB per-file limit. If a chunk is over it, the audio is re-split at half the target size, up to a bounded depth |

The upload is written to a unique file in the system temporary directory
(`<tmp>/gnani-lecture-uploads/`). The name includes a timestamp and a hash of
the original filename, so two students uploading the same filename at the
same time cannot collide. The file is deleted as soon as the pipeline
finishes, whether it succeeded or failed.

After the upload, the audio is transcoded to 16 kHz mono MP3 — the input
distribution Prisma was trained on — and cut into chunks on detected
silences, so a chunk boundary lands between sentences rather than inside one.

## The credit ceiling

Gnani credits are a finite resource: the project has 5,000 of them, and the
brief asks for them to be conserved.

Three mechanisms protect them.

1. **The transcript cache.** The cache key is the SHA-256 of the uploaded
   file, combined with the language code and the STT model. A re-upload of
   the same recording finds the cached transcript and creates **no Gnani
   job at all** — zero credits. The key survives restarts, because it is
   stored in SQLite rather than in memory. The language code is part of the
   key on purpose: it decides the script the transcript comes back in, so a
   Telugu lecture transcribed as `ta-IN` is a different transcript from the
   same audio transcribed as `te-IN`.
2. **The daily ceiling.** Before a batch job starts, the pipeline sums the
   estimated credits recorded for the current UTC day and refuses the job if
   the total would pass `DAILY_CREDIT_CEILING` (default 300). The refusal
   names the numbers, so the limit is never a silent surprise. The estimate
   uses 27 credits per audio hour — a reseller's published rate card, not
   Gnani's own, because Gnani publishes none. It is a planning figure, and
   it is deliberately conservative.
3. **The credit ledger.** Every batch job writes a row to `credit_usage`
   with the estimate, the actual audio seconds and the Gnani job id, so the
   day's usage can be read without asking the dashboard.

Measured to date: one 34-minute lecture cost one batch job, an estimated 15.2
credits against the 5,000 available. Re-running it cost 0.

## Rate-limit handling

The two free tiers this project runs on are both tight, and their limits look
alike, so the code distinguishes them.

| Provider | Measured limit | Error | Handling |
|---|---|---|---|
| Google Gemini | 20 requests per day per model per project | 429, `Quota exceeded for metric: ... generate_content_free_tier_requests, limit: 20 ... Please retry in 20h38m` | Classified as a **quota** error. **Never retried** — retrying cannot help within the day. The notes workload switches to the fallback provider (Groq) instead |
| Groq | 1000 output tokens per minute for `qwen/qwen3.8-27b` | 429, `Rate limit reached ... (OTPM): Limit 1000, Used 895, Requested 795. Please try again in 41.4s` | Classified as a **rate limit**. Retried, and the provider's own wait (`41.4s`) is parsed out of the message and honoured, because a per-minute budget refills on a schedule the caller cannot guess |
| Gnani batch STT | 10 s minimum poll interval (30 s preferred for large jobs) | — | The poller honours the 10 s floor, backs off up to 30 s, and honours `Retry-After` when the provider sends it |
| Gnani streaming STT | not published as a number | — | No retry loop; the stream is re-established by the LiveKit framework |

The classification lives in `backend/app/llm/factory.py`:
`ErrorKind.QUOTA`, `ErrorKind.RATE_LIMIT`, `ErrorKind.SERVER`,
`ErrorKind.CLIENT`, `ErrorKind.SHAPE`, `ErrorKind.UNKNOWN`. Only rate
limits, server errors and shape mismatches are retried; a quota error, a
client error and an authentication error fail fast.

This classification is not a nicety. Google's daily quota error is a 429,
identical to a per-minute rate limit. A retry loop that could not tell them
apart retried the quota error three times per segment, turned 9 needed calls
into 24, and exhausted the whole day's allowance in one lecture. The
defect and its fix are recorded in `docs/PHASE1_REPORT.md` §3.2.

Model calls in the notes workload are also **paced**: a minimum of 2 seconds
between calls, serialised rather than parallel. A per-minute token budget
cannot be spent in a burst, and eight parallel long notes did exceed the
window.

Retries are bounded everywhere: the batch client rebuilds its multipart body
per attempt (a consumed body would have sent an empty file), and it only
retries on retryable conditions.

**Rate limits:** sign-in, upload and other writes are each limited per
client address by the HTTP middleware (30/10/120 per minute by default).
The sliding-window hits live in `rate_limit_hits` in the app database, so
every API process on that database shares one quota; a single
`BEGIN IMMEDIATE` transaction makes the prune/count/insert atomic. The
table holds about a minute of hits and is disposable. A per-host
concurrency cap on outbound requests is still missing (gap 3).

## Data retention

| Data | Where it lives | How long |
|---|---|---|
| The uploaded recording | `<tmp>/gnani-lecture-uploads/` | Deleted as soon as the pipeline finishes, success or failure |
| The transcoded 16 kHz copy and the chunks | `data/cache/ingest/<hash>/` | About 23 MB for a 34-minute lecture. Pruned after `CACHE_TTL_DAYS` (default 30) when no lecture or transcript references the source hash and no job is in flight |
| The transcript, the segments, the notes, the flashcards | `data/app.sqlite3` | Kept until the lecture's rows are deleted. This is the product's own state, and it is what makes the notes reader and the doubt chat work |
| The generated notes, as Markdown | `data/notes/<lecture_id>.md` | Kept alongside the database |
| The credit ledger | `data/app.sqlite3` (`credit_usage`) | Kept; it is the daily accounting |

Everything under `data/` is gitignored, so none of it can be committed by
accident.

A signed-in student can download everything tied to their account from
`GET /api/account/export`, or erase it with `DELETE /api/account`. Erasure
removes the user row and its sessions, and cascades from each owned lecture
to segments, notes, cards, review state and the FTS index. It deletes the
per-lecture Markdown files and any content-addressed audio-cache directory
no other lecture still references. The shared transcript rows and the
global credit ledger have no owner and are deliberately kept.

**Outbound concurrency:** `MAX_OUTBOUND_CONCURRENT` (default 8) bounds the
simultaneous HTTP calls to Gnani, Gemini and the OAuth endpoints in each API
process. It is applied around the request itself, so retries, polling waits
and long-lived provider sessions do not hold a slot. The agent worker is a
separate process with its own budget.

**What leaves the machine, and where:**

| Data | Sent to | When |
|---|---|---|
| The audio, in chunks under 10 MB | Gnani's batch STT (`api.vachana.ai`) | Ingestion only. A direct multipart upload — **not** the `cloud_storage` source type, which would require the file to be publicly reachable |
| The student's speech | Gnani's streaming STT (`api.vachana.ai`) | Live conversation only |
| The transcript and the retrieved segments | Google Gemini and Groq | Notes generation, retrieval indexing, and answering. The prompt forbids the model from using anything outside the supplied excerpts |
| The answer text | Gnani Timbre | Live conversation only, to be spoken |
| The student's voice | LiveKit Cloud, between the browser and the agent worker | Live conversation only |

The transcript is sent to the LLM providers because the model cannot write
notes about a lecture it has not read. It is not sent to any other service,
and it is never sent to a service that has not been named above.

The application supports Google sign-in and per-user isolation. With
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` set, data routes require a
session and every lecture, note and card is scoped to its owner. Without
credentials the API **fails closed**: data routes answer 401. The local demo
must explicitly opt into `OPEN_MODE=true`, in which case all requests share
the `anonymous` user; that flag must never be set in a deployment. Setting
credentials always wins over the flag.

## Dependencies and licences

### Python (direct)

| Package | Version | Licence | What it enables |
|---|---|---|---|
| `livekit-agents` | 1.8.8 | Apache-2.0 | The real-time agent lifecycle, the transport, the VAD and the turn detection |
| `livekit-plugins-gnani` | 1.8.8 | Apache-2.0 | Prisma streaming STT and Timbre TTS over LiveKit's interfaces |
| `livekit-plugins-google` | 1.8.8 | Apache-2.0 | Gemini, for the notes workload |
| `livekit-plugins-groq` | 1.8.8 | Apache-2.0 | Groq, for the conversational workload and the notes fallback |
| `fastapi` | 0.159.1 | MIT | The HTTP API |
| `uvicorn[standard]` | 0.54.0 | BSD-3-Clause | The ASGI server |
| `python-multipart` | 0.0.32 | Apache-2.0 | The multipart upload parsing FastAPI needs |
| `pydantic` | 2.14.1 | MIT | The settings object and the request/response schemas |
| `pydantic-settings` | 2.16.1 | MIT | Reading the settings from the environment and `.env` |
| `google-genai` | 2.32.0 | Apache-2.0 | The Gemini client, used for the embedding endpoint |
| `websockets` | 15.0.1 | BSD-3-Clause | The Gnani streaming STT and TTS transports |
| `aiohttp` | 3.14.5 | Apache-2.0 AND MIT | The HTTP client for the batch STT and the embedding calls |

### Python (development)

| Package | Version | Licence |
|---|---|---|
| `pytest` | 9.1.1 | MIT |
| `pytest-asyncio` | 1.4.0 | Apache-2.0 |
| `respx` | 0.23.1 | BSD-3-Clause |
| `ruff` | 0.17.1 | MIT |

### JavaScript (direct)

| Package | Version | Licence | What it enables |
|---|---|---|---|
| `react`, `react-dom` | 18.3.1 | MIT | The two screens |
| `livekit-client` | 2.23.0 | Apache-2.0 | The browser's WebRTC client and the room connection |
| `katex`, `rehype-katex`, `remark-math` | 0.16.47 / 7.0.1 / 6.0.0 | MIT | Rendering the equations in the notes |
| `react-markdown`, `remark-gfm`, `remark-parse`, `unified` | 9.1.0 / 4.0.1 / 11.0.0 / 11.0.5 | MIT | Rendering the merged notes document |
| `@fontsource/noto-sans-devanagari`, `@fontsource/noto-sans-gujarati` | 5.3.0 | OFL-1.1 | The Devanagari and Gujarati scripts, self-hosted — no font CDN, no third-party request |
| `vite`, `@vitejs/plugin-react`, `@tailwindcss/vite`, `tailwindcss` | 5.4.21 / 4.7.0 / 4.3.3 / 4.3.3 | MIT | The build and the styling |
| `typescript`, `@types/react`, `@types/react-dom` | 5.9.3 / 18.3.31 / 18.3.7 | Apache-2.0 / MIT / MIT | The build-time types |

### One dependency to be aware of

`livekit-agents` 1.8.8 depends on `livekit-local-inference` 0.2.7, which is
licensed **Apache-2.0 AND LicenseRef-LiveKit-Model** — the second term
covers LiveKit's model files, which are not open-licensed in the same way as
the code. This project does not use the local inference models: the STT is
Gnani's and the LLM is Gemini or Groq, both remote. The package is installed
because it is a hard dependency of `livekit-agents`, not because it is used.
If the project is ever distributed rather than run, that licence term needs
reviewing.

### Dependency audit (20 Nov 2026)

- `pip-audit`: no known vulnerabilities in installed packages (the local
  editable project is skipped because it is not published).
- `npm audit --omit=dev`: 4 low findings — `katex`, `remark-math`,
  `micromark-extension-math` and `rehype-katex`. `renderMath` only accepts a
  strict allowlist of equation characters and KaTeX runs with `trust: false`,
  so the reported prototype-pollution path is not reachable here.
- The earlier Vite 5 / esbuild high and moderate advisories (dev tooling
  only) were fixed on 20 Nov 2026 by upgrading to Vite 8.3.8 and
  `@vitejs/plugin-react` 6.3.0 after reading the v7/v8 migration notes;
  Node 20.20 meets the new engine requirement, the Tailwind plugin still
  supports Vite 8, and the build plus all 32 E2E checks pass. The full
  `npm audit` now reports the same 4 low findings as production.
- `npm audit fix --force` was deliberately not used: the upgrade was
  reviewed, pinned and verified on its own instead.
- `npm audit signatures`: all packages have verified registry signatures.

## Deployment requirements

The code-side gaps in earlier versions of this note are closed: an
unconfigured deployment fails closed, lectures are owner-scoped with a
per-user quota and an operator role, outbound calls are bounded, the ingest
cache has a TTL, and account export/erasure exists. What remains is for the
operator to configure and run the service responsibly:

- Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`; leave `OPEN_MODE` unset.
- Serve HTTPS with `FRONTEND_URL` set to the `https://` origin.
- Set `ADMIN_EMAILS` to the operator addresses and review the per-user cap.
- Remove the CC-licensed demo lecture and accept only recordings made with
  the required consent. `docs/DATA_POLICY.md` lists the classification,
  processors, retention and backup obligations.
- Keep backups on the same retention window as the cache, so an erased
  student's data does not survive in an old backup.
- Run the release gates and first-hour checks in `docs/SHIPPING.md`.
