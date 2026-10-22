# Phase 0 — Reconnaissance Report & Implementation Plan

**Project:** Gnani AI Lecture-to-Learning Platform
**Date:** 2026-10-22
**Deadline:** Gnani Great Indian AI Internship Challenge — 2026-11-10 23:59 IST (fixed, no extensions; 19 days)
**Status:** Phase 0 complete. No code written, no dependencies installed, no cloud resources created, no commits made.

Labels used throughout: **VERIFIED** (confirmed against version-matched source/docs or a passing check I ran), **PARTIALLY VERIFIED** (evidence exists, key behaviour untested), **UNVERIFIED** (no authoritative source found).

### Decisions taken with the project owner (2026-10-22)

| Question | Decision | Consequence |
|---|---|---|
| LiveKit deployment | **LiveKit Cloud only.** The account, URL and API keys are already available. No local LiveKit server, no container runtime. | The three-way deployment comparison collapses to **one real, measured configuration** (A: agent worker on this machine + LiveKit Cloud transport). B and C are reported as documented estimates with the reason — which §7 of the brief explicitly permits when a setup cannot be run. |
| Self-hosted LiveKit (C) | **Not needed.** Dropped. | No Go build, no `livekit-server` binary, no local Redis/LocalStore work. |
| Agent worker on LiveKit Cloud (B) | **Deferred.** It would need a local image build, and no container runtime is present. | Documented as an estimate with exact `lk agent deploy` steps, not claimed as measured. Revisit only if a container runtime is approved later. |
| Demo lecture | **Telugu open lecture** (CC BY-NC-ND 4.0, real human lecture, ~34 min, 7.7 MB). | Fits the 10 MB batch limit in one file; no `gu-IN` batch ambiguity; targets the challenge's *"Main Character"* regional-language award. The tutor itself remains Hindi/Hinglish/Gujarati. |
| LLM providers | **Google Gemini** (Workload A, notes) and **Groq** (Workload B, voice chat). | Two independent provider/model profiles, as the brief requires. |

---

## 1. Existing state

**The project directory is empty.** `/Users/yuvraj/Desktop/Lecture-to-Learning Platform` contains only:

| Path | Notes |
|---|---|
| `read.md` | The engineering brief (read in full) |
| `.omo/run-continuation/*.json` | Two stale session-continuation stubs, 0 bytes of project state |

- **Not a Git repository** — `git status` → `fatal: not a git repository`.
- No application code, no backend, no frontend, no tests, no dependencies, no `.env`, no config files, no CI.
- No pre-existing user changes to preserve. There is nothing to preserve — the working tree is effectively clean slate.

**Conclusion:** every layer of this product is to be built from scratch. There is no "existing implementation" to preserve or extend, and therefore no regression risk from prior work. The brief's instruction to "work on a new Git branch" applies as: initialise a repo in this directory and branch from the first commit.

---

## 2. Environment

| Item | Value | Evidence |
|---|---|---|
| OS | macOS 26.6.2 (Darwin 25.6.0), Apple Silicon `aarch64` | `sw_vers`, `uname` |
| CPU / RAM | 8 cores / 8 GB | `sysctl hw.ncpu hw.memsize` |
| Disk free | 58 GB | `df -h` |
| Python | 3.13.0 (system), 3.11.8, 3.12.13, 3.14.8 all present | `uv python list` |
| Package manager | **uv 0.11.7** + Homebrew 7.0.9 (no poetry/pipx) | `uv --version` |
| Node | v20.20.0, npm 11.17.0, pnpm present | `node --version` |
| Go | **go1.26.5 darwin/arm64** | `go version` |
| ffmpeg | installed, with `libmp3lame` + `libopus` | `ffmpeg -encoders` |
| `lk` CLI | **2.18.8 installed** (2.19.0 in brew) | `lk --version` |
| Docker / colima / podman | **absent** | `which` → not found |
| SQLite | 3.45.3 with **FTS5** enabled | `sqlite3` version probe |
| numpy | 2.5.3 | `import numpy` |

**Env vars present:** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` (and Claude Code's own `*_MODEL` vars). **No `GOOGLE_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `LIVEKIT_API_KEY`, `GNANI_API_KEY` are set** — these must be supplied by you via a local `.env` (never committed).

**Network reachability (measured from this machine):**
- `https://api.vachana.ai` → HTTP 401 (reachable, auth enforced). DNS 3 ms, TCP connect 37 ms, TLS handshake **96 ms**, total 199 ms.
- `https://cloud.livekit.io` → HTTP 307 (reachable).
- `https://api.livekit.io` → connection failed from this network (000).

**Constraints that materially shape the plan:**
1. **No container runtime.** This is fine for the shipping configuration (the agent worker runs locally and connects to LiveKit Cloud), but it means LiveKit Cloud *agent deployment* (config B) cannot be built locally. Config B is therefore an estimate, not a measurement. **No container runtime will be installed.**
2. **No local LiveKit server.** Self-hosting (config C) would need a Go build of `livekit-server` (no macOS binary is published) or a container. **Dropped by decision** — config C is an estimate.
3. **8 GB RAM** — enough for a single local agent worker + a browser, but not for a local 30B LLM. This is fine: all LLMs are remote.
4. The **project path contains a space** (`Lecture-to-Learning Platform`). All tooling handles it, but every shell command must quote paths. Noted so it is not mistaken for a bug later.
5. **The LiveKit Cloud project must be identified before benchmarking** — its region determines the network path to the AI providers and must be recorded with every measurement.

---

## 3. Documentation findings

### 3.1 The API host is `api.vachana.ai`, not `api.gnani.ai` — VERIFIED
`app.gnani.ai` is the dashboard only. Every API call goes to `https://api.vachana.ai` with header `X-API-Key-ID: <key>`. "Vachana" is the platform namespace, not a model family (the only `vachana-`-prefixed model is `vachana-vc-v1`, voice cloning).

### 3.2 Prisma batch STT — VERIFIED
| Aspect | Value |
|---|---|
| Base | `https://api.vachana.ai` |
| Auth | header `X-API-Key-ID` |
| Model | `gnani-prisma-v2.5` |
| Flow | **Asynchronous.** `POST /stt/v3/batch/jobs` (201) → `POST /stt/v3/batch/jobs/{id}/start` (202) → poll `GET /stt/v3/batch/jobs/{id}` |
| States | `CREATED→STARTING→QUEUED→IN_PROGRESS→COMPLETED`; terminals `COMPLETED, PARTIAL_FAILURE, FAILED, START_FAILED, CANCELLED` |
| Upload | `multipart/form-data`, file field **`files`** (not `audio_file`), plus a `config` form field containing a JSON **string** |
| `config` fields | `model`, `language_code`, `mode`, `with_diarization`, `num_speakers` (1–8), `is_multi_channel`, `with_denoise`, `bias_list`, `bias_score` |
| **Max file size** | **10 MB per file** (multipart); ZIP 50 MB compressed / 200 MB decompressed |
| **Max duration** | **4 hours per file**; min 0.1 s |
| Max segments | 5,000 per file |
| Max files/job | 100 |
| Formats | wav, mp3, mp4, flac, ogg, opus, m4a, aac, webm, amr |
| Poll interval | **≥ 10 s** (docs suggest 30 s for 100+ files) |
| Rate limits | Exist (429) but **no numeric RPM limit is published** — UNVERIFIED |
| `transcript_url` | pre-signed S3, **1-hour expiry**, no auth needed |
| ITN | **Not supported on Batch** |
| Webhooks | `job.completed` / `job.partial_failure` / `job.failed` / `job.cancelled`; docs say treat as best-effort, polling is source of truth |

