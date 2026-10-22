# Engineering Brief: Gnani AI Lecture-to-Learning Platform

## Role and operating principles

Act as a senior full-stack engineer, voice-AI engineer, and performance engineer. Build a production-quality, mobile-first demo for a single developer entering Gnani AI's Great Indian AI Internship Challenge.

Read this entire brief before touching the repository, running commands that change state, installing packages, or writing code.

Your work must be evidence-driven. Verify APIs, package versions, model capabilities, latency, deployment requirements, and language support rather than relying on memory or assumptions.

**Your first deliverable is a reconnaissance report and implementation plan, not code. Stop after presenting it and wait for my explicit approval.**

Optimize for three things, in this order:

1. Correctness and a reliable end-to-end learning experience.
2. Low, measured latency, especially for voice conversations.
3. A polished, credible demo that can be understood in 60 seconds.

Do not sacrifice the first two for visual polish or unnecessary features.

---

## 1. Non-negotiable engineering safeguards

### Repository and environment safety

- Inspect the existing repository, Git status, branches, project structure, dependencies, runtime versions, and current implementation before proposing changes.
- Work on a new Git branch. If there is no repository, use a new project directory.
- Never modify files outside the project directory.
- Never use `sudo`, install packages globally, or modify shell profiles, system settings, or global configuration.
- Use the existing project environment where appropriate. Otherwise, create an isolated virtual environment or container and pin dependency versions.
- Do not delete, overwrite, reset, clean, or force-push existing work. Do not discard uncommitted changes.
- Do not modify existing `.env` files, dotfiles, or configuration files without my explicit approval. Add new configuration files only when needed and keep their scope isolated.
- Add a `.env.example` containing variable names and safe placeholder values, never actual credentials.
- Read secrets from environment variables or the project's existing secret-management mechanism. Never ask me to paste secret values into the conversation, terminal logs, reports, or source files.
- Do not create paid resources, start billable cloud services, incur new API costs, or subscribe to services without approval.
- Ask before destructive operations, changes outside the agreed scope, data migrations that can lose data, or modifications to existing configuration.
- Make small, reviewable commits only after I approve the plan. Verify the relevant tests and application startup before each commit. Never commit broken or knowingly unverified work.
- Preserve existing functionality. Do not replace an existing working implementation merely because a different architecture seems more attractive.

If the working tree contains existing user changes, preserve them. Record the initial Git state and distinguish your changes from pre-existing work.

### Documentation and verification

Do not trust remembered API signatures.

Inspect the documentation and the installed source code corresponding to the exact package versions used.

Priority references:

- Gnani documentation: `https://docs.gnani.ai`
- `https://docs.gnani.ai/api/STT/speech-to-text.md`
- `https://docs.gnani.ai/api/introduction/quick-start`
- Current documentation and source for `livekit-agents`
- Current documentation and source for `livekit-plugins-gnani`
- Official LiveKit documentation for VAD, turn detection, interruptions, preemptive generation, and deployment

Verify actual package names, installation extras, Python compatibility, method signatures, callback formats, streaming semantics, and configuration parameters.

For each important technical decision, classify the evidence as one of:

- **Verified:** confirmed by version-matched documentation, source, or a passing test.
- **Partially verified:** evidence exists, but an important behavior remains untested.
- **Unverified:** documentation is missing, ambiguous, inaccessible, or insufficient.

Do not fabricate test results, API support, model capabilities, benchmark results, or deployment availability.

If official documentation and installed code disagree, report the discrepancy and use the behavior verified for the actual environment. Use a minimal isolated test to resolve ambiguity whenever safe and practical.

---

## 2. Product and competition context

Build a web application for Indian college students that converts classroom lectures, including Hinglish and regional-language speech, into structured study notes and a conversational voice tutor.

The entry is for Gnani AI's Great Indian AI Internship Challenge, which closes on November 10, 2026.

**Mandatory voice-model requirement:** use Gnani Prisma for speech-to-text (STT) and Gnani Timbre (beta) for text-to-speech (TTS). Do not silently substitute other voice models in the shipped product.

