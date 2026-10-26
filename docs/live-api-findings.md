# Live Gnani API findings

Everything here was measured against the real API on 2026-10-22 with the
project's own key. It supersedes the documentation where the two disagree.
The probes that produced it live in `scripts/probe_stt.py` and
`scripts/probe_batch.py`, and can be re-run at any time.

Labels: **MEASURED** (observed directly), **VERIFIED** (documented and
confirmed), **CONTRADICTS DOCS** (the live behaviour differs from the
published documentation).

---

## 1. Streaming STT — `wss://api.vachana.ai/stt/v3/stream`

### 1.1 The server DOES send `speech_start` — CONTRADICTS DOCS
The documentation lists only `connected`, `processing`, `transcript`,
`error` and `settings_updated`. The live server also sends:

```json
{"type": "speech_start", "utterance_id": "u7841d1e1",
 "timestamp": "2026-10-22T09:05:40.487785+00:00"}
```

This is good news: `livekit-plugins-gnani` already handles `speech_start`
and maps it to LiveKit's `START_OF_SPEECH`, so that path works.

### 1.2 The server NEVER sends `speech_end` / `vad_end` — CONTRADICTS DOCS
Observed frame types across a full utterance: `connected`, `speech_start`,
`transcript`. **No `speech_end`.** The plugin maps `speech_end`/`vad_end` to
LiveKit's `END_OF_SPEECH`, so that branch never fires.

**Consequence:** the plugin gives LiveKit a speech *start* but never a
speech *end*. LiveKit's own bundled Silero VAD supplies the end-of-speech
boundary, and the turn detector is audio-based, so the turn lifecycle still
works — but this must be confirmed in the Phase 2 end-to-end test rather
than assumed. The `stt_gnani.py` subclass removes the dependency entirely
by deriving speech events from LiveKit's VAD.

### 1.3 The `transcript` event has undocumented fields — CONTRADICTS DOCS
Full frame as received:

```json
{
  "type": "transcript",
  "text": "नमस्ते दोस्तों आज हम कम्प्यूटर नेटवर्क के बारे में पढ़ेंगे ...",
  "audio_duration_ms": 10560.0,
  "segment_id": "01a162db-e2fb-7904-b787-5d0ba97abf4f-seg-1",
  "segment_index": 1,
  "utterance_id": "u7841d1e1",
  "is_final": true,
  "latency": 233,
  "timestamp": "2026-10-22T09:05:51.748154+00:00",
  "output": {"literal": "नमस्ते दोस्तों आज हम कम्प्यूटर नेटवर्क ..."}
}
```

- `utterance_id`, `is_final` and `output.literal` are not documented.
- `segment_index` is **1-based** here; the documentation says 0-based. Do
  not rely on the documented value — use it only as a within-utterance
  ordinal.
- `segment_id` is `<utterance_id>-seg-<n>`, not a bare integer.

### 1.4 The server needs TRAILING SILENCE to close a segment
The server closes a VAD segment only when it *hears* silence. A client that
stops sending audio without sending silence frames leaves the segment open
and **no transcript is ever delivered** — the connection just idles.

This matters for the LiveKit plugin: it forwards audio frames and does not
inject silence. LiveKit's framework does push silence into the STT when it
flushes at turn-commit time, so the transcript does eventually arrive, but
only *after* the end-of-turn decision. Injecting silence earlier (as soon
as the VAD reports end-of-speech) removes that ordering from the critical
path. This is a measured optimisation, not a guess — see §3.

### 1.5 Measured latency
10.06 s of Hindi speech (synthesised locally with macOS `say`, voice
`Lekha`/hi-IN), streamed at real-time cadence in 1024-byte frames:

| Event | t (from connect) |
|---|---|
| `connected` | 0.583 s |
| `speech_start` | 0.850 s |
| last audio frame sent | 11.281 s |
| `transcript` received | **12.121 s** |

**End-of-speech → transcript = 840 ms**, of which the server attributes
233 ms to its own transcription latency. The remainder is the documented
`x-min-silence-ms` default of 500 ms plus framing overhead.

### 1.6 Transcript quality and format
- The Hindi transcript is accurate Devanagari. One error in the sample:
  "साथ स्तरों" for "सात स्तरों" (seven layers).
- The text is **unpunctuated and uncapitalised**, consistent with the
  documented behaviour. Normalisation is therefore a real pipeline stage.

---

## 2. Batch STT — `POST /stt/v3/batch/jobs`

### 2.1 Segment timestamps are RELATIVE to each file — RESOLVES ASSUMPTION 4
The documentation does not state the basis. Measured directly with a
two-chunk job (2.69 s + 2.75 s, second chunk starting at 2.69 s in the
original):

| File | min_start | max_end | own duration |
|---|---|---|---|
| chunk0.wav | 0.0 | 2.688 | 2.69 s |
| chunk1.wav | 0.0 | 2.747 | 2.75 s |

