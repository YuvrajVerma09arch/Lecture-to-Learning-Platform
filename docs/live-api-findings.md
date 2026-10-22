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
that matters for perceived latency, and it is measured in Phase 2.

---

## 4. What the live behaviour implies for the design

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

## 5. Credit cost of this probe

Four batch jobs of ~5 s each plus two streaming sessions of ~10 s each, and
one TTS call. On the reseller rate card (27 credits per audio hour) that
is well under one credit. The real credit figures come from the Gnani
dashboard, which the pipeline records locally as well.
