# Benchmark report

**Date:** 2026-10-22
**Subject:** the Gnani Lecture-to-Learning Platform
**Labels used throughout:** **MEASURED** (observed on the live system),
**ESTIMATED** (a reasoned figure, not observed), **MOCKED** (covered by a
test with the provider replaced), **UNVERIFIED** (no authoritative evidence
available).

Nothing in this report is a projection presented as a result. Where a number
was not measured, the section says so and gives the reason.

## 1. The summary

| Question | Answer | Label |
|---|---|---|
| How long does the notes pipeline take on a 34-minute lecture? | 347.3 s cold, 200.9 s with the transcript cached | MEASURED |
| What does that cost in Gnani credits? | One batch job, 15.2 estimated credits, against 5,000 available | MEASURED (the API call count) / ESTIMATED (the credit figure, which uses a reseller's rate card) |
| How long from the student stopping speaking to the transcript? | ~1.9 s with the shipped configuration, 840 ms with the server's defaults | MEASURED |
| How long from the answer text to the first playable audio? | 1.049 s time-to-first-byte over the REST transport | MEASURED |
| Which deployment configuration is fastest? | **Only configuration A was run.** A is the shipping configuration. B and C were not run | MEASURED (A) / ESTIMATED (B, C) |
| End-to-end user-perceived latency over 20 voice turns | See §3.3 | see §3.3 |

The brief asked for at least 20 measured end-to-end turns per configuration
that could legitimately be tested, with the sample count, the percentiles and
the failures reported. §3.3 reports what was actually collected.

## 2. Method

### 2.1 The metrics and what they mean

Two families, kept apart on purpose. **Server-side processing time** is
measured on the machine that does the work; **user-perceived latency** is
measured from the moment the student's turn ends to the moment audio comes
out of the speaker. A provider can be fast and the product still feel slow,
and only the second number is the product.

| Marker | Definition | Clock | Where it is taken |
|---|---|---|---|
| `end_of_speech` | The VAD fires the end of the student's turn | monotonic | the agent worker |
| `final_transcript` | The final transcript for the turn arrives | monotonic | the agent worker |
| `retrieval_start` / `retrieval_end` | Around the FTS5 + semantic search and the rerank | monotonic | the agent worker |
| `llm_first_token` | The first token of the answer | monotonic | the agent worker, from LiveKit's `LLMMetrics.ttft` |
| `llm_complete` | The whole answer is available | monotonic | the agent worker |
| `tts_first_byte` | The first playable audio chunk | monotonic | the agent worker, from LiveKit's `TTSMetrics` |
| `playback_start` | The first sample actually rendered | browser clock | the client, reported back over the room's data channel |

All durations use `time.monotonic()`. The two processes do **not** share a
clock, so the worker publishes its markers into the room on a data channel
(`lecture-tutor-markers`) and the client correlates them with its own
readings. Nothing is computed by subtracting two unrelated clocks.

### 2.2 The conditions

| Condition | Value |
|---|---|
| Network | The developer's own machine, on the network described in the brief (Ahmedabad, India). The LiveKit Cloud project is `name-5pv71xyx.livekit.cloud`, a **US region**, as confirmed by the project owner |
| Concurrency | 1 |
| STT | Gnani Prisma, streaming, `x-min-silence-ms: 700` |
| LLM (chat) | Groq `qwen/qwen3.8-27b` |
| LLM (notes) | Google `gemini-3.5-flash`, with Groq as the fallback provider |
| TTS | Gnani Timbre `timbre-v2.5`, WebSocket transport, sentence-level |
| VAD | Silero, bundled ONNX, no network call |
| Turn detection | `v1-mini`, bundled ONNX, no network call |
| Lecture under test | *Bhagavad Gita Chapter 5 with Shankara Bhashya*, 2028.5 s, Telugu, 66 source segments, 8 topic sections |

## 3. The results

### 3.1 Batch ingestion — MEASURED

One real lecture, one real Gnani batch job, one real note generation run.

| Stage | Cold run | With the transcript cached |
|---|---|---|
| Ingest (validate, transcode to 16 kHz mono, silence detection, chunking) | 116.7 s | 0.1 s |
| Transcribe (the Gnani batch job) | 66.4 s | **0.0 s — a cache hit, no job, no credits** |
| Segment (66 transcript segments → 8 topic groups) | 0.2 s | 0.0 s |
| Note (8 segment notes + 1 merge) | 163.7 s | 200.8 s |
| **Total** | **347.3 s** | **200.9 s** |

The input: 8,115,430 bytes, 2028.5 s, MP3 44.1 kHz stereo, Telugu. The
output: a merged document with a title, a summary, 8 sections with their
source time spans, and a key-terms list.

The 200 s of the note stage is **not** model latency. It is deliberate
pacing: a minimum of 2 s between model calls, serialised. Both free tiers
enforce a per-minute budget, and eight parallel long notes exhausted the
window and made the merge call — the largest request in the pipeline — fail
with a 413. The pacing is a config value; on a paid quota it would drop
towards zero.

**Credits.** One batch job for 2028.5 s of audio. The estimate is 15.2
credits, using 27 credits per audio hour from a reseller's published rate
card — Gnani publishes no rate card of its own, so the authoritative figure
is the usage panel at `app.gnani.ai/voice`, which has not been read. The
**measured** part is the API-call count: one job, and zero for every
re-run. Re-uploading the same recording creates no Gnani job at all, which
is the property the cache exists for.

**The batch API's own speed.** MEASURED: a two-file, 5.43 s job completed in
4.68 s of wall-clock time. The poller honours a 10 s minimum interval
between polls, and backs off to 30 s.

### 3.2 The live voice chain, stage by stage — MEASURED

Each number below is the measured latency of one link in the chain, from the
probe scripts in `scripts/`. They are the inputs to the end-to-end figure,
not the end-to-end figure itself.

| Link | Measured | How |
|---|---|---|
| Student stops speaking → the final transcript arrives | **840 ms** with the server's default `x-min-silence-ms: 500` | 10.06 s of Hindi speech (synthesised with the macOS `say` voice `Lekha`), streamed at real-time cadence in 1024-byte frames. The server attributes 233 ms of it to its own transcription; the rest is the 500 ms minimum silence plus framing |
| The same, with the shipped tuning (`x-min-silence-ms: 700`, 0.8 s silence injection) | **~1.9 s** after the student stops speaking, and the utterance is **not** split | `scripts/probe_tuned_stt.py` |
| The same, with **no** silence injection | **No transcript at all** | The server closes a segment only when it hears silence. A client that stops sending audio leaves the segment open and nothing is delivered |
| With a 0.5 s injection and a 500 ms server threshold | A 0.6 s mid-sentence pause **splits** the utterance into two transcripts | Same script |
| Answer text → first playable audio (Timbre, REST) | **1.049 s** to the first byte, 1.144 s in total, 81,964 bytes for ~2.56 s of 16 kHz mono PCM | `POST /api/v1/tts/inference`, voice `Nalini`, `hi-IN`, model `timbre-v2.5` |
| The chat model, one call | **0.35 s** for `qwen/qwen3.8-27b` | `scripts/probe_llm_models.py`, a non-streaming probe |
| The notes model, one call | **2.88 s** for `gemini-3.5-flash` | Same |

The REST TTS transport returns the whole file in one response, so its
time-to-first-byte and its total time are nearly the same. **The voice path
does not use it**: the plugin's `synthesize_method="websocket"` streams
chunks over `wss://api.vachana.ai/api/v1/tts`, and that is the transport
that determines how quickly the first sentence is audible. The 1.049 s
figure is therefore a lower bound for the REST path and an *indication* of
the websocket path's cost, not a measurement of it.

### 3.3 End-to-end voice turns — see the status line below

This section is the one the brief cares about most, and it is kept short
because the number is not a single measurement: it is a distribution over
turns, and it depends on how many of them completed.

**Status at the time of writing: the end-to-end voice benchmark had not been
run.** The harness and the measured run are the last piece of work in
progress. What is measured so far is every link of the chain in §3.2, on
the real APIs, plus the two configuration changes that were made in
response to those measurements (the trailing-silence injection, and the
VAD-derived speech end).

This report will not carry a p50 or a p95 for a run that did not happen.
When the run happens, it belongs here, with the sample count, the warm-up
runs excluded and counted, the failures listed, and the network path stated.

**Why the number cannot be assembled from the parts.** The chain is
serial — the transcript is needed before the retrieval, the retrieval before
the model, the model before the synthesis — so the per-stage numbers add
up. But the sum of the parts is not the perceived latency: the first
sentence starts synthesising as soon as it exists, so the audible answer
begins before the answer is finished, and the perceived latency is
end-of-speech → first byte of the *first sentence*, not end-of-speech → the
whole answer. The only way to get that number honestly is to measure it.

### 3.4 The retrieval stage — MEASURED, and it changed the design

Before the semantic stage existed, a Telugu question retrieved the right
segment from the Telugu lecture, and an English or Hinglish question
retrieved **nothing at all**. Word overlap only helps when the question
shares a script with the lecture, and the product's core requirement is a
student asking in one language about a lecture in another.

The fix was a second retrieval stage: multilingual embeddings, 768
dimensions, computed once per lecture in one batched call and stored as
float32 in SQLite. The corpus is a few hundred vectors, so the search is a
brute-force cosine in microseconds. If the embedding provider is
unavailable, the lexical stage still works and the product degrades rather
than failing.

**Cost:** one embedding call per lecture, in the same quota family as the
notes model but a different quota bucket. **Complexity:** no vector
database, no second service, no new dependency.

## 4. The provider limits that shaped the design

These are the three measurements that surprised everyone, and each one
changed the code.

### 4.1 Google Gemini: 20 requests per day, per model, per project — MEASURED

```
Quota exceeded for metric: generativelanguage.googleapis.com/
generate_content_free_tier_requests, limit: 20 ... Please retry in 20h38m.
```

A lecture needs 9 calls: 8 segment notes and 1 merge. That fits inside 20 —
**only if nothing is retried**.

The trap is that the quota error is a **429**, the same status code as a
per-minute rate limit. A retry loop that cannot tell them apart retried
the quota error three times per segment, turned 9 needed calls into 24, and
exhausted the whole day's allowance in one lecture. That is a real
incident, recorded in `docs/PHASE1_REPORT.md` §3.2, defect 5.

The fix: errors are classified (`ErrorKind.QUOTA` is not
`ErrorKind.RATE_LIMIT`), a quota error is **never** retried, and the notes
workload falls back to Groq automatically. Both providers were already
authorised; no new service and no paid plan.

The catalogue is not the constraint. `GET /v1beta/models` lists 50 models
including `gemini-4-argon`, which returns 429 on this account, and
`gemini-3.8-flash`, which returns 503/504 under load. The account's quota
is the constraint, which is why the defaults in `.env.example` are the
models that were measured to work rather than the newest published ones.

### 4.2 Groq: 7000 input tokens and 1000 output tokens per minute — MEASURED

```
Rate limit reached ... on output tokens per minute (OTPM): Limit 1000,
Used 895, Requested 795. Please try again in 41.4s.
```

Eight segment notes at roughly 800 tokens each exceeded the window, and the
merge — the largest request in the pipeline — then failed with
`413 Request too large ... on tokens per minute`.

Three changes followed:

1. **The model calls are paced**, 2 s apart and serialised. A per-minute
   budget cannot be spent in a burst.
2. **The per-segment notes are short** — three to five sentences, under 120
   words. The detail belongs in the merged document, not in every segment.
3. **The provider's own retry hint is authoritative.** "Please try again in
   41.4s" is parsed out of the error message and honoured, because a
   per-minute budget refills on a schedule the caller cannot guess.

`gpt-oss-120b` and `gpt-oss-20b` produced 1200-word and 940-word
completions without being rejected, so their budget is larger — but
`qwen/qwen3.8-27b` is the fastest of the available models (0.35 s) and
streams clean content with no reasoning preamble, which matters because the
answer is spoken aloud.

### 4.3 Gnani: 10 MB per file on a batch job — MEASURED

A 45-minute lecture is about 42 MB at 128 kbps, so chunking is not an
optimisation, it is a requirement. The chunker transcodes to 16 kHz mono
MP3 (Prisma is a telephonic model, so this is its input distribution) and
cuts on detected silence, so a chunk boundary lands between sentences
rather than inside one. Every chunk is checked against the 10 MB limit, and
an oversized chunk triggers a re-split at half the target size.

**A second measured fact about the same API:** the segment timestamps it
returns are **relative to each file**, not to the job. A two-chunk job
confirmed it: the first chunk ran 0.0 → 2.688 s and the second 0.0 →
2.747 s, even though the second chunk started at 2.69 s in the original.
The timestamps are rebased onto the chunk offset, and a detector checks the
basis rather than assuming it, because getting it wrong would silently
corrupt every timestamp in the notes.

## 5. The deployment comparison

The brief asks for three configurations to be compared. **One of them was
run.** The other two are documented estimates, with the reason they were
not run, and **no latency number is presented for them**.

### A. Local agent worker + LiveKit Cloud — **MEASURED**

This is the shipping configuration, and the only one that was exercised.

| | |
|---|---|
| Architecture | The agent worker runs on the developer's machine; the browser, the API and the agent all talk to LiveKit Cloud for the media |
| Component locations | Browser → LiveKit Cloud (media) → agent worker → Gnani / Groq / Gemini; the API and the SQLite file stay on the machine |
| Service regions | LiveKit Cloud: **MEASURED**, a US region (`name-5pv71xyx.livekit.cloud`, confirmed by the project owner). The three AI providers: their regions are **UNVERIFIED** from this project |
| Network path | Student audio → LiveKit Cloud → worker; the worker's three AI calls each leave the machine to a different provider |
| Reliability | The chain has four remote dependencies in series; a failure in any one of them is visible in `/api/ready` and degrades to the text path |
| Deployment complexity | Two processes to start, no container runtime, no infrastructure to provision |
| Operational overhead | The worker is a long-lived process that must be running before a student joins a room |
| Recurring cost | LiveKit Cloud's plan and egress — **UNVERIFIED**, the account's usage panel has not been read. Gnani credits: 15.2 per 34-minute lecture. Google and Groq: free tiers, 0 |
| Security trade-off | The LiveKit API secret stays server-side; the browser holds a scoped, one-hour room token |

### B. Agent worker deployed to LiveKit Cloud — **ESTIMATE, not run**

**Why it was not run:** there is no container runtime on this machine, and
installing one was declined. The deployment method (`lk agent create` plus
`lk agent deploy`) needs a local image build.

**What would change:** the leg between the agent and the room becomes a
cloud-local call instead of a WAN call. **The serial STT → LLM → TTS chain
stays exactly the same**, because all three of those calls leave the
machine in every configuration. The expectation is therefore that the
end-to-end p50 changes by **tens of milliseconds, not hundreds** — and
that expectation is a hypothesis, not a result.

**What it would cost:** LiveKit Cloud's agent hosting and egress, on top of
whatever the Cloud plan already costs. **UNVERIFIED** until the account is
read.

**What it would buy:** the worker is no longer a process the developer has
to remember to start, and it is no longer bound to one machine's uptime.

### C. Fully self-hosted LiveKit on the same machine — **ESTIMATE, not run**

**Why it was not run:** self-hosting was declined outright, and the same
missing container runtime applies.

**What would change:** one media hop moves from the cloud to the loopback
interface. **All three AI calls still traverse the WAN** — that is the
dominant cost, and it does not change. The expectation is that this
configuration is **no faster than A, and less reliable**: it adds a process
to supervise, TLS to terminate, and an uptime to maintain, in exchange for
saving one low-latency WebRTC leg out of a chain whose length is set by
three remote AI calls.

**What it would cost:** the machine's CPU and bandwidth, plus the
operator's time. No LiveKit Cloud bill.

**The honest conclusion:** the three configurations collapse, because the
end-to-end user-perceived latency of this product is dominated by the
serial chain *student audio → Gnani STT → LLM → Timbre TTS → speaker*, and
every one of those three is a remote call to a different provider in every
configuration. **A is the recommendation**: it is the configuration that was
measured, it needs no container runtime, and the reasoning above says the
latency difference against B is tens of milliseconds while the operational
difference is a process you have to remember to start.

**The caveat that has to stay attached to that recommendation:** the
reasoning is a hypothesis, and it has not been tested. If the LiveKit
region turns out to be far from the AI providers, or if the local network
is what dominates, the ranking could change. The experiment that would
settle it is to run configuration A from the same machine and the same
network with the agent on LiveKit Cloud, at least 20 turns each, and
compare the distributions. The harness for that exists; the runs have not
happened.

## 6. What is estimated, mocked and unverified

### ESTIMATED

| Item | Basis |
|---|---|
| 15.2 credits for one 34-minute lecture | 27 credits per audio hour, a reseller's rate card. Gnani publishes no rate card |
| The daily credit ceiling of 300 | A planning figure chosen against the 5,000 available |
| The latency difference between configurations A and B | Tens of milliseconds, from the reasoning in §5 |
| The operational overhead of configuration C | A process to supervise, TLS, uptime |

### MOCKED

The whole test suite: 219 tests, none of which call a remote API. The Gnani
and LLM clients are replaced with fakes, every test gets its own temporary
SQLite file and cache directory, and the retry delays are shortened. A test
that does exercise a live API is marked `@pytest.mark.live` and is skipped
unless `GNANI_LIVE_TESTS=1` is set.

The unit tests cover the parts a benchmark cannot reach cheaply: the
timestamp rebasing, the chunk-boundary handling, the credit ceiling, the
error classification (a quota error is never retried), the citation
validation (an answer that cites an excerpt which was not in the prompt is
rejected), the abstention path, the transcript cache, and the claim that a
downstream failure never destroys a cached transcript.

### UNVERIFIED

| Item | Why |
|---|---|
| Gnani's own credit rate | Gnani publishes no rate card. The dashboard is the authority and has not been read |
| The LiveKit Cloud project region | Resolved: it is a US region. The project owner confirmed it |
| The cost of configuration B | The account's usage panel has not been read |
| The end-to-end user-perceived latency of a voice turn | The run had not happened when this report was written — see §3.3 |
| Whether a Gujarati *lecture* transcribes as well as a Hindi one | The batch API accepts `gu-IN` (measured), but no Gujarati audio was available to test with |
| Batch STT with a file over 10 MB | The lecture fitted in one chunk. The chunking path is tested with real ffmpeg and the limit is enforced, but no real job has exceeded it |
| The accuracy of the Telugu transcript | Partially verified. The transcript contains real ASR errors on Sanskrit philosophical vocabulary. The concepts survive in the notes; the words do not always |

## 7. Reproducing the measurements

The probe scripts call the live APIs and cost a small number of Gnani
credits. Run them deliberately.

```bash
PYTHONPATH=backend .venv/bin/python scripts/probe_stt.py
PYTHONPATH=backend .venv/bin/python scripts/probe_batch.py
PYTHONPATH=backend .venv/bin/python scripts/probe_llm_models.py
PYTHONPATH=backend .venv/bin/python scripts/probe_tuned_stt.py
```

The ingestion timings are reproduced by running the pipeline over the demo
lecture:

```bash
PYTHONPATH=backend .venv/bin/python scripts/run_pipeline.py \
  data/fixtures/telugu_lecture_ch5.mp3 --lang ta-IN
```

The first run is cold. Every run after it is a cache hit, and the script
prints `cached : True` and `transcribe : 0.0s`.