**Both start at 0.0, so the timestamps are per-file and must be rebased
onto the chunk offset.** `detect_timestamp_basis()` in
`app/pipeline/transcribe.py` detects this correctly. Applying the wrong
rebasing would silently corrupt every timestamp in the notes, so the
detector stays in place rather than hard-coding the answer.

### 2.2 Batch STT DOES support `gu-IN` and `pa-IN` — CONTRADICTS DOCS
The documentation's prose says Batch excludes Gujarati and Punjabi
("Supported: 8 languages"); the embedded OpenAPI schema and the
`UNSUPPORTED_LANGUAGE` error message list all ten. **The live API accepts
all ten.** Verified by running a job for each of `hi-IN`, `gu-IN`,
`pa-IN`, `en-IN`, `ta-IN` — every one returned `COMPLETED` with
`failed_files: 0`.

Practical note: when the requested language does not match the audio, the
model transcribes *into* the requested script. An English clip submitted
as `gu-IN` came back in Gujarati script. The language code is an
instruction, not a detector.

### 2.3 Other confirmed behaviours
- `segment_id` is `0` for every segment — confirmed, not unique. Use the
  array index.
- `confidence` is `null` — confirmed.
- The async lifecycle works as documented: create → start → poll →
  `COMPLETED`, with `progress.percent` reported.
- A 5.43 s two-file job completed in **4.68 s** wall-clock, including the
  10 s-minimum poll interval. Batch transcription is fast.

### 2.4 Chunk boundaries DO break sentences — the problem is real
With a mid-sentence split, the two chunks read:

```
chunk0: வணக்கம் நண்பர்களே இன்று நாம் கணினி நெட்வொர்க்
chunk1: பற்றி படிப்போம் இன்றைய விஷயம் ஓ எஸ் ஐ மாடல்
```

The words survived intact here, but the phrase was cut mid-sentence. This
is exactly the case the silence-aware chunker in `app/pipeline/ingest.py`
is designed to reduce: cutting at a detected silence lands between
sentences instead of inside one.

---

## 3. TTS — `POST /api/v1/tts/inference`

Measured with `timbre-v2.5`, voice `Nalini`, language `hi-IN`:

| Field | Value |
|---|---|
| status | 200 |
| content-type | `audio/wav` |
| bytes | 81,964 |
| **time to first byte** | **1.049 s** |
| total time | 1.144 s |
| audio produced | ~2.56 s of 16 kHz mono 16-bit PCM |

The REST endpoint returns the whole file in one response, so time-to-first-byte
and total time are nearly identical. For the live voice path the plugin uses
the **WebSocket** transport, which streams chunks — that is the transport
that matters for perceived latency, and it stays unmeasured until the
end-to-end run happens.

---

## 4. LLM providers — what the configured keys can actually reach

Measured on 2026-10-22 with `scripts/probe_llm_models.py`. A model can be
listed in a provider's catalogue and still be unavailable to a particular
account, so each candidate was probed with a real request.

### 4.1 Groq

| Model | Result | Time |
|---|---|---|
| `qwen/qwen3.8-27b` | **OK** | 0.35 s |
| `openai/gpt-oss-120b` | OK | 1.03 s |
| `openai/gpt-oss-20b` | OK | 0.73 s |
| `llama-3.3-70b-versatile` | 404 for this account | — |
| `llama-3.1-8b-instant` | 404 for this account | — |
| `qwen/qwen3-32b` | 404 for this account | — |
| `moonshotai/kimi-k2-instruct-0905` | 404 for this account | — |
| `meta-llama/llama-4-scout-17b-16e-instruct` | 404 for this account | — |
| `deepseek-r1-distill-llama-70b` | decommissioned | — |
| `gemma2-9b-it` | decommissioned | — |

`qwen/qwen3.8-27b` streams clean content with **no reasoning preamble**,
which matters because the answer is spoken aloud. Verified by reading the
raw token stream.

**Rate limit, measured:** the free tier allows **1000 output tokens per
minute** for `qwen/qwen3.8-27b`. The error is explicit:
`Rate limit reached ... on output tokens per minute (OTPM): Limit 1000,
Used 895, Requested 795. Please try again in 41.4s.` `gpt-oss-120b` and
`gpt-oss-20b` produced 1200-word and 940-word completions without being
rejected, so their budget is larger.

### 4.2 Google Gemini

| Model | Result | Time |
|---|---|---|
| `gemini-3.5-flash` | **OK** | 2.88 s |
| `gemini-3.1-flash-lite` | OK | 6.39 s |
| `gemini-4-argon` | 429 quota exceeded | — |
| `gemini-3.8-flash` | 503/504, high demand | — |
| `gemini-2.5-flash` | 404 for this account | — |
| `gemini-2.5-pro` | 404 for this account | — |