Verify the current challenge requirements, official product naming, availability, API constraints, supported languages, and model access from authoritative sources. Flag any discrepancy with this brief before implementation.

The main demonstration must show a credible end-to-end journey in approximately 60 seconds:

Teacher uploads a lecture → structured notes appear → student asks a spoken doubt → the application answers from the lecture → the answer is spoken back.

The demonstration may use a clearly identified, previously processed lecture and cached artifacts to avoid unnecessary API calls. Do not use mock responses in place of the real core experience in the shipped demo.

---

## 3. Product scope and implementation order

Build in this order:

1. Lecture ingestion and structured notes.
2. Grounded doubt chat with voice input and output.
3. Text flashcards with basic spaced repetition.
4. Final UI polish, seeded demo flow, and documentation.

Phases 1 and 2 are the core product. A reliable notes pipeline and voice tutor are more important than an incomplete flashcard feature.

### Feature A: Lecture ingestion and notes

The teacher must be able to upload a lecture recording of 45 minutes or longer.

Required workflow:

1. Validate the uploaded file's type, size, duration where available, and supported audio format.
2. Submit audio to Gnani Prisma's batch transcription API.
3. Respect documented duration limits, asynchronous job behavior, polling constraints, and rate limits.
4. If a recording exceeds the verified per-job duration limit, split it into supported chunks.
5. Preserve chunk order and timestamp offsets. Avoid losing words, duplicating transcript segments, or breaking sentences at chunk boundaries.
6. Cache successful transcription results durably so the same recording is never unnecessarily re-transcribed.
7. Normalize the transcript without changing its factual meaning.
8. Split the transcript into timestamped topic segments.
9. Generate structured notes for each segment.
10. Merge the segment outputs into a coherent document containing:
   - Topic headings and subheadings.
   - Explanations suitable for college students.
   - Key terms and definitions.
   - Important equations and concepts when present in the transcript.
   - A concise lecture summary.
   - Timestamped source segments.
11. Preserve the source segments separately because they will serve as retrieval-augmented generation (RAG) chunks.
12. Allow an optional teacher-provided glossary or keyword list to correct mangled technical vocabulary.

**Grounding rule:** the audio contains no visual input. If the teacher says something such as "ye wala equation" or refers to an unseen diagram, do not invent the missing equation, diagram, or context. Mark the relevant note as unclear or dependent on missing visual material, include a timestamp, and explain what information is unavailable.

Make transcript provenance available so that a note or answer can be traced to its source segment.

A note-generation failure must not destroy a successfully cached transcript. Failed processing stages should be resumable independently.

### Feature B: Grounded doubt chat

Students must be able to ask questions by voice or text in Hindi, Hinglish, and Gujarati.

Do not assume that Prisma, Timbre, or the selected chat LLM supports these languages equally well. Verify support and test actual quality.

The conversational workflow should be:

1. Capture student speech using the Gnani streaming STT integration.
2. Let LiveKit Agents manage the real-time conversation lifecycle.
3. Retrieve relevant lecture segments using RAG.
4. Generate a simple, helpful answer grounded in the lecture notes.
5. Stream answer text to the interface.
6. Start Timbre TTS as soon as the first complete, speakable sentence is available, subject to the verified streaming capabilities of its API.
7. Play generated audio as it becomes available.
8. Show the transcript alongside the spoken answer.
9. Let the student interrupt naturally and ask a follow-up question.

When the lecture notes do not contain enough information, the assistant must say so instead of fabricating an answer. Distinguish lecture-grounded information from any additional explanation, if external knowledge is explicitly permitted.

The interface should communicate the current listening, thinking, and speaking state.

### Feature C: Flashcards

Generate text-based question-and-answer flashcards from the lecture notes.

Include basic spaced repetition that tracks review history and schedules subsequent reviews. Keep the initial implementation simple and testable.

Do not begin voice-based flashcard drills until Features A and B pass their acceptance tests. Do not add voice drills merely to make the feature list appear more impressive.

---

## 4. Architecture and technology constraints

### LiveKit and Gnani

