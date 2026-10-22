/**
 * Screen B: doubt chat.
 *
 * The voice tutor and the text path answer from the same lecture, so a
 * student can speak when the microphone works and type when it does
 * not. The screen shows which of the two is happening, what was heard
 * and what was said back.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useLiveKitRoom } from "../hooks/useLiveKitRoom";
import { AudioMeter } from "../components/AudioMeter";
import { ChatAnswerCard } from "../components/ChatAnswerCard";
import { EmptyState, ErrorState } from "../components/States";
import type { ChatAnswer, Lecture } from "../types";

type Item =
  | {
      kind: "voice";
      id: string;
      at: number;
      speaker: "student" | "tutor";
      text: string;
      final: boolean;
    }
  | { kind: "question"; id: string; at: number; text: string }
  | { kind: "answer"; id: string; at: number; answer: ChatAnswer };

export function Chat({
  lectureId,
  lectures,
  onNavigate,
}: {
  lectureId: string | null;
  lectures: Lecture[];
  onNavigate: (path: string, params?: Record<string, string>) => void;
}) {
  const room = useLiveKitRoom(lectureId);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [answer, setAnswer] = useState<ChatAnswer | null>(null);
  const [askError, setAskError] = useState<Error | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const lecture = lectures.find((item) => item.id === lectureId) ?? null;

  useEffect(() => {
    setAnswer(null);
    setAskError(null);
    setItems([]);
  }, [lectureId]);

  useEffect(() => {
    const latest = items[items.length - 1];
    if (!latest) return;
    if (latest.kind === "voice" && !latest.final) return;
    transcriptRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items]);

  const voiceItems: Item[] = useMemo(
    () =>
      room.entries.map((entry) => ({
        kind: "voice" as const,
        id: entry.id,
        at: entry.at,
        speaker: entry.speaker,
        text: entry.text,
        final: entry.final,
      })),
    [room.entries],
  );

  const merged = useMemo(() => {
    return [...items, ...voiceItems].sort((a, b) => a.at - b.at);
  }, [items, voiceItems]);

  const state = describeState(room);

  const ask = async () => {
    const text = question.trim();
    if (!text || !lectureId || pending) return;
    setPending(true);
    setAskError(null);
    setQuestion("");
    setItems((current) => [
      ...current,
      { kind: "question", id: `q-${Date.now()}`, at: Date.now(), text },
    ]);
    try {
      const result = await api.ask(text, lectureId);
      setAnswer(result);
      setItems((current) => [
        ...current,
        { kind: "answer", id: `a-${Date.now()}`, at: Date.now(), answer: result },
      ]);
    } catch (error) {
      setAskError(error instanceof Error ? error : new Error(String(error)));
    } finally {
      setPending(false);
    }
  };

  if (!lectureId) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-6">
        <EmptyState
          title="Choose a lecture"
          body="Pick a lecture above. The tutor answers only about the lecture that is selected."
        />
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 pb-24 pt-4">
      <h1 className="text-xl font-semibold tracking-tight text-ink">Doubt chat</h1>
      <p className="mt-1 text-sm text-muted">
        {lecture ? `About ${lecture.title}. ` : ""}
        Speak to the tutor, or type your question.
      </p>

      <section
        aria-labelledby="voice-heading"
        className="mt-4 rounded-lg border border-line bg-white p-3"
      >
        <div className="flex items-center justify-between gap-2">
          <h2 id="voice-heading" className="text-sm font-semibold text-ink">
            Voice tutor
          </h2>
          <span className="text-xs text-muted">
            {room.micEnabled ? "Microphone on" : "Microphone off"}
          </span>
        </div>

        <p
          aria-live="polite"
          className="mt-2 rounded-md border border-line bg-canvas px-3 py-2 text-sm text-body"
        >
          {state}
        </p>

        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <AudioMeter
            meter={room.inputMeter}
            label="Microphone"
            active={room.micEnabled}
          />
          <AudioMeter
            meter={room.outputMeter}
            label="Tutor playback"
            active={room.agentSpeaking}
          />
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          {room.status === "idle" ? (
            <button
              type="button"
              onClick={() => void room.start()}
              disabled={!lectureId}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-ink disabled:opacity-60"
            >
              Start voice
            </button>
          ) : null}
          {room.status === "connecting" ? (
            <button
              type="button"
              onClick={() => void room.stop()}
              className="rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink"
            >
              Cancel
            </button>
          ) : null}
          {room.status === "connected" || room.status === "reconnecting" ? (
            <>
              <button
                type="button"
                onClick={() => void room.toggleMic()}
                className="rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink hover:bg-canvas"
              >
                {room.micEnabled ? "Mute microphone" : "Unmute microphone"}
              </button>
              <button
                type="button"
                onClick={() => void room.stop()}
                className="rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink hover:bg-canvas"
              >
                Stop voice
              </button>
            </>
          ) : null}
        </div>

        {room.error ? (
          <div className="mt-3">
            <ErrorState
              error={new Error(room.error.message)}
              onRetry={() => {
                room.dismissError();
                void room.start();
              }}
              retryLabel="Retry the voice room"
            />
          </div>
        ) : null}
      </section>

      <section aria-labelledby="conversation-heading" className="mt-5">
        <h2
          id="conversation-heading"
          className="text-sm font-semibold uppercase tracking-wide text-muted"
        >
          Conversation
        </h2>
        <div ref={transcriptRef} className="mt-2 space-y-3">
          {merged.length === 0 ? (
            <p className="rounded-lg border border-dashed border-strong-line bg-white px-4 py-6 text-sm text-muted">
              Nothing has been said yet. Start the voice tutor to speak, or
              type a question below.
            </p>
          ) : null}
          {merged.map((item) => {
            if (item.kind === "question") {
              return (
                <div
                  key={item.id}
                  className="rounded-lg border border-line bg-canvas px-3 py-2"
                >
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                    You
                  </p>
                  <p className="mt-1 text-sm leading-6 text-body">{item.text}</p>
                </div>
              );
            }
            if (item.kind === "answer") {
              return (
                <ChatAnswerCard
                  key={item.id}
                  answer={item.answer}
                  onOpenSource={(seconds) =>
                    onNavigate("/notes", {
                      lecture: lectureId,
                      source: String(Math.round(seconds)),
                    })
                  }
                />
              );
            }
            return (
              <div
                key={item.id}
                className={`rounded-lg border px-3 py-2 ${
                  item.speaker === "student"
                    ? "border-line bg-canvas"
                    : "border-accent bg-white"
                }`}
              >
                <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                  {item.speaker === "student" ? "You" : "Tutor"}
                </p>
                <p
                  className={`mt-1 text-sm leading-6 text-body ${
                    item.final ? "" : "italic text-muted"
                  }`}
                >
                  {item.text}
                </p>
              </div>
            );
          })}
        </div>
      </section>

      <section aria-labelledby="ask-heading" className="mt-5">
        <h2
          id="ask-heading"
          className="text-sm font-semibold uppercase tracking-wide text-muted"
        >
          Ask by text
        </h2>
        <form
          className="mt-2"
          onSubmit={(event) => {
            event.preventDefault();
            void ask();
          }}
        >
          <label htmlFor="question" className="text-sm font-medium text-ink">
            Your question about this lecture
          </label>
          <textarea
            id="question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={3}
            maxLength={2000}
            placeholder="What did the teacher say about ..."
            className="mt-1 w-full rounded-md border border-strong-line bg-white px-3 py-2 text-sm text-ink"
          />
          <div className="mt-2 flex items-center gap-3">
            <button
              type="submit"
              disabled={pending || question.trim().length === 0}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-ink disabled:opacity-60"
            >
              {pending ? "Asking…" : "Send"}
            </button>
            {answer ? (
              <p className="text-xs text-muted">
                Answered in {Math.round(answer.timings.total_ms ?? 0)} ms
              </p>
            ) : null}
          </div>
        </form>
        {askError ? (
          <div className="mt-3">
            <ErrorState error={askError} onRetry={() => void ask()} />
          </div>
        ) : null}
      </section>

    </main>
  );
}

/** The one line that says what the room is doing right now. */
function describeState(room: {
  status: string;
  agentSpeaking: boolean;
  studentSpeaking: boolean;
  interrupted: boolean;
  agentPresent: boolean;
}): string {
  if (room.status === "connecting") return "Connecting to the tutor.";
  if (room.status === "reconnecting") return "Reconnecting to the tutor.";
  if (room.status === "error") return "The voice room stopped. Use the buttons above to retry.";
  if (room.status === "idle") {
    return "Not connected. Start the voice tutor to ask out loud.";
  }
  if (room.interrupted) return "You interrupted the tutor. It is listening to you.";
  if (room.studentSpeaking) return "Listening. You are speaking.";
  if (room.agentSpeaking) return "The tutor is answering.";
  if (!room.agentPresent) return "Listening. Waiting for the tutor to join the room.";
  return "Listening. The tutor will answer when you stop speaking.";
}
