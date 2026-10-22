# Demo script — 60 seconds

The demo shows the real product on a real lecture. The one thing it does not
do live is transcribe a recording, and that is stated out loud, in the first
sentence.

**Say this, in the first five seconds:**

> "This lecture was uploaded and transcribed earlier by Gnani Prisma. The
> transcript is cached, so it costs zero credits to show it again. Everything
> you see from here — the notes, the retrieval, the answer, the voice — is
> live."

The brief allows a clearly identified, previously processed lecture, and
requires transparency about the cached preprocessing. The ingestion
receipt — the real job id, the real credit figure, the real content hash —
is shown on screen in the first step, so the claim is checkable rather than
asserted.

## Before the clock starts

Three terminals, all in the project root.

**Terminal 1 — the API**

```bash
cd "/Users/yuvraj/Desktop/Lecture-to-Learning Platform"
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — the front end** (or skip this and let the API serve the built
front end, if `web/dist` exists)

```bash
cd "/Users/yuvraj/Desktop/Lecture-to-Learning Platform/web" && npm run dev
```

**Terminal 3 — the agent worker**

```bash
cd "/Users/yuvraj/Desktop/Lecture-to-Learning Platform"
PYTHONPATH=backend .venv/bin/python -m agent.worker
```

**Check the state once, before the demo:**

```bash
curl -s http://127.0.0.1:8000/api/ready
```

`"status": "ready"` with all five checks `ok` means the database, Gnani,
Google, Groq and LiveKit are all configured. If `gnani` or `livekit` is
`false`, the voice path will not work; run the text-only variant below.

**Open the browser at the demo URL, so there is no typing on the clock:**

```
http://localhost:5173/notes?lecture=lec_54a9c1e582af41d0
```

and the chat screen, in a second tab:

```
http://localhost:5173/chat?lecture=lec_54a9c1e582af41d0
```

**Rehearse the question once** (it costs one cheap Groq call, and it proves
the answer is grounded before a judge sees it):

```bash
curl -s -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"Karmayoga aur Jnana yoga mein kya antar hai?","lecture_id":"lec_54a9c1e582af41d0"}' \
  | python3 -m json.tool
```

Expect `"covered": true` and a `cited_segments` list. If the answer says the
lecture does not cover it, try the fallback question below. **Do not run this
during the 60 seconds** — it is a rehearsal step.

## The demo lecture

| | |
|---|---|
| Title | Bhagavad Gita Chapter 5 with Shankara Bhashya |
| Speaker | Swami Tattvividananda Saraswati, Ramakrishna Math (2007) |
| Source | Internet Archive, `BhagvadGitaChapter5WithShankaraBhashya` |
| Licence | CC BY-NC-ND 4.0, creator `rparimi` |
| Language | Telugu (`te-IN`) |
| Length | 2028.5 s (33.8 min), 8,115,430 bytes, MP3 44.1 kHz stereo |
| Transcript | 66 segments from the Gnani job, kept as the 66 source segments |
| Notes | 8 topic sections plus a summary and a key-terms list |
| Gnani job id | `01a16376-9f1c-7747-bf4d-4b44e7ff3053` |
| Credits | 15.2 estimated (27 credits per audio hour, a reseller's rate card) |

It is a real human lecture, not a synthesised recording, which is what the
challenge's synthetic-data rule asks for.

## The 60 seconds

### 0:00 – 0:12 · The notes

**On screen:** `/notes?lecture=lec_54a9c1e582af41d0`.

- The merged document: the summary, eight topic sections, each with its
  source time span, and the key-terms list.
- Scroll once to the second section. Point at the line
  `*Source: 0:44 – 3:56*`.
- Point at the first section's marker:

  > **Needs the original visual material.** (the teacher referred to a
  > figure that is not in the audio)

**Say:** "The teacher referred to a figure on the board. There is no video in
this pipeline, so the notes say the visual material is missing and point at
the timestamp, instead of inventing the diagram."

This is the grounding rule, on screen, in the first ten seconds.

### 0:12 – 0:22 · The ingestion receipt

**On screen:** a second terminal.

```bash
curl -s http://127.0.0.1:8000/api/lectures/lec_54a9c1e582af41d0 | python3 -m json.tool
```

**Say:** "One Gnani batch job for this lecture, 2028 seconds of audio, 15.2
credits. The transcript is cached by the SHA-256 of the file, so showing it
again costs nothing."

If there is time, one more line, and it is the strongest claim in the demo:

```bash
curl -s http://127.0.0.1:8000/api/ready | python3 -m json.tool
```

### 0:22 – 0:42 · The spoken doubt

**On screen:** the chat tab.

- Press the microphone. The interface shows the listening state.
- **Ask, out loud, in Hinglish:**

  > "Karmayoga aur Jnana yoga mein kya antar hai?"

  (or the rehearsal question that worked)

- The interface shows the recognised speech, then the thinking state, then
  the speaking state.
- Timbre speaks the answer. The transcript of the answer appears alongside
  it, with the cited timestamp.

**Say:** "Prisma transcribed the question, the notes were searched, Groq
answered using only the segments it was given, and Timbre spoke it. The
answer cites the minute of the lecture it came from."

### 0:42 – 0:52 · The refusal

**On screen:** the text box in the chat tab.

Type:

```
Quantum chromodynamics ka Feynman diagram explain karo
```

and press send.

**Say:** "Nothing in the lecture is about that. The system says so instead
of answering from its own knowledge."

The answer comes back as the abstention message, and the model is not even
called — the retrieval gate rejects the question before the LLM is reached.

### 0:52 – 1:00 · The close

**On screen:** back to the notes screen, scroll to the key terms.

**Say:** "Lecture in, notes out, and a tutor that only knows this lecture."

## The text-only variant

If the microphone, the agent worker or the LiveKit connection is not
available, the demo still shows the real pipeline. Replace the 0:22–0:42
block with the text box in the chat tab, which uses the same retrieval and
the same model:

```bash
curl -s -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"Karmayoga aur Jnana yoga mein kya antar hai?","lecture_id":"lec_54a9c1e582af41d0"}' \
  | python3 -m json.tool