Use LiveKit Agents as the real-time orchestration layer.

Use `livekit-plugins-gnani` through the verified supported installation mechanism, potentially `livekit-agents[gnani]` if that extra exists for the chosen version.

Use:

- Gnani Prisma batch STT for lecture transcription.
- Gnani's supported streaming STT integration for live student speech.
- Gnani Timbre beta for generated speech.
- LiveKit Agents for the real-time agent lifecycle and conversation transport.
- Gnani's HTTP API directly for batch transcription.

Keep batch ingestion and live conversational audio paths separate.

The following must be handled by LiveKit rather than custom, hand-rolled equivalents wherever the installed version supports them:

- WebRTC transport.
- Voice activity detection (VAD).
- Turn detection.
- Barge-in and interruption handling.
- Adaptive interruption behavior.

Verify the exact capabilities exposed by the installed version. Do not assume a feature exists because it appears in documentation for another version.

### Critical STT/VAD interaction

Gnani's streaming STT may perform its own VAD and emit partial or per-segment transcripts. Verify the exact behavior for the selected plugin and configuration.

LiveKit must remain responsible for deciding when the student has completed a conversational turn, using the supported VAD and turn-detection mechanisms.

Avoid answering prematurely when the student hesitates, speaks slowly, or pauses mid-thought.

Create specific tests for slow, pause-heavy Hinglish utterances, including:

- Pauses within a sentence.
- Pauses before a technical term.
- Partial transcript updates.
- Multiple transcript segments for a single utterance.
- End-of-turn decisions.
- Student interruption while TTS is playing.

Do not blindly disable the plugin's internal VAD or impose unsupported settings. Investigate the supported integration semantics and choose a compatible configuration.

### LLM architecture

Keep the LLM provider configurable through environment-based settings. Do not tightly couple application logic to a single provider.

Treat these as separate workloads:

**Workload A — Notes generation**

Prioritize long-context handling, factual fidelity, structured output, technical vocabulary, and quality. It runs relatively infrequently for each lecture.

**Workload B — Conversational doubts**

Prioritize time-to-first-token (TTFT), streaming, grounded reasoning, and good Hindi, Hinglish, and Gujarati responses.

Use independent model and provider settings for the two workloads, even if both ultimately use the same provider.

During Phase 0, inspect the existing configuration and ask me which LLM providers and models I already have access to. Ask for provider names and credential availability only, never the actual secret keys.

Benchmark viable candidates on a small, representative evaluation set containing real or carefully curated Hinglish and Gujarati student doubts. Evaluate TTFT, answer quality, language quality, grounding, and reliability.

Select the best justified option for each workload based on observed results, cost constraints, availability, and integration effort. Do not introduce paid services without approval.

Stream tokens and responses wherever the provider and API permit. If a streaming capability is unavailable, document the limitation instead of pretending it is supported.

### Dependencies and maintainability

Start from the actual repository and environment. Do not impose a fashionable stack without justification.

For each new direct dependency, explain in one line what it enables and why an existing dependency cannot fulfill the same need.

Avoid unnecessary abstraction layers, duplicate libraries, unused packages, and premature microservices. Remove unused dependencies only when it is safe, in scope, and approved where required.

---

## 5. Data handling, caching, and privacy

I have 5,000 Gnani programme credits. Conserve them.

- Develop the notes pipeline against cached transcript text whenever possible.
- Mock STT and TTS in unit tests.
- Use cached fixtures for repeated development and regression testing.
- Never re-transcribe identical audio unnecessarily.
- Persist a stable source identifier or content hash so transcript caching can survive application restarts.
- Track processing states and failures so interrupted jobs can resume without repeating completed work.
- Do not call a remote API merely to test application wiring if a safe local mock can establish the same property.
- Clearly distinguish mocked tests from tests that actually exercised the Gnani APIs.

For actual API calls, verify credit usage and documented limits before execution. Ask before running anything likely to consume significant credits.

Treat recordings, transcripts, notes, and student questions as potentially sensitive data. Do not upload them to undeclared services, expose them publicly, or write their contents to unrestricted logs.