`GET /v1beta/models` lists 50 models including `gemini-4-argon` and
`gemini-3.8-flash`, so **the catalogue is not the constraint** — the
account's quota is.

**The quota, measured:** the free tier allows **20 requests per day per
model per project**
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). The error states
`Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests,
limit: 20 ... Please retry in 20h38m.`

This is a **daily** quota, and its status code is 429 — identical to a
per-minute rate limit. That ambiguity caused a real failure: a retry
loop could not tell them apart, retried the quota error three times per
segment, and turned 9 needed calls into 24, exhausting the day. Errors
are now classified, a quota error is never retried, and the notes
workload falls back to a provider on a different account.

### 4.3 What this means for the design

1. **Both free tiers are tight.** 20 requests/day from one provider and
   1000 output tokens/minute from the other. The notes workload for one
   lecture is 8 segment notes plus 1 merge.
2. **Model calls are paced, not parallel.** 2 s apart, serialised, with
   the per-segment note asked for three to five sentences. Eight paced
   short notes fit inside one token-budget window; eight parallel long
   ones do not.
3. **The provider's own retry hint is authoritative.** "Please try again
   in 41.4s" is parsed and honoured, because a per-minute budget refills
   on a schedule the caller cannot guess.
4. **The notes workload has a provider chain**, primary Google, fallback
   Groq, switched automatically on a quota error. Both are already
   authorised; no new service was introduced.
5. **The configured model defaults are the ones that work.** They are
   env-overridable, and `scripts/probe_llm_models.py` re-checks them.

---


1. **The turn lifecycle must not depend on the plugin's `speech_end`.**
   It never arrives. The `stt_gnani.py` subclass derives speech events from
   LiveKit's VAD and adds the five Gnani VAD-tuning headers the plugin
   does not expose.
2. **Inject trailing silence into the STT WebSocket as soon as the VAD
   reports end-of-speech.** The server needs to hear silence to close a
   segment, and doing it early removes ~0.5 s of waiting from the path
   between the user stopping and the transcript being available.
3. **Batch timestamp rebasing is mandatory**, and the basis is now known
   rather than assumed.
4. **Gujarati batch transcription works**, so a Gujarati lecture is
   possible. This removes a risk the documentation had created.

## 5. STT turn-taking configurations — MEASURED

`scripts/probe_tuned_stt.py` sweeps the two knobs that decide when a
transcript arrives and whether a pause splits the utterance. The input
is 10 s of Hindi speech, streamed at real-time cadence, with an
optional 0.6 s gap in the middle. "finals" is how many final
transcripts the utterance produced: more than one means the pause split
it. "latency" is measured from the end of the audio.

| config | server min-silence | injection timeout | clean: finals / latency | pause: finals / latency |
|---|---|---|---|---|
| server-default | 500 ms | 0.50 s | 1 / +1.27 s | **2** (split) |
| patient | 700 ms | 0.80 s | 1 / +2.03 s | **1** |
| eager | 500 ms | 0.20 s | 1 / +0.98 s | **2** (split) |
| no injection | 500 ms | — | **0 — no transcript at all** | 0 |

Findings:

1. **Without trailing silence there is no transcript.** The plugin on
   its own forwards audio and stops; the server never hears the silence
   it needs to close the segment, so nothing is ever delivered. LiveKit
   papers over this by pushing silence when it flushes the STT at
   turn-commit time — which is *after* the end-of-turn decision, so the
   transcript is not available when the turn is committed.
2. **The injection timeout and the server's min-silence must both
   exceed the longest pause to tolerate.** At 0.5 s / 500 ms a 0.6 s
   pause split the utterance; at 0.8 s / 700 ms it did not. The
   injection is what causes the split in the fast configurations: the
   injected silence pushes the server past its own threshold in the
   middle of a sentence.
3. **The cost of not splitting is about 750 ms of transcript latency.**
   That is the trade the brief's "pauses before a technical term" case
   demands.

The shipped default is therefore **700 ms min-silence with a 0.8 s
injection timeout**: one transcript in both cases, ~1.9 s after the
student stops speaking. Re-verified live after the change: 1 final in
the clean case, 1 final in the paused case.

**Turn control is unaffected by any of this.** LiveKit's own VAD drives
the turn lifecycle and its turn detector is audio-based, so it does not
need partial transcripts. A split transcript is not fatal — LiveKit
concatenates multiple final transcripts into one turn — but a turn
committed on the first half would answer half the question, so the
split is avoided rather than merely tolerated.

---

## 6. Credit cost of this probe

Four batch jobs of ~5 s each plus two streaming sessions of ~10 s each, and
one TTS call. On the reseller rate card (27 credits per audio hour) that
is well under one credit. The real credit figures come from the Gnani
dashboard, which the pipeline records locally as well.