**Transcript JSON schema** (downloaded from `transcript_url`): top-level `file_id`, `job_id`, `original_path`, `language_code`, `duration_seconds`, `model`, `mode`, `segments[]`, `full_transcript`, `created_at`. Each segment: `segment_id`, `start_time` (seconds, float), `end_time`, `text`, `speaker_id`, `confidence`, `language_detected`, `sentiment`, `emotion`.

Three traps I would have hit:
- `segment_id` is **`0` for every segment** in a live-verified sample — not a unique key. Use array index.
- `confidence`, `language_detected`, `sentiment`, `emotion` are **always `null`**. Do not build logic on them.
- `full_transcript` is a **flat concatenation** of segment text with no punctuation and no case restored.

**Batch language support is contradictory in the docs.** The prose and the Supported-Languages table say Batch excludes `gu-IN` and `pa-IN` (8 languages); the embedded OpenAPI `STTConfig.language_code` description and the `UNSUPPORTED_LANGUAGE` error example list all 10. **UNVERIFIED — must be probed at runtime.** The demo lecture is Telugu (`ta-IN`), which is unambiguously supported on every endpoint, so this ambiguity does not put the demo at risk; only a Gujarati *lecture* would.

**No `timestamps` parameter exists** — timestamps come from segmentation/diarization. Whether per-file segment timestamps are relative to the file or to the job is **not stated in the docs** (UNVERIFIED) and must be established with a 2-chunk probe.

### 3.3 Gnani streaming STT — VERIFIED
- **WebSocket**: `wss://api.vachana.ai/stt/v3/stream`. Auth via **upgrade headers** (`x-api-key-id`), not query params.
- Required headers: `x-api-key-id`, `lang_code`. Optional: `x-sample-rate` (8000/16000/44100/48000), `x-format` (`verbatim`|`transcribe`), `itn_native_numerals`, `x-enable-substitution` + `x-substitution-map`, `x-bias-list` + `x-bias-score`, and five **server-side VAD** headers: `x-vad-threshold` (0.7), `x-min-silence-ms` (500), `x-min-speech-ms` (100), `x-min-segment-ms` (120), `x-max-speech-s` (off).
- VAD headers are **fixed for the connection lifetime**.
- Audio: **raw PCM s16le mono, 1024 bytes (512 samples) per frame, at real-time cadence.** No container format mid-stream.
- Documented server events: `connected`, `processing`, `transcript`, `error`, `settings_updated`. The `transcript` event carries `text`, `audio_duration_ms`, `segment_id`, `segment_index` (0-based, monotonic — the correct dedup key), `latency` (ms from end-of-speech to delivery).
- **No partial transcripts.** There is no documented `partial_transcript` event for `/stt/v3/stream`. Every transcript is final for a VAD-closed segment. A stray mention of an `x-partials` header appears once in the Word Boosting section with no header row, no schema, no example — **UNVERIFIED, do not rely on it.**
- The server performs **its own VAD and its own endpointing**: "Every closed segment becomes exactly one `transcript` message." `x-min-silence-ms` is the single biggest lever on end-of-utterance latency.
- Session hard cap: **15 minutes**, then reconnect.
- Browser `WebSocket` cannot set headers → the browser must go through our backend. (We use LiveKit transport anyway, so this is moot for the student path.)
- **The `gnani-vachana` Python SDK does not expose VAD tuning** — a raw WebSocket client is required for those headers. The LiveKit plugin also does not expose them.

### 3.4 Gnani Timbre TTS — VERIFIED
- `POST https://api.vachana.ai/api/v1/tts/inference` → **raw binary audio**; `POST /api/v1/tts/sse` → **chunked SSE** (base64); `wss://api.vachana.ai/api/v1/tts` → **chunked WebSocket** (base64 JSON). Auth header `X-API-Key-ID` (upgrade header on WS).
- Body: `text` (req), `model` (req), `voice` (req in prose), `language` (req in prose), `speed` (0.85–1.15, or `"slow"`/`"medium"`/`"fast"`), `audio_config` (req per OpenAPI) → `sample_rate` (8000–48000, default 48000), `num_channels`, `sample_width`, `encoding` (`linear_pcm`, `pcm_s16le`, `pcm_mulaw`, `pcm_alaw`, `oggopus`), `container` (`wav`, `raw`, `mp3`, `ogg`, `mulaw`, `alaw`).
- **Prose vs OpenAPI conflict:** prose marks `text`/`model`/`voice`/`language` required; the OpenAPI `required` array is `["text","model","audio_config"]`. → **Send all five.**
- Models: `timbre-v2.5` (42 voices, one preferred language each), `timbre-v3.0` (13 voices, **each speaks all 10 languages**, plus 13 inline audio tags), `timbre-v2.0` (4 voices, **being deprecated**).
- **Voices are not interchangeable between models** → a v2.5 voice with `model: "timbre-v3.0"` returns 400.
- Language enum includes `hi-IN`, `gu-IN`, `hi-en` (Hinglish), `en-IN`, `auto`, and 6 more. **Hindi and Gujarati are both explicitly supported.**
- TTS bills **per character**; inline audio tags count as billable characters. OGG/Opus cannot be streamed (single chunk).

### 3.5 LiveKit Agents 1.8.8 (released 2026-10-15) — VERIFIED against source
This is the newest release and the version everything below is pinned to. `requires_python = ">=3.10,<3.15"`.

**Corrections to common assumptions, verified in source at tag `livekit-agents@1.8.8`:**

| Assumption | Reality |
|---|---|
| `from livekit.agents import RoomSession` | **Does not exist.** Use `AgentSession` + `room_io.RoomOptions`. (`RemoteSession` exists but is a different thing — it drives a *primary* session over a room.) |
| `WorkerOptions(entrypoint_fnc=...)` | Replaced by `AgentServer()` + `@server.rtc_session()` decorator. `cli.run_app(server)` still works but is **marked deprecated** in favour of the `lk` CLI and `python -m livekit.agents`. |
| `livekit-plugins-turn-detector` | **Deprecated** — emits a `DeprecationWarning`; use `livekit.agents.inference.TurnDetector` (in core since 1.6.1, **no extra pip package**). |
| `livekit-plugins-silero` | Still published, but the **default VAD is now native** `inference.VAD(model="silero")` (bundled via `livekit-local-inference`, 35 MB wheel with the ONNX models included). Pick one; do not mix. |
| `tts_text_provider=` / `sentence=` session params | **Do not exist** in 1.8.8. Sentence segmentation is now internal to the framework (`Agent.tts_node`), overridable by overriding `tts_node`. The user-facing knob is `tts_text_transforms` (default `["filter_markdown","filter_emoji"]`). |
| STT plugin implements `_stream_impl` | **No.** The contract is: override `stream()` → return a `RecognizeStream` subclass → implement `RecognizeStream._run()` → emit via `self._event_ch.send_nowait(stt.SpeechEvent(...))`. |
| Flat params like `allow_interruptions=`, `min_endpointing_delay=`, `preemptive_generation=True` | Still accepted but emit **deprecation warnings targeting v2.0**. Use `turn_handling=TurnHandlingOptions(...)`. |

**`TurnHandlingOptions` (authoritative key list, from `livekit/agents/voice/turn.py`):**
```
turn_detection:   "stt" | "vad" | "realtime_llm" | "manual" | TurnDetector instance
endpointing:      { mode: "fixed"|"dynamic", min_delay, max_delay, alpha }
interruption:     { enabled, mode: "adaptive"|"vad", min_duration, min_words,
                    resume_false_interruption, false_interruption_timeout,
                    discard_audio_if_uninterruptible, backchannel_boundary }
preemptive_generation: { enabled, preemptive_tts, max_speech_duration, max_retries }
user_turn_limit:  { max_words, max_duration }
```
Defaults: `endpointing.min_delay=0.5`, `max_delay=3.0`; `interruption.min_duration=0.5`, `min_words=0`, `resume_false_interruption=True`, `false_interruption_timeout=2.0`, `backchannel_boundary=(1.0,1.0)`; `preemptive_generation.enabled=True` but `preemptive_tts=False`.

