# Data policy and operator responsibilities

This note is the human half of the security work. Code can enforce access,
retention and erasure; it cannot decide whether a recording was made with
consent or whether a lecture may be published. An operator must accept those
responsibilities before exposing the app beyond a local demo.

## Classification and purpose

| Data | Class | Why it is held |
|---|---|---|
| Google `sub`, email, name, picture | PII | Sign-in and account identification. Only the opaque local user id is written to lectures and LiveKit tokens |
| Uploaded recording | Sensitive (voice, possibly identifiable people) | The input to transcription; deleted from the temp area as soon as the pipeline settles |
| Transcript, segments, notes, flashcards | Sensitive (derived from the recording) | The study material the product exists to provide; scoped to the owner in SQL |
| Content-addressed audio cache | Sensitive (same audio as the upload) | Makes re-uploads free; pruned by TTL and on account erasure when unreferenced |
| Credit ledger | Operational | Daily accounting; no owner or lecture reference |
| Telemetry | None collected | No analytics, no third-party tracking, no PII in logs |

## Consent and content

- The person uploading a recording is responsible for having the permission
  needed to process it: the lecturer for copyright, and every identifiable
  speaker for the use of their voice and image.
- Do not record or upload lectures where students are identifiable without
  their consent and a lawful basis. A classroom recording is not automatically
  safe to publish to a class group.
- The bundled demo lecture is a 2007 Internet Archive recording under
  **CC BY-NC-ND 4.0**. It belongs in a demo, not in a public deployment's
  content set. Remove it before using the app for real courses.
- Third-party processors are limited to the providers named in
  `docs/SECURITY.md` (Gnani for speech, Google Gemini and Groq for notes and
  retrieval, LiveKit for the voice session). Sending a recording to them is
  sharing it; a real deployment needs the appropriate data-processing terms.

## Rights and retention

- `GET /api/account/export` gives a signed-in student everything tied to
  their account; `DELETE /api/account` erases it, including the FTS index,
  per-lecture Markdown and unreferenced cache directories.
- The audio cache has a bounded TTL (`CACHE_TTL_DAYS`, default 30). Nothing
  in `data/` is committed to git.
- Keep database backups for the same bounded window as the cache. A backup
  older than the retention window can still hold data a student erased, so
  prune backups on the same schedule.

## Pre-deployment checklist

- [ ] Google sign-in configured and the demo's open mode **not** enabled.
- [ ] `FRONTEND_URL` set to the HTTPS origin; session cookies carry `Secure`.
- [ ] Demo lecture removed; only recordings with documented consent remain.
- [ ] Processor agreements in place with the providers listed above.
- [ ] Backup and cache retention aligned with the stated window.
- [ ] Owner notified of this policy and of the export/erasure endpoints.

This is an engineering checklist, not legal advice.
