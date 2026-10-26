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
import { Conversation, type ConversationItem } from "../components/Conversation";
import { QuestionForm } from "../components/QuestionForm";
import { VoicePanel } from "../components/VoicePanel";
import { EmptyState } from "../components/States";
import type { ChatAnswer, Lecture } from "../types";

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
  const [items, setItems] = useState<ConversationItem[]>([]);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const lastQuestion = useRef("");
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

  const voiceItems: ConversationItem[] = useMemo(
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

  const merged = useMemo(
    () => [...items, ...voiceItems].sort((a, b) => a.at - b.at),
    [items, voiceItems],
  );

  const ask = async (textOverride?: string) => {
    const text = (textOverride ?? question).trim();
    if (!text || !lectureId || pending) return;
    lastQuestion.current = text;
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
      // The question is put back so it can be edited and sent again.
      setQuestion(text);
    } finally {
      setPending(false);
    }
  };

  if (!lectureId) {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        <EmptyState
          title="Choose a lecture"
          body="Pick a lecture above. The tutor answers only about the lecture that is selected."
        />
      </main>
    );
  }

  return (
    <main id="main" className="mx-auto max-w-3xl px-4 pb-24 pt-4">
      <h1 className="text-xl font-semibold tracking-tight text-ink">Doubt chat</h1>
      <p className="mt-1 text-sm text-muted">
        {lecture ? `About ${lecture.title}. ` : ""}
        Speak to the tutor, or type your question.
      </p>

      <VoicePanel room={room} />

      <div ref={transcriptRef}>
        <Conversation
          items={merged}
          onOpenSource={(seconds) =>
            onNavigate("/notes", {
              lecture: lectureId,
              source: String(Math.round(seconds)),
            })
          }
        />
      </div>

      <QuestionForm
        value={question}
        onChange={setQuestion}
        onSubmit={() => void ask()}
        pending={pending}
        error={askError}
        onRetry={() => void ask(lastQuestion.current)}
        lastAnswerMs={answer ? (answer.timings.total_ms ?? 0) : null}
      />
    </main>
  );
}
