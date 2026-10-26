/**
 * The conversation: what the student said and what the tutor said back.
 *
 * Both voice turns and typed turns appear in the same list, in the order
 * they happened, so a student can read the whole exchange.
 */
import { ChatAnswerCard } from "./ChatAnswerCard";
import type { ChatAnswer } from "../types";

export type ConversationItem =
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

export function Conversation({
  items,
  onOpenSource,
}: {
  items: ConversationItem[];
  onOpenSource: (seconds: number) => void;
}) {
  return (
    <section aria-labelledby="conversation-heading" className="mt-5">
      <h2
        id="conversation-heading"
        className="text-sm font-semibold uppercase tracking-wide text-muted"
      >
        Conversation
      </h2>
      <div className="mt-2 space-y-3">
        {items.length === 0 ? (
          <p className="rounded-lg border border-dashed border-strong-line bg-white px-4 py-6 text-sm text-muted">
            Nothing has been said yet. Start the voice tutor to speak, or type
            a question below.
          </p>
        ) : null}
        {items.map((item) => {
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
                onOpenSource={onOpenSource}
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
  );
}