Document storage, retention, and cleanup behavior. Do not add a third-party analytics or telemetry service without approval.

---

## 6. Reliability and failure handling

Production-oriented behavior is required even for the demo.

Implement and test:

- Explicit connection and request timeouts.
- Bounded retries with exponential backoff and jitter for retryable failures.
- Rate-limit handling based on documented response headers or retry instructions.
- Respect for Gnani batch polling restrictions.
- Idempotent processing where repeated requests could otherwise duplicate work or consume credits.
- Graceful handling of unavailable providers.
- Transcript and notes preservation when downstream stages fail.
- Graceful text-only fallback if STT or TTS becomes unavailable.
- Safe handling of malformed model output.
- Upload type, size, and duration validation.
- Structured, sanitized logs without secrets or unnecessarily sensitive content.
- Health and readiness endpoints that distinguish application health from the availability of external dependencies.
- Clear user-facing loading, empty, timeout, and error states.

Do not retry non-retryable errors indiscriminately. Do not retry billable operations without considering idempotency and whether the previous operation actually succeeded.

The application must fail visibly and safely instead of hanging indefinitely or displaying fabricated results.

---

## 7. Latency engineering and benchmarking

Latency is a measurable product requirement, not a marketing claim.

Build a repeatable benchmark harness that records per-stage timestamps and computes end-to-end metrics.

### Required measurements

Record at least:

- Audio input completion.
- End-of-speech detection.
- Final STT transcript availability.
- Retrieval start and completion.
- LLM request start.
- LLM first-token latency.
- LLM complete-answer latency.
- TTS request start.
- First TTS audio byte or first playable audio chunk.
- Audio playback start, where measurable.
- Total question-to-first-audible-answer latency.
- Total response completion latency.

Define each metric precisely. Separate user-perceived latency from server-side processing time. Use monotonic clocks for local duration measurements, and correlate distributed timestamps carefully rather than subtracting unrelated clocks.

Run at least 20 measured end-to-end turns for each setup that can be tested legitimately. Report sample count, p50, p95, failures, and the measurement conditions. Identify warm-up runs, cache hits, network location, concurrency, model configuration, and whether the test was a simulation or a real voice conversation.

If 20 runs are infeasible because of cost, service access, deployment restrictions, or hardware availability, stop short of claiming success. Report the completed sample size and why the target was not met.

### Required deployment comparison

Compare:

**A. Local agent worker + LiveKit Cloud**

Agent runs on my machine; real-time transport and LiveKit services run in the cloud.

**B. Agent worker deployed to LiveKit Cloud**

Agent execution is hosted in the cloud using a supported deployment method.

**C. Fully self-hosted LiveKit server on my machine**

Self-host the LiveKit server and required services locally, with explicit analysis of which connections remain local and which still reach remote STT, LLM, or TTS services.

For all three, record:

- Actual architecture and component locations.
- Service regions, where verifiable.
- Network paths and limitations.
- Median and p95 user-perceived latency.
- Reliability and disconnections.
- Deployment complexity.
- Operational overhead.
- Recurring costs, estimated from verified pricing and actual resource requirements.
- Security and operational trade-offs.

Run from my real network in Ahmedabad, India, when access is available. Do not pretend the benchmark ran from Ahmedabad if it ran elsewhere.

A localhost result is not a fair substitute for real student experience. Include a realistic network path and clearly label any estimates, simulations, or unavailable regional tests.

Do not create new paid infrastructure just to complete the comparison without my approval. Use available environments where practical. When a setup cannot be run, provide a documented, evidence-based estimate rather than fabricated benchmark numbers.

### Streaming and interruption optimizations

Verify whether the selected LiveKit Agents version supports:

- Preemptive generation.
- Adaptive interruption handling.
- Sentence-level or incremental TTS.
- Streaming model output and playback.

Evaluate compatible optimizations and compare their effects on TTFT, perceived latency, unnecessary cancellations, early cut-offs, and response quality.

Begin TTS on the first complete, speakable sentence when the supported integration allows it. Avoid speaking incomplete clauses, equations, or malformed text.