**Turn detector (core).** `inference.TurnDetector(version="v1"|"v1-mini")`. `v1` is cloud-hosted via the LiveKit Inference gateway (needs `LIVEKIT_API_KEY`/`SECRET`); **`v1-mini` runs locally as a bundled ONNX model — no network, no extra package**. Auto-selects `v1` when hosted/dev-mode, else `v1-mini`. **The detector operates on raw audio frames, not on transcript text** — this is the fact that makes the whole design work (see §4). Per-language thresholds exist for 15 languages including `hi`; **there is no `gu` (Gujarati) threshold**, so Gujarati falls back to the English default (0.36). Requires the VAD's `min_silence_duration` to be **≥ 0.2 s** (`MIN_SILENCE_DURATION_MS = 200`), else LiveKit warns and behaviour degrades. The core bundled VAD default is 0.25 s — above the floor.

**Preemptive generation** is triggered by the STT emitting `PREFLIGHT_TRANSCRIPT` (Deepgram-Flux-style) *or* by a changed `FINAL_TRANSCRIPT`. Since Gnani emits no preflight and no partials, **preemptive generation will rarely fire** in this stack. It is not a meaningful latency lever here; I will leave it enabled (default) but not claim a benefit for it, and I will measure rather than assume.

**End-of-turn flow in `audio_recognition.py` (the part that decides the answer):**
- The framework's own VAD drives speech start/end; the STT supplies text.
- `FINAL_TRANSCRIPT` events **accumulate**: `self._audio_transcript += f" {transcript}"` — so multiple final segments for one utterance are concatenated, not treated as separate turns.
- At turn close, `_commit_user_turn()` waits for the final transcript with a timeout; if the last final transcript is older than 0.5 s it **pushes silence frames into the STT to flush its buffer**, then waits for `_final_transcript_received`.
- ⇒ A late-arriving Gnani transcript is *waited for*, not lost.

**Available metrics** (for the benchmark harness, all in `livekit/agents/metrics/base.py`): `STTMetrics` (`duration`, `audio_duration`, `streamed`, `input_tokens`/`output_tokens`, `acquire_time`, `connection_reused`), `LLMMetrics` (`ttft`, `duration`, `tokens`), `TTSMetrics` (`ttfb`, `duration`, `audio_duration`, `characters`), `EOUMetrics`, `EOTInferenceMetrics`, `InterruptionMetrics`, `VADMetrics`. Delivered via the session event `metrics_collected`.

**Plugins available at 1.8.8:** ~80 `livekit-plugins-*` packages. Relevant here: `gnani` (1.8.8 ✓), `google` (1.8.8), `groq` (1.8.8), `openai` (1.8.8), `cerebras` (1.8.8), `silero` (1.8.8), `turn-detector` (1.8.8, deprecated), plus `livekit` 1.1.21 (browser/JS SDK) and `livekit-local-inference` 0.2.7.

**`livekit-plugins-gnani` 1.8.8 (verified by reading the plugin source):**
- `from livekit.plugins.gnani import STT, TTS`.
- `STT(language="en-IN", api_key=None, sample_rate=16000, base_url="https://api.vachana.ai", format="verbatim", itn_native_numerals=False, recognize_method="websocket")`.
  - `STTCapabilities(streaming=recognize_method=="websocket", interim_results=False, aligned_transcript=False)`.
  - `recognize_method="rest"` → `POST /stt/v3` with multipart `audio_file`; LiveKit auto-wraps a non-streaming STT in `stt.StreamAdapter` driven by the **local** VAD (verified in `voice/agent.py:513-522`).
  - Websocket mode sends 1024-byte PCM frames and maps `transcript` → `FINAL_TRANSCRIPT`, `speech_start`/`vad_start` → `START_OF_SPEECH`, `speech_end`/`vad_end` → `END_OF_SPEECH`.
  - The plugin's WS connect headers are only `x-api-key-id`, `lang_code`, `x-sample-rate`, `x-api-request-id`, `x-format`, `itn_native_numerals` — **it exposes none of the five VAD-tuning headers.**
- `TTS(voice="Nalini", model="timbre-v2.5", language=None, sample_rate=16000, num_channels=1, encoding="linear_pcm", container="wav", bitrate=None, api_key=None, base_url=..., synthesize_method="rest")` with `synthesize_method ∈ {rest, sse, websocket}`; `stream()` always uses the WebSocket transport.

**Discrepancy I will not paper over (VERIFIED conflict):** the plugin handles server message types `speech_start`, `vad_start`, `speech_end`, `vad_end`, but the current Gnani WebSocket documentation lists only `connected`, `processing`, `transcript`, `error`, `settings_updated`. The plugin's source comment describes `vachana-stt-v3`; the docs describe `/stt/v3/stream`. Either the docs omit two event types, or the plugin targets a slightly different protocol revision. **Consequence:** the plugin's `START_OF_SPEECH`/`END_OF_SPEECH` may never fire, and the plugin forwards **none** of Gnani's five VAD-tuning headers. This is the single highest-risk unknown in the whole stack and drives the mitigation in §4.