```

Say that the voice path is the same answer spoken by Timbre. Do not pretend
the voice path ran if it did not.

## Why the upload is not in the 60 seconds

Re-uploading the same recording **is** free of Gnani credits — the transcript
cache is keyed by the SHA-256 of the file — but it is not free of time, and
it is not in the critical path:

- The API accepts the upload immediately and runs the pipeline in the
  background, so the request returns in under a second.
- The transcript is a cache hit, so no Gnani job is created.
- The segmentation, the embedding index and the note generation still run:
  the note stage is about 200 s, almost all of it deliberate pacing between
  model calls, because both free tiers are tight.

So a fresh upload inside the demo would leave the notes screen empty for
three minutes. The brief's rule applies: the demo path must not depend on a
fresh long-running job finishing in 60 seconds.

To show the upload as a live step **outside** the 60 seconds, in the
rehearsal, run:

```bash
curl -s -X POST http://127.0.0.1:8000/api/lectures/upload \
  -F "file=@data/fixtures/telugu_lecture_ch5.mp3" \
  -F "title=Bhagavad Gita Chapter 5 with Shankara Bhashya" \
  -F "language_code=te-IN" | python3 -m json.tool
```

and then:

```bash
curl -s http://127.0.0.1:8000/api/lectures | python3 -m json.tool
```

The new lecture's `stages.transcribe` reads `cache hit, no transcription
needed`, and no new row appears in `credit_usage`. Note that the upload
creates a *new* lecture row for the same cached transcript, and that the
note stage re-runs in the background for that new row; the original lecture
id, `lec_54a9c1e582af41d0`, is untouched.

## Resetting or reseeding the demo state

The demo state is the rows for one lecture id. Everything below touches only
that lecture, and the transcript row is left alone — deleting it would mean
paying for the Gnani job again.

**Back up first** (one file, one command):

```bash
cp data/app.sqlite3 "data/app.sqlite3.backup.$(date +%Y%m%d-%H%M%S)"
```

**Reseed the notes for the demo lecture** — the transcript is reused, so
there is no Gnani call and no credit cost, but the note stage takes about
200 s:

```bash
PYTHONPATH=backend .venv/bin/python scripts/run_pipeline.py \
  data/fixtures/telugu_lecture_ch5.mp3 --lang ta-IN \
  --title "Bhagavad Gita Chapter 5 with Shankara Bhashya"
```

**Reset the derived rows for the demo lecture only** — the notes, the
segments, the topic groupings, the flashcards and the stage checkpoints.
The transcript survives, so the next pipeline run is still a cache hit:

```bash
sqlite3 data/app.sqlite3 <<'SQL'
PRAGMA foreign_keys=ON;
BEGIN;
DELETE FROM flashcard_reviews WHERE flashcard_id IN
  (SELECT id FROM flashcards WHERE lecture_id='lec_54a9c1e582af41d0');
DELETE FROM flashcards   WHERE lecture_id='lec_54a9c1e582af41d0';
DELETE FROM notes        WHERE lecture_id='lec_54a9c1e582af41d0';
DELETE FROM note_topics  WHERE lecture_id='lec_54a9c1e582af41d0';
DELETE FROM segments     WHERE lecture_id='lec_54a9c1e582af41d0';
DELETE FROM stage_state  WHERE lecture_id='lec_54a9c1e582af41d0';
UPDATE lectures SET status='uploaded', failed_stage=NULL, failure_reason=NULL
  WHERE id='lec_54a9c1e582af41d0';
COMMIT;
SQL
```

The `segments_fts` index and the embedding rows are kept in step by triggers
on `segments`, so the deletes above leave the index consistent.

**What not to do:** do not delete the row in `transcripts`. It is the
expensive artefact — it cost a Gnani job and 15.2 credits — and every
downstream stage can be rebuilt from it for free.

**To remove the duplicate lecture rows** created by earlier re-uploads, keep
the one the demo uses and delete the other by id:

```bash
sqlite3 data/app.sqlite3 "DELETE FROM lectures WHERE id='lec_75902e5f9bb54221';"
```

The `ON DELETE CASCADE` constraints remove that lecture's segments, topics,
notes and flashcards with it. The transcript row is not touched.

## If something goes wrong during the demo

| Symptom | What to do |
|---|---|
| The notes screen is empty | The lecture id in the URL is wrong. It is `lec_54a9c1e582af41d0` |
| The chat says the lecture does not cover the question | Use the rehearsal question that worked. The retrieval index may not have been built for this lecture |
| The microphone does not work | Switch to the text-only variant, and say so |
| The agent does not answer | Check terminal 3. The worker must be running before the student joins the room |
| The API returns 503 | A provider key is missing. `GET /api/ready` says which one |
| The demo machine has no network | Nothing in the demo works. This is a network-bound product by design: the STT, the LLM and the TTS are all remote calls |