Choose the deployment winner using measured user-perceived latency, reliability, operational complexity, and cost—not a single favorable metric.

If the available evidence is insufficient to declare a winner, state that limitation and recommend the next experiment needed to choose responsibly.

---

## 8. Mobile-first interface and accessibility

Design for Indian college students primarily using phones.

Use a restrained, high-contrast visual system, one accent color, readable typography, and clear hierarchy.

Provide two polished primary screens.

### Screen A — Notes reader

- Responsive document layout.
- Clear headings and subheadings.
- Heading navigation.
- Timestamp links to source segments.
- Legible equations and technical notation.
- Proper Devanagari and Gujarati script rendering.
- Appropriate Noto fonts, using existing project assets or a justified, properly loaded font configuration.
- Clear indicators for uncertain content or missing visual context.
- Useful mobile scrolling and navigation.

### Screen B — Doubt chat

- Clear listening, thinking, and speaking states.
- Visible transcript for recognized speech and generated answers.
- Live waveform or audio-level visualization driven by actual audio data when possible.
- Visible microphone and playback states.
- Clear indication of interruptions and barge-in.
- Text input as an alternative to voice.
- Clear stop, retry, and recover-from-error actions.
- Readable text alongside generated speech.

Implement useful empty, loading, offline, failure, and permission-denied states.

Include keyboard accessibility, semantic HTML, accessible labels, focus management, screen-reader-friendly state announcements, and sufficient contrast.

### Design exclusions

No:

- Purple-gradient hero sections or gratuitous gradients.
- Emoji bullets.
- Lorem ipsum.
- Fake statistics or fabricated testimonials.
- Placeholder content presented as real output.
- Filler marketing phrases such as "seamless", "supercharge", or "leverage".
- Decorative visualizations disconnected from actual application state.
- Comments that merely restate the code.
- Dead code, needless abstraction, or leftover TODOs.
- Mock data in the production/demo happy path.

Make the real product functionality visible immediately. Do not spend time building a generic landing page when the notes reader and voice tutor are the core experience.

---

## 9. Phased delivery and approval gates

### Phase 0 — Reconnaissance and planning ONLY

Do not implement features or change application code during this phase.

Inspect:

- Repository files and Git status.
- Existing application architecture and entry points.
- Runtime and dependency versions.
- Existing backend and frontend functionality.
- Available test infrastructure.
- Existing audio-processing and RAG components.
- Environment-variable conventions and configuration.
- Available local compute and relevant service access, without exposing credentials.
- Applicable Gnani and LiveKit documentation.
- Actual compatibility between the package versions and desired model integrations.

Produce a reconnaissance report with these sections:

1. **Existing state:** what is already implemented and what demonstrably works.
2. **Environment:** versions, runtime, package manager, and important constraints.
3. **Documentation findings:** verified APIs, supported languages, limitations, and version-specific behavior.
4. **Architecture proposal:** end-to-end component diagram and data flow.
5. **Stack choices:** each proposed technology and a one-line justification.
6. **Latency plan:** benchmark methodology, three deployment configurations, and required access.
7. **Security and reliability risks:** secrets, upload handling, retries, caching, and failure recovery.
8. **Implementation phases:** files/modules expected to change, tests, acceptance criteria, and dependencies between phases.
9. **Open questions:** only decisions that materially affect implementation.
10. **Cost and credit risks:** expected remote calls, possible credit usage, and any paid resources requiring approval.

Include an assumptions table with the assumption, evidence, confidence, and validation method.

End with the exact decisions or information needed from me, including which LLM providers/models I can access and whether the necessary cloud environments are available.

Do not ask for actual API keys.

**Stop here. Do not code, install dependencies, change configuration, create cloud resources, or make commits until I explicitly approve the plan.**

### Phase 1 — Notes pipeline

Implement the lecture ingestion, transcription caching, transcript cleanup, segmentation, and note generation pipeline.

Before requesting a real lecture sample, establish the test approach using cached fixtures and mocks. Then ask me for a suitable sample recording before conducting real end-to-end audio processing.