### 3.6 The challenge's actual rules — VERIFIED (fetched the live pages)
- Deadline **2026-11-10 23:59 IST, fixed**. Registration is **free**; **5,000 programme credits per registrant** (go to the primary registrant only for a team of two). Credits apply to models on the API platform (`app.gnani.ai/voice`); the agent builder is out of scope.
- Submission requires: a **demo video**, a **written project explanation** (problem, users, tech, each member's contribution), the **list of Gnani models/APIs used**, and **LinkedIn/X post links** with both `#GreatIndianAIInternshipChallenge` and `#GnaniAI`. A repo URL and a public deployment are **not** required.
- **The rule is "at least one Gnani model/API"** — "Prisma, Timbre **or** Evon". The brief's stronger requirement (use **both** Prisma and Timbre) is stricter than the challenge and is what we will build to, which is safe.
- **"Timbre (text to speech, beta)" is official wording** in the T&C and the challenge model card — it refers to the TTS line, whose then-current version was v2.5. There is no product literally named "Timbre beta"; the API models are `timbre-v2.5` and `timbre-v3.0`.
- "Timbre v2.0 will be deprecated soon" per the docs. `timbre-v2.5` is described as "now the recommended TTS model".
- **No language is mandatory.** No LiveKit requirement. No deployment requirement. 10 award categories × ₹50,000, incl. *"Mixed Vibes" (code-mixed voice — Hinglish/Tanglish/Benglish)* and *"Main Character" (regional / low-resource Indian language)* and *"Demo Day Drop (best 60-second demo video)"* — the last one aligns exactly with this brief's 60-second demo target.
- "Synthetic data only" — no real phone numbers, Aadhaar/PAN, or recorded calls (DPDP). **This is why the demo lecture is a public, clearly-licensed open lecture and not a student's recording.**
- LiveKit is **not mentioned anywhere** on the challenge pages — it is a first-party, officially documented integration path, not a requirement. Using it is a technical choice, and I will say so plainly in the write-up rather than imply it is mandated.

### 3.7 Model-selection facts for the two LLM workloads — VERIFIED unless noted
- **Gemini** (`livekit-plugins-google` 1.8.8, uses the new `google-genai` SDK): `google.LLM(model=..., api_key=..., vertexai=...)`. Streaming is the only mode (`generate_content_stream`). ⚠️ `gemini-4-*` models **reject `temperature`/`top_p`/`top_k` outright (400)** and use `thinking_level` instead of `thinking_budget`; Gemini 2.5-and-earlier use `thinking_budget`. Current stable IDs: `gemini-4-argon` (1M context, long decode), `gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-3.1-pro-preview`. `gemini-2.5-flash` is retired (2026-10-20) — **must not use**. The plugin's `ChatModels` Literal is stale but accepts any string at runtime.
- **Groq** (`livekit-plugins-groq` 1.8.8): the LLM class lives in `services.py`, not `llm.py` (import path is still `groq.LLM`). It **subclasses `openai.LLM`**, so it streams and accepts `base_url`. Live Groq catalogue: `llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b` (production) + `qwen/qwen3.8-27b`, `minimaxai/minimax-m2.7` (preview). The plugin's `LLMModels` Literal is **stale** — it lists `meta-llama/llama-4-scout-17b-16e-instruct`, `moonshotai/kimi-k2-instruct-0905`, `qwen/qwen3-32b`, none of which are in Groq's live catalogue. Do not use those three.
- **Cerebras** has its own plugin with `gpt-oss-120b`, `qwen-3.8-27b`, `zai-glm-4.7`, `gemma-4-31b`, plus gzip/msgpack payload compression that cuts TTFT on long prompts.
- **Fireworks has no LLM plugin at 1.8.8** (STT only) — use `openai.LLM.with_fireworks(...)`.
- `openai.LLM` works against any OpenAI-compatible endpoint via `base_url`, and ships 15 provider presets (`with_cerebras`, `with_fireworks`, `with_together`, `with_openrouter`, `with_ollama`, …).
- **LiveKit Inference gateway** (`inference.LLM("google/gemini-3.8-flash")`) authenticates with a **LiveKit-minted JWT**, not a provider key — so a custom `base_url` there is not a bring-your-own-model path. Build ($0) includes **$2.50 of inference credits**; Gemini 3.8 Flash via the gateway is $0.75 in / $3.75 out per 1M. Given you have direct Gemini and Groq access, **direct provider keys are cheaper and simpler than the gateway** — the gateway's only unique value here is the cloud turn-detector `v1`.
- **Gujarati text generation is the weak link.** Google's current text-generation language list **excludes Gujarati**; only the now-shut-down 1.5/2.0 models listed it. Gujarati *is* officially supported for TTS (Timbre `gu-IN`) and for STT (streaming Realtime supports `gu-IN`; Batch is contradictory). Llama 3.3's Gujarati quality is **UNVERIFIED**. This is exactly what the Phase 2 benchmark must measure rather than assume.
- Embeddings, if needed: `gemini-embedding-2` / `gemini-embedding-001` exist (PARTIALLY VERIFIED — found in Google's embeddings doc, not exercised).

### 3.8 Pricing and credit risk
Gnani publishes **no rate card** (VERIFIED: no pricing page, no per-minute/per-character rates anywhere in the docs corpus). The only published numbers are from a **third-party reseller (CallMissed)**, usable for planning but not authoritative: **1 credit ≈ ₹1 (≈US$0.0104)**; `gnani-prisma-v2.5` ≈ **US$0.2813/hour** (≈27 credits/hour); `gnani-timbre-v3.0` ≈ **US$0.2813 / 10,000 characters** (≈27 credits/10K chars). Credits never expire there.

**Planning figures (estimates, flagged as such):** 45-minute lecture STT ≈ 20 credits. A 45-minute TTS script ≈ 25K chars ≈ 68 credits. A full development cycle — 2 transcriptions plus ~40 minutes of TTS across all tests — ≈ 150–250 credits of the 5,000. **Budget headroom is large.** The real risk is *waste*, not cost: re-transcribing the same audio, or looping the TTS in a test. Hence hard requirements: content-hash caching of transcripts, mocked STT/TTS in unit tests, and cached fixtures for regression runs.

**LiveKit Cloud** pricing is not a stated cost driver here: Build is $0/mo with $2.50 of inference credits; Ship is $50/mo. Cloud agent hosting and egress may be billed — **UNVERIFIED** until the account is inspected, and the brief forbids creating paid resources without approval.

---

## 4. Architecture proposal

```
┌──────────────────────────── Browser (mobile-first) ────────────────────────────┐
│  Screen A: Notes Reader        │        Screen B: Doubt Chat                   │
│  headings, TOC, timestamp links│  state: listening/thinking/speaking           │
│  KaTeX, Noto Devanagari+Guj    │  live transcript, waveform, text fallback    │
└───────────────┬──────────────────────────────────────────┬────────────────────┘
                │ HTTPS (notes API, token mint)            │ WebRTC audio+data
                │                                          │
┌───────────────▼──────────────────────────────────────────▼────────────────────┐
│                     FastAPI backend  (single deployable)                      │
│  POST /api/lectures/upload      GET /api/lectures/{id}/notes                   │
│  GET  /api/notes/{id}           POST /api/chat  (text fallback)                │
│  POST /api/flashcards/review    GET  /api/health  /api/ready                   │
│  POST /api/livekit/token        (mints room token for the browser)             │
│                                                                                │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  ┌─────────────────────┐ │
│  │ Pipeline     │  │ Retrieval    │  │ Storage    │  │ LLM router          │ │
│  │ ingest→      │  │ FTS5 BM25    │  │ SQLite     │  │ Gemini (notes)      │ │
│  │ transcribe→  │  │ → LLM rerank │  │ + FTS5     │  │ Groq   (chat)       │ │
│  │ segment→     │  │ → grounded   │  │ (WAL)      │  │ env-configurable    │ │
│  │ notes        │  │   answer     │  │            │  │                     │ │
│  └──────┬───────┘  └──────────────┘  └────────────┘  └─────────────────────┘ │
└─────────┼────────────────────────────────────────────────────────────────────┘
          │
   ┌──────▼──────────────────────────────────────────────────────────────┐
   │  LiveKit Agents worker (separate process, livekit.agents 1.8.8)     │
   │  AgentSession: stt=gnani STT  llm=groq/google  tts=gnani TTS        │
   │  turn_detection = inference.TurnDetector("v1-mini")  ← audio-based │
   │  vad = inference.VAD("silero", min_silence_duration=0.5)            │
   │  RAG via HTTP → backend /api/retrieve  (shares SQLite, no duplication)│
   └──────┬────────────────────────────────────┬──────────────────────────┘
          │ WebSocket STT (1024B PCM frames)   │ WS TTS (base64 chunks)
          │                                    │
   ┌──────▼─────────────────┐        ┌─────────▼───────────────┐
   │ Gnani Prisma          │        │ Gnani Timbre            │
   │ wss://api.vachana.ai  │        │ api.vachana.ai/api/v1/tts│
   │ /stt/v3/stream        │        │ (websocket transport)   │
   └────────────────────────┘        └─────────────────────────┘

  Batch path (offline, teacher):
   local .mp3/m4a → ffprobe validate → ffmpeg → 16kHz mono MP3
   → silence-aware chunking (<10MB) → POST /stt/v3/batch/jobs (multipart, files[])
   → poll ≥10s → download transcript_url (1h TTL) → cache by content hash
   → normalise → segment into timestamped topics → LLM notes → merge → SQLite
```

**Data flow for one student question:** browser mic → WebRTC → LiveKit room → worker → 1024-byte PCM frames → Gnani streaming STT (server VAD segments) → final transcript per segment → LiveKit's silero VAD + local turn detector (v1-mini, **audio-based**) decide end-of-turn → worker POSTs the question to the backend → backend retrieves candidate segments (FTS5 BM25) → LLM reranks + answers grounded in those segments → answer streams back as tokens → LiveKit's sentence tokenizer feeds the TTS `SynthesizeStream` → Timbre WS returns base64 audio chunks → browser plays. Interruption: student's speech triggers LiveKit's VAD → the agent's speech is truncated and the turn is handed over.

**The single most important architectural decision — who owns end-of-turn.** The brief demands LiveKit own it. Three facts force the design:
1. Gnani's server does its own VAD **and its own endpointing** — the server decides when a segment closes. `x-min-silence-ms` (default 500 ms) is the end-of-utterance latency lever, and it is **not reachable through the plugin**.
2. Gnani emits **no partial transcripts** — only final, per VAD-closed segment.
3. LiveKit's **turn detector is audio-based** (`push_frame`/`run_inference` over raw frames), so it does **not** need partial transcripts to judge whether the student has finished.

⇒ Configure LiveKit with `turn_detection = inference.TurnDetector(version="v1-mini")` (local, bundled, no extra package, no network) and a bundled Silero VAD with `min_silence_duration=0.5` (≥ the 0.2 s floor the detector requires, and generous enough for pause-heavy Hinglish). **LiveKit's VAD + turn detector then own turn boundaries; Gnani's server VAD only governs when a transcript becomes available.** This is exactly the decoupling the brief asks for, and it is the answer to the "STT/VAD interaction" risk.

⇒ Because the plugin forwards **neither** the VAD-tuning headers **nor** (per current docs) the `speech_end` events, I will implement a **thin subclass of the official plugin's `STT` class** in our repo that (a) adds the five Gnani VAD headers to the WS upgrade, (b) derives `START_OF_SPEECH`/`END_OF_SPEECH` from our own VAD rather than from undocumented server events, and (c) exposes a `rest` mode that lets LiveKit's `StreamAdapter` drive segmentation entirely. This is a supported extension of the officially-installed plugin — not a reimplementation — and it is the only way to satisfy "LiveKit must remain responsible for deciding when the student has completed a conversational turn" while also controlling Gnani's endpointing. **Both modes will be implemented and benchmarked; the winner becomes the default.** The `rest` mode is the more conservative fallback: it hands segmentation wholly to LiveKit's VAD, at the cost of one REST round-trip per utterance instead of a streaming transcript.

**Grounding rule implementation (no invented equations).** Three layers:
1. *Deterministic pre-pass* over segments flags visual-dependent speech via a pattern list (`ye wala`, `is par`, `is diagram`, `figure`, `graph`, `screen par`, `dekh raha`, `is equation`, `niche diya`, …) → segment marked `visual_dependent=true`.
2. *Prompt instruction* to the note generator: never reconstruct a missing diagram/equation; emit an explicit "unclear — depends on visual material not present in audio" marker with the timestamp and name the missing artefact.
3. *Post-validation* that every generated note cites segment IDs that exist in the source, and that any note touching a `visual_dependent` segment carries the uncertainty marker. A note that fails validation is rejected and regenerated, not shipped.

**Resumability & state.** A `lectures` table with a `status` state machine (`uploaded → transcoding → transcribed → segmenting → noting → ready`, plus `failed` with a stage-specific error) and per-stage checkpoint rows. Each stage is independently re-runnable; a note-generation failure never touches the stored transcript. Content hash (`sha256` of the source audio) is the cache key, so transcripts survive restarts and identical uploads are never re-transcribed.

---

## 5. Stack choices

| Layer | Choice | One-line justification |
|---|---|---|
| Language/runtime | **Python 3.12** in a `uv`-managed venv | `livekit-agents` needs ≥3.10,<3.15; 3.12 has the widest native-wheel coverage (`onnxruntime` cp311–cp314, `livekit-local-inference` cp310–cp314) and is already downloaded by uv. |
| Agent framework | **livekit-agents 1.8.8** (pinned) | Required by the brief; 1.8.8 is the current release and the only one with the `gnani` extra, the core turn detector, and the `AgentServer` API. |
| STT (live) | **`livekit-plugins-gnani` 1.8.8**, `STT`, websocket mode (benchmarked vs `rest`) | Official Gnani-maintained plugin; websocket is the only streaming-capable path and the only one with acceptable turn latency. |
| STT (batch) | **Direct HTTP** to `/stt/v3/batch/jobs` | The batch API is not exposed by the LiveKit plugin, and the brief requires keeping batch ingestion and live conversation separate. |
| TTS | **`livekit-plugins-gnani` 1.8.8**, `TTS`, `synthesize_method="websocket"` | WebSocket transport is the only chunked/streaming option the plugin's `stream()` uses, enabling sentence-level incremental synthesis. |
| Turn detection | **Core `inference.TurnDetector("v1-mini")`** | Local ONNX, bundled, no network, audio-based so it works without partial transcripts; the deprecated `livekit-plugins-turn-detector` is not used. |
| VAD | **Core `inference.VAD("silero")`, `min_silence_duration=0.5`** | Bundled default; ≥0.2 s floor required by the turn detector; generous silence suits pause-heavy Hinglish. |
| Notes LLM (Workload A) | **Gemini — `gemini-4-argon`** (via `livekit-plugins-google`) | 1M context and long decode hold a whole 45-min transcript plus the merge pass; strongest Indic grounding of the current stable models. Segment-level passes use `gemini-3.8-flash` to keep cost trivial. No `temperature` is set (Gemini 4 rejects it). |
| Chat LLM (Workload B) | **Groq — `llama-3.3-70b-versatile`** (via `livekit-plugins-groq`) | Lowest TTFT with streaming and a 131k context; independently configurable from Workload A as the brief requires. `gemini-3.8-flash` is the benchmarked alternative. |
| LLM provider isolation | Env-driven factory (`LLM_PROVIDER_CHAT`, `LLM_MODEL_CHAT`, …) | The brief forbids coupling application logic to one provider; a factory with two workload profiles satisfies it. |
| Backend API | **FastAPI + Uvicorn** | Async, native upload/streaming support, small surface, one process to deploy. |
| Storage | **SQLite 3.45 (WAL) + FTS5** | Zero-cost, file-based, survives restarts, FTS5 verified working on Devanagari/Gujarati/Hinglish text. |
| Retrieval | **FTS5 (unicode61) BM25 → LLM rerank → grounded answer with segment citations** | Lexical retrieval is exact on the vocabulary the student shares with the lecture; the LLM rerank adds semantic tolerance without a second service or embedding bill. Embeddings (`gemini-embedding-2`) remain an optional third stage if measurement shows recall is weak. |
| Frontend | **Vite + React 18 + TypeScript + Tailwind** | Static SPA served by FastAPI → one deployable artifact, fastest mobile iteration, no SSR complexity. Pairs with `livekit-client` 2.23.0 for the room. |
| Equations / script | **KaTeX** + **Noto Sans Devanagari / Noto Sans Gujarati** (self-hosted, subset) | Legible technical notation and correct Indic shaping without depending on a CDN. |
| Tests | **pytest + pytest-asyncio**, mocked STT/TTS, fixture transcripts | The brief mandates mocked unit tests and cached fixtures; no real API calls in CI. |
| Container/deploy | Local processes; `lk agent dev` for A; `lk agent deploy` for B; local `livekit-server` build for C | `lk` 2.18.8 is already installed; no container runtime is present, which shapes config B. |

**Dependency discipline:** every direct dependency is either (a) required by the brief (livekit-agents, livekit-plugins-gnani), (b) the only way to reach a mandated capability (google/groq plugins → the chosen LLMs), or (c) a standard tool for the layer (FastAPI, React, Tailwind, pytest). No vector database, no embedding service, no B2 framework, no extra HTTP client (LiveKit and the SDKs bring `aiohttp`/`httpx`).

---

## 6. Latency plan

### 6.1 What gets measured
Per the brief, a structured event log per turn, using a **monotonic clock** (`time.monotonic_ns()`) for all durations, written as JSONL. Server-side stages are measured on the worker; **user-perceived** stages (playback start) are measured in the browser and reported back via a beacon, because server-side processing time ≠ perceived latency.

| Metric | Definition | Source |
|---|---|---|
| `audio_input_complete` | monotonic ns when the last audio frame of the turn enters the worker | worker |
| `end_of_speech_detected` | when the VAD fires END_OF_SPEECH | worker (VAD event) |
| `stt_final_transcript` | when the final transcript for the turn is received | worker (STT event) |
| `retrieval_start` / `retrieval_complete` | around FTS5 query + LLM rerank | worker/backend |
| `llm_request_start` | when the chat LLM request is issued | worker |
| `llm_first_token` | TTFT | worker (LLMMetrics.ttft) |
| `llm_complete` | full answer available | worker |
| `tts_request_start` | when synthesis begins for the first sentence | worker |
| `tts_first_byte` | first playable audio chunk | worker (TTSMetrics.ttfb) |
| `playback_start` | first sample actually rendered to the speaker | browser beacon |
| `e2e_first_audible` | `playback_start` − `audio_input_complete` | correlated |
| `e2e_complete` | end of agent speech − `audio_input_complete` | correlated |

LiveKit's own `metrics_collected` event supplies `STTMetrics`, `LLMMetrics`, `TTSMetrics`, `EOUMetrics`, `EOTInferenceMetrics`; we correlate our own markers by `request_id`/`turn_id` and record the LiveKit Cloud **region** for every run, since the STT/LLM/TTS calls are remote.

### 6.2 Method
- **≥ 20 measured end-to-end turns per configuration.** Turns are driven by a scripted client (LiveKit JS/Python SDK) that joins a real room, plays a synthesised question, and records the agent's audio — a *real* voice conversation through the real pipeline, not a simulation, and repeatable across configurations so the only variable is the deployment.
- A smaller set of human-in-the-loop turns over `lk agent console` validates that the scripted driver reflects real behaviour.
- Report **n, p50, p95, failures**, plus: warm-up runs excluded and counted, cache hits excluded, region, network path, concurrency = 1, model IDs, and whether the run was scripted or human.
- If a configuration cannot be run, we report the completed sample size and the reason. **No fabricated numbers.**

### 6.3 The deployment configurations

**Owner decision: LiveKit Cloud only.** The account, URL and API keys already exist; no local LiveKit server and no container runtime will be installed. This means **one** configuration is measured for real and two are documented as estimates.

| | A. Local worker + LiveKit Cloud | B. Worker on LiveKit Cloud | C. Self-hosted LiveKit on this Mac |
|---|---|---|---|
| Status | **MEASURED** — the shipping configuration | **ESTIMATE** (not run) | **ESTIMATE** (not run) |
| Transport | LiveKit Cloud (existing account) | LiveKit Cloud | Local `livekit-server` |
| Where STT/LLM/TTS run | Remote (Gnani + Gemini/Groq) | Remote (same) | Remote (same) — all three still leave the machine |
| Media path | Cloud SFU | Cloud SFU | Local SFU |
| How it would run | `lk agent dev` against the cloud project | `lk agent create` + `lk agent deploy` (needs a local image build) | `go install github.com/livekit/livekit-server/cmd/server@v1.13.10` then `livekit-server --dev` |
| Why not run | — | no container runtime; installing one was declined | declined outright |
| Estimate basis | — | the agent↔room leg becomes a cloud-local call instead of a WAN call; **the serial STT→LLM→TTS chain stays identical**, so expected end-to-end p50 changes by tens of milliseconds, not hundreds. Operational overhead: near zero, because LiveKit runs it. Cost: LiveKit Cloud agent hosting + egress (**UNVERIFIED** until the account is read). | removes one media hop but **all three AI calls still traverse the WAN** — the dominant cost. Adds operational burden: a process to supervise, TLS, uptime. **Expected to be no faster than A and less reliable.** |

**Why the three collapse honestly.** The end-to-end user-perceived latency of this product is dominated by the serial chain *student audio → Gnani STT → LLM → Timbre TTS → speaker*, and every one of those three is a remote call to a different provider in every configuration. The LiveKit media hop is a single low-latency WebRTC leg. Moving the worker into LiveKit Cloud (B) shortens one hop; self-hosting the SFU (C) shortens a different one. Neither removes a Gnani/Gemini/Groq round-trip. So the honest expectation is that **A is within tens of milliseconds of B and no faster than C**, and that the differences are dominated by reliability and operational cost rather than latency.

That expectation is a hypothesis, not a result. §6.2's benchmark will measure A for real, and B/C will be presented with this reasoning and their reproduction steps, explicitly labelled as estimates. The brief's rule applies: *"If a setup cannot be run, provide a documented, evidence-based estimate rather than fabricated benchmark numbers."* No B or C latency figure will be presented as measured.

**Access needed from you:** the LiveKit Cloud project URL and API key/secret set as env vars (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`) — never pasted into chat. The project region must be recorded with every measurement, since it affects the network path to the AI providers.

### 6.4 Streaming / interruption optimisations to evaluate
- Sentence-level incremental TTS: LiveKit's internal sentence tokenizer already feeds the TTS stream per complete sentence, so the first complete speakable sentence starts synthesis before the LLM finishes. We will measure first-audible-time with and without it.
- `preemptive_generation` — **expected to be inert here** (it needs a `PREFLIGHT_TRANSCRIPT` that Gnani does not emit). We will measure rather than claim a benefit.
- `interruption.mode`: `"vad"` works everywhere; `"adaptive"` needs the LiveKit Cloud inference endpoint (billed) — evaluate on config A/B only and report the cost.
- `resume_false_interruption` + `false_interruption_timeout` tuning to stop the agent talking over a student who is merely thinking.

---

## 7. Security and reliability risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Secret leakage** into logs, reports, git | Secrets read only from env; a `.env.example` with names and dummy values ships, `.env` is gitignored; structured logs carry request/turn IDs and lengths, never transcripts, keys, or audio; a test asserts no key-shaped string appears in log output. |
| 2 | **Unbounded credit burn** from re-transcription or TTS loops | Content-hash transcript cache; single-flight lock per content hash; a global daily credit ceiling that refuses new batch jobs beyond it; no real API calls in unit tests; explicit per-call cost estimates logged before a large job. |
| 3 | **Upload abuse** (huge files, wrong types, malformed media) | Extension + magic-byte check, ffprobe decode of duration/streams, hard size cap, 10 MB-per-chunk enforcement before upload, request timeouts, and a per-IP upload rate limit. |
| 4 | **Chunk-boundary corruption** (lost/duplicated words, broken sentences) | Silence-aware chunk boundaries via `silencedetect`; a two-chunk probe test establishes whether per-file timestamps are file-relative or job-absolute; offsets are re-applied deterministically; a merge test asserts no segment is lost or duplicated and that segment order is monotonic. |
| 5 | **Non-idempotent billable retries** | Job creation is keyed by content hash; the pipeline records the job ID before starting, so a crash resumes by polling the existing job rather than creating a new one. Retries are bounded with exponential backoff + jitter and only on retryable codes (429/5xx/network). Non-retryable errors (4xx validation) fail fast. |
| 6 | **Transcript loss on downstream failure** | Stages are independently resumable; the transcript is written to SQLite and fsynced before segmentation begins; a note-generation failure leaves the transcript intact and marks only the noting stage failed. |
| 7 | **Fabricated answers** when the lecture does not cover the question | Retrieval returns a confidence signal; below a calibrated threshold the system answers "this isn't covered in the lecture" **without** calling the LLM; the prompt forbids external knowledge; a post-check rejects unsupported claims. |
| 8 | **Invented equations / diagrams** | Three-layer grounding rule (§4) with post-generation validation. |
| 9 | **Indefinite hangs** | Explicit connect/request timeouts on every remote call; a total per-turn deadline; a job-level watchdog; the UI shows a timeout state with retry. |
| 10 | **Privacy of recordings/transcripts** | Audio is not uploaded anywhere except the Gnani API. Batch path uses direct multipart upload (not a public URL — no `cloud_storage` source, which would expose the file publicly). No third-party analytics or telemetry. Storage/retention documented; a reset script clears demo state without touching user data. |
| 11 | **Rate limiting / polling violations** | Poll interval ≥ 10 s (30 s preferred), adaptive backoff on 429 honouring `Retry-After`, and a per-host concurrency cap. |
| 12 | **Provider outage** | `FallbackAdapter` on STT/TTS; text-only chat fallback when TTS is down; the health endpoint distinguishes app health from provider reachability; the UI degrades visibly rather than silently. |

**Health endpoints:** `/api/health` (liveness — process up) and `/api/ready` (readiness — DB writable, provider reachability reported as *degraded* rather than failing the check, so a Gnani outage does not take the app down).

---

## 8. Implementation phases

Directory layout (created in Phase 1):

```
/Lecture-to-Learning Platform/
├── backend/
│   ├── app/{main,config,logging,db,models}.py
│   ├── app/api/{lectures,notes,chat,flashcards,health,livekit}.py
│   ├── app/pipeline/{ingest,transcribe,segment,notes,grounding}.py
│   ├── app/retrieval/{fts,rerank}.py
│   ├── app/llm/{factory,prompts}.py
│   └── tests/
├── agent/
│   ├── worker.py                 # AgentServer + @server.rtc_session()
│   ├── tutor.py                  # Agent subclass, RAG tool
│   ├── stt_gnani.py              # thin subclass of the official plugin's STT
│   └── bench/{harness,driver,report}.py
├── web/                          # Vite + React + TS
├── data/                         # SQLite + cached artefacts (gitignored)
├── .env.example
├── pyproject.toml / uv.lock
└── RECONNAISSANCE.md
```

### Phase 1 — Notes pipeline
*Files:* `backend/app/pipeline/{ingest,transcribe,segment,notes}.py`, `db.py`, `api/lectures.py`, `api/notes.py`; `agent/../tests/` fixtures.
*Work:* upload validation → ffmpeg transcode (16 kHz mono MP3 — Prisma is a **telephonic** model, so this is also the highest-accuracy input) → silence-aware chunking under 10 MB → batch job create/start/poll → download transcript → **cache by content hash** → normalise (punctuation/case restore without altering meaning) → segment into timestamped topics → per-segment LLM notes → merge into one coherent document with headings, key terms, equations, summary, and **timestamped source links** → persist the source segments separately as the RAG corpus.
*Tests:* upload validation (type/size/duration); chunk-boundary integrity (no loss/dup, monotonic order, correct offsets — including the 2-chunk probe establishing the timestamp basis); cache hit prevents a second transcription; note-generation failure preserves the transcript; grounding marks visual-dependent content as unclear rather than inventing it; malformed model output is rejected and retried.
*Acceptance:* one real 45-minute lecture processed end-to-end via the public fixture; measured wall time, API call count, and credit usage reported; transcript cache verified by a second identical upload making **zero** transcription calls.
*Depends on:* nothing.

### Phase 2 — Voice doubt chat + latency
*Files:* `agent/{worker,tutor,stt_gnani}.py`, `backend/app/retrieval/*`, `backend/app/api/chat.py`, `agent/bench/*`.
*Work:* RAG retrieval + grounded answering with segment citations; Gnani STT (websocket, and `rest` for comparison) → LiveKit VAD + `TurnDetector("v1-mini")` → Groq/Gemini chat with streaming → sentence-level incremental TTS via Timbre WebSocket; barge-in; text-only fallback; benchmark harness with the full metric set; the deployment comparison (A measured against a LiveKit Cloud account; B and C documented as estimates with reproduction steps, per the owner decision).
*Tests:* Hindi/Hinglish/Gujarati; a question answered from the lecture; a question **not** in the lecture (must decline); slow/hesitant/pause-heavy Hinglish (pauses inside a sentence, pauses before a technical term, multiple segments per utterance, end-of-turn correctness); STT/LLM/TTS failure paths; barge-in (agent stops, does not talk over); text-only fallback; no indefinite loading; no fabricated answers.
*Acceptance:* text and voice both work; answers cite retrieved segments; streaming is visible; speech is **Timbre**; interruption works; ≥ 20 measured turns on the shipping configuration (A) with p50/p95 reported; configurations B and C are documented as estimates with the reason and reproduction steps, and no latency figure for them is presented as measured.
*Depends on:* Phase 1 (needs the corpus).

### Phase 3 — Flashcards
*Files:* `backend/app/pipeline/flashcards.py`, `api/flashcards.py`.
*Work:* generate Q/A pairs from the notes (deduplicated, non-overlapping); SM-2-style spaced repetition with a persisted schedule per card.
*Tests:* cards reflect source material; generation failure leaves existing cards intact; scheduling is deterministic (fake-clock tested); state persists across sessions.
*Depends on:* Phase 1. **Not started before Phases 1 and 2 pass.**

### Phase 4 — Polish, seeded demo, docs
*Files:* `web/src/pages/{Notes,Chat}.tsx`, `backend/app/api/demo.py`, `scripts/seed_demo.py`, `README.md`, `ARCHITECTURE.md`, `BENCHMARK.md`.
*Work:* two polished mobile-first screens; KaTeX + Indic fonts; heading navigation; timestamp deep-links; empty/loading/offline/error/permission-denied states; keyboard + screen-reader support; a seeded demo that replays a **pre-processed** lecture so the 60-second path never waits on a transcription job; a reset script; README, architecture diagram, dependency/security note, benchmark report, and a 60-second demo script. The write-up will state plainly that LiveKit was a technical choice (it is not a challenge requirement) and name the Gnani models used, as the challenge rules require.
*Acceptance:* the demo path completes in ~60 s from cached artefacts, with the cached preprocessing disclosed on screen.
*Depends on:* Phases 1–3.

**Explicitly out of scope** (per the brief): voice-based flashcard drills, a generic landing page, mock data in the demo path.

**Commit discipline:** small, reviewable commits per phase, each preceded by a passing test run and an application start-up check. The repo is initialised in Phase 1 with the brief on `main` and the implementation on `feat/gnani-lecture-platform`.

---

## 9. Open questions

Only decisions that materially change what gets built. **Items 1–3 are now closed** (see the decisions table at the top).

1. ~~LiveKit Cloud project details~~ — **CLOSED.** The account, URL and API keys exist. They will be supplied as env vars and read from the environment; no values in chat, logs, reports, or source.
2. ~~Container runtime for configuration B~~ — **CLOSED: LiveKit Cloud only, no container runtime.** B is documented as an estimate, not measured.
3. ~~Self-hosted LiveKit build (configuration C)~~ — **CLOSED: dropped.** C is documented as an estimate, not measured.
4. **Gujarati quality risk** — current Gemini text generation is documented as *not* covering Gujarati, and Llama 3.3's Gujarati quality is unverified. My plan is to measure it on a curated Gujarati/Hinglish doubt set in Phase 2 and, if it is poor, fall back to answering in Hindi with a clear note, rather than shipping broken Gujarati. Flagging it now because it changes what the student sees. **No decision needed from you unless you would rather I not attempt Gujarati at all** — I intend to attempt it, measure honestly, and degrade gracefully.
5. ~~Demo lecture language~~ — **CLOSED: Telugu open lecture.** The tutor itself stays Hindi/Hinglish/Gujarati.
6. **Model IDs to pin** — `gemini-4-argon` (notes) and `llama-3.3-70b-versatile` (chat) are my defaults; both are env-overridable and will be benchmarked against `gemini-3.8-flash` before being locked. No approval needed to benchmark; the final choice is a Phase 2 output.

**Not asked, and not needed:** no secret values, no API keys, no credits. Everything reads from env.

---

## 10. Cost and credit risks

| Item | Remote calls | Credit estimate | Approval needed |
|---|---|---|---|
| Batch STT, one 45-min lecture | 1 job (+~1-2 polls) | ≈ 20 credits | **Yes — before the first real transcription** |
| Batch STT, repeat of the same audio | **0** (cache hit) | 0 | no |
| TTS, per benchmark turn | 1 WS call | ≈ 0.3 credits/turn | yes, covered by the benchmark budget |
| TTS, full 45-min script (once) | ~5 calls | ≈ 68 credits | yes |
| LiveKit Cloud | agent hosting + egress | **UNVERIFIED** — depends on the plan | read the account; do not enable a paid plan without asking |
| Gemini / Groq | your own accounts | your own billing | already authorised by you |
| New paid infrastructure | **none** | 0 | no paid resources will be created |

**Total planned real Gnani usage for the whole build: ≈ 150–250 credits of 5,000.** The dominant risk is waste, not spend, so caching, single-flight, mocks and a daily ceiling are enforced from the first commit.

---

## 11. Assumptions table

| # | Assumption | Evidence | Confidence | How it will be validated |
|---|---|---|---|---|
| 1 | `livekit-agents` 1.8.8 + `livekit-plugins-gnani` 1.8.8 install and import cleanly on Python 3.12 arm64 | PyPI metadata; plugin source read | High | Fresh venv install + import smoke test, Phase 1 |
| 2 | The Gnani plugin's websocket mode works against the live `/stt/v3/stream` | Plugin source; docs | Medium | Live STT smoke test with a 5-second clip, Phase 1 |
| 3 | The server does **not** emit `speech_start`/`vad_end`, so the plugin's speech events may never fire | Plugin source handles them; docs omit them | **Low–Medium** | Live capture of the raw WS frame types in the first STT test; the `stt_gnani.py` subclass removes the dependency either way |
| 4 | Gnani's per-file segment timestamps are file-relative (not job-absolute) | Docs silent | **Low** | Two-chunk probe job: compare `duration_seconds`/segment times of both files |
| 5 | Batch STT supports `ta-IN` | Docs list `ta-IN` everywhere | High | One real batch job |
| 6 | Batch STT supports `gu-IN` | Docs contradict (prose says no, schema says yes) | **Low** | One tiny `gu-IN` batch job, ~10 s of audio, negligible credits — run only if a Gujarati lecture is wanted |
| 7 | Streaming STT supports `gu-IN` and `hi-IN` | Docs list both for Realtime | High | Live STT test in each language |
| 8 | LiveKit's turn detector (`v1-mini`) works on the bundled ONNX model offline | Core source: `livekit.local_inference.EOT` | High | Turn-detection smoke test with no network |
| 9 | The detector needs no transcript (audio-only EOT) | `push_frame`/`run_inference` in `eot/base.py` + `transports.py` | High | Confirm at runtime that EOT fires with Gnani's final-only transcripts |
| 10 | `min_silence_duration=0.5` on the VAD is acceptable for pause-heavy Hinglish | Turn detector floor is 0.2 s | Medium | A/B measured on the hesitation test set; false-turn rate recorded |
| 11 | `gemini-4-argon` accepts a full 45-min transcript in one context | Google models page; plugin constants (`LONG_DECODE_CONTEXT_WINDOW = 1_000_000`) | High | Real notes run; token counts logged |
| 12 | Gemini 4 rejects `temperature`/`top_p`/`top_k` | Plugin source `_ignores_sampling_params` | High | Omit them; assert no 400 in the first call |
| 13 | `llama-3.3-70b-versatile` gives acceptable Hindi/Hinglish and low TTFT | Groq catalogue; general knowledge of Llama 3.3's Indic pretraining | Medium | Benchmark on the curated doubt set (quality + TTFT) |
| 14 | Gujarati generation quality is adequate on at least one candidate | Gemini docs exclude `gu`; Llama unverified | **Low** | Benchmark on ≥10 curated Gujarati doubts; fall back to Hindi with a clear note if it fails |
| 15 | `livekit-server` builds and runs on darwin/arm64 from source | Go 1.26.5 present; module resolves on the Go proxy; `LocalStore` fallback verified in `wire.go` | Medium | **Dropped** — config C is an estimate by owner decision; the build path remains a documented alternative if C is ever revisited |
| 16 | `lk agent deploy` is blocked without a container runtime | `lk agent create --help` (`--image` requires Docker; `--image-tar` needs a prebuilt tar); no Docker/Colima present | High | Confirmed by CLI help; **B is an estimate by owner decision** |
| 17 | The Batch 10 MB limit requires chunking a 45-min lecture at common bitrates | 10 MB limit is documented; 45 min @ 128 kbps ≈ 42 MB, @ 64 kbps ≈ 21 MB, @ 32 kbps ≈ 10.5 MB | High | Computed; the pipeline transcodes to 16 kHz mono and chunks with margin |
| 18 | FTS5 `unicode61` retrieves Devanagari/Gujarati/Hinglish correctly | **I ran it**: `पानी`, `પાણી`, `बाइनरी`, `ट्री`, `binary` all matched; `ટ્રી` did not (token absent from the row, not a tokenizer failure) | High | Covered by a retrieval unit test |
| 19 | `gemini-embedding-2` exists and is usable | Found on Google's embeddings doc | Medium | Only exercised if the benchmark shows lexical recall is insufficient |
| 20 | Gnani credit consumption follows the CallMissed rate card | **Reseller's** published rate, not Gnani's; Gnani publishes no rates | Medium | Read the usage dashboard at `app.gnani.ai/voice` after the first job and reconcile against the estimate |
| 21 | The public Telugu lecture is acceptable as the demo artefact | CC BY-NC-ND 4.0, real human lecture, not a recorded call, so it respects the challenge's synthetic-data rule | High | **Confirmed by the owner.** Cite the licence in the write-up |
| 22 | 20 measured turns per configuration is achievable | Scripted LiveKit client can drive a real room and capture audio | Medium | Build the driver in Phase 2; report the actual `n` honestly. Only config A is measured |
| 23 | The LiveKit Cloud region is in or near India | Not checked — account not inspected | Low | Read the project region from the CLI/API before benchmarking, and record it with every measurement |

---

## 12. What I will do on approval

1. `git init`, commit the brief on `main`, branch `feat/gnani-lecture-platform`.
2. `uv` venv on Python 3.12; pin `livekit-agents==1.8.8`, `livekit-plugins-gnani==1.8.8`, `livekit-plugins-google==1.8.8`, `livekit-plugins-groq==1.8.8`.
3. `.env.example` with names + dummy values only; `.env` gitignored. Names: `GNANI_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `GOOGLE_API_KEY`, `GROQ_API_KEY`.
4. Phase 1: the notes pipeline, with mocked tests first and the **2-chunk timestamp probe** and the **live STT smoke test** as the first two real API calls (both tiny, both reported).
5. Then request your approval before the first full batch transcription of the Telugu lecture (~20 credits).
6. Phase 2 against the LiveKit Cloud project already provisioned.

**Deployment footprint: none.** No container runtime, no local LiveKit server, no cloud resources created, no paid plan enabled, nothing installed outside the project directory.

**Standing rule for every phase:** measure, then report. Measured / estimated / mocked / unverified are labelled explicitly, and no feature is called production-ready because it starts.