Acceptance criteria:

- A supported lecture can be ingested successfully.
- Long recordings are handled according to verified API limits.
- Segment ordering and timestamp offsets are correct.
- Transcript caching prevents redundant transcription.
- Notes are structured and traceable to source segments.
- Unseen visual references are marked uncertain rather than fabricated.
- A failed downstream stage does not destroy cached transcripts.
- Automated tests cover the principal success and failure paths.
- One actual end-to-end sample is processed successfully when a suitable recording and API access are available.

Report measured processing times, API calls, credit implications, known quality issues, and test evidence.

### Phase 2 — Voice doubt chat and latency

Implement RAG-based student questions over lecture notes, real-time voice input, LLM streaming, and Timbre speech output.

Test:

- Hindi, Hinglish, and Gujarati to the extent supported by the chosen components.
- Grounded questions whose answers appear in the lecture.
- Questions not answered by the lecture.
- Slow speech, hesitation, and pause-heavy Hinglish.
- Transcript updates and final transcription.
- Barge-in and interrupted audio generation.
- STT, LLM, and TTS failures.
- Text-only fallback.
- End-to-end latency measurement.

Complete the three deployment comparisons wherever permitted and feasible. Record measurements and limitations. Recommend the best setup based on actual evidence.

Acceptance criteria:

- The student can ask a question through text and supported voice input.
- Answers are grounded in retrieved source segments.
- Streaming is visible and works wherever supported.
- Spoken answers use Timbre, not an unapproved substitute.
- The student can interrupt without the agent continuing to speak over them.
- Errors do not result in fabricated answers or indefinite loading.
- Benchmark results are reproducible and clearly distinguish real measurements from estimates.

### Phase 3 — Flashcards

Generate flashcards from lecture notes and implement simple spaced repetition.

Acceptance criteria:

- Cards reflect source material.
- Question-and-answer pairs are clear and non-duplicative where practical.
- Review state persists across sessions using the chosen storage mechanism.
- Scheduling behavior is deterministic and tested.
- Errors during generation do not corrupt existing cards.

### Phase 4 — Product polish and demo

Polish both core screens, verify script and equation rendering, complete accessibility basics, and implement a reproducible demo path.

Produce:

- A seeded demo using real, preprocessed lecture artifacts.
- A concise README with setup, environment variables, migration or initialization instructions if required, testing, architecture, and troubleshooting.
- An architecture diagram.
- A dependency and security note.
- A benchmark report with methodology and results.
- A short demo script that shows the core journey within approximately 60 seconds.
- Instructions to reset or reseed demo state safely without deleting user data.

The demo path must not depend on a fresh long-running transcription job finishing within 60 seconds. Be transparent about cached preprocessing, and demonstrate the real downstream product functionality.

---

## 10. Reporting, testing, and completion rules

After each phase, provide a report containing:

- What was implemented.
- What works and how it was tested.
- What failed or remains incomplete.
- What could not be verified.
- Test results and exact sample counts.
- Latency and cost figures, if measured.
- Dependency and configuration changes.
- Relevant Git commits.
- Known risks and recommended next steps.

Use explicit labels for **measured**, **estimated**, **mocked**, and **unverified** results.

Do not describe a feature as production-ready because the application starts successfully or a single happy-path request passes.

Do not claim that latency is low without benchmarks. Do not claim language support without testing or authoritative documentation. Do not claim an integration works based solely on successful installation.

Keep logs and reports free of credentials.

The code must remain understandable to one developer maintaining the application. Prefer simple, tested implementations and clear boundaries over unnecessary architecture.

**Completion priority:** correctness → reliable voice conversation → measured latency → mobile UI → flashcards → additional polish.

If a requirement cannot be satisfied with the existing services, APIs, credits, or hardware, report the limitation and propose the smallest viable alternative. Never silently bypass the requirement.

## Final directive

Start with Phase 0 only.

Inspect, verify, document, and propose. Present the reconnaissance report and implementation plan. Then stop and wait for my explicit approval before making changes.