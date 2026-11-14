/**
 * Screen C: flashcards and spaced repetition.
 *
 * The deck is generated from the lecture's own transcript, so a card
 * can only ask about what the recording actually said. The student
 * sees the question first, reveals the answer only after trying, and
 * grades themselves; that grade is what the backend's schedule turns
 * into the next due date.
 */
import { useEffect, useState } from "react";
import { api } from "../api";
import { useAsync } from "../hooks/useAsync";
import type { Flashcard, Lecture } from "../types";
import { EmptyState, ErrorState, LoadingState } from "../components/States";

/** Below 3 the scheduler treats the answer as wrong and resets the card. */
const GRADES = [
  { quality: 1, label: "Again" },
  { quality: 3, label: "Good" },
  { quality: 5, label: "Easy" },
] as const;

export function Flashcards({
  lectureId,
  lectures,
  onNavigate,
}: {
  lectureId: string | null;
  lectures: Lecture[];
  onNavigate: (path: string, params?: Record<string, string>) => void;
}) {
  const lecture = lectures.find((item) => item.id === lectureId) ?? null;
  // A lecture still moving through the pipeline has no deck to fetch:
  // the shell's poll flips this on the moment it settles.
  const settled =
    lecture === null || lecture.status === "ready" || lecture.status === "failed";
  const cards = useAsync(
    () => (lectureId ? api.flashcards(lectureId) : Promise.resolve(null)),
    [lectureId],
    Boolean(lectureId) && settled,
  );

  const [phase, setPhase] = useState<"browse" | "study" | "done">("browse");
  const [deck, setDeck] = useState<Flashcard[]>([]);
  const [position, setPosition] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const [grading, setGrading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const all = cards.data?.cards ?? [];
  const dueCount = all.filter((card) => card.is_due).length;

  // A different lecture is a different deck: nothing carries over.
  useEffect(() => {
    setPhase("browse");
    setDeck([]);
    setPosition(0);
    setRevealed(false);
    setError(null);
  }, [lectureId]);

  const start = () => {
    // Due cards first: what has slipped is what needs rehearsing now.
    setDeck([...all].sort((a, b) => Number(b.is_due) - Number(a.is_due)));
    setPosition(0);
    setRevealed(false);
    setError(null);
    setPhase("study");
  };

  const generate = async () => {
    if (!lectureId || generating) return;
    setGenerating(true);
    setError(null);
    try {
      await api.generateFlashcards(lectureId);
      cards.reload();
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setGenerating(false);
    }
  };

  const grade = async (quality: number) => {
    const card = deck[position];
    if (!card || grading) return;
    setGrading(true);
    setError(null);
    try {
      await api.reviewCard(card.id, quality);
      if (position + 1 >= deck.length) {
        setPhase("done");
      } else {
        setPosition(position + 1);
        setRevealed(false);
      }
    } catch (err) {
      // The card stays where it is: the student can grade it again.
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setGrading(false);
    }
  };

  if (!lectureId) {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        <h1 className="sr-only">Flashcards</h1>
        <EmptyState
          title="Choose a lecture"
          body="Pick a lecture above to make flashcards from it. Each card asks about something the recording actually said."
        />
      </main>
    );
  }

  if (cards.status === "loading") {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        <h1 className="sr-only">Flashcards</h1>
        <LoadingState
          label={generating ? "Writing flashcards from this lecture" : "Loading the flashcards"}
        />
      </main>
    );
  }

  if (cards.status === "error" && cards.error) {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        <h1 className="sr-only">Flashcards</h1>
        <ErrorState error={cards.error} onRetry={cards.reload} />
      </main>
    );
  }

  if (phase === "browse" && all.length === 0) {
    const failed = lecture?.status === "failed";
    const processing = lecture !== null && !settled;
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        <h1 className="sr-only">Flashcards</h1>
        {error ? (
          <div className="mb-4">
            <ErrorState error={error} onRetry={generate} retryLabel="Try again" />
          </div>
        ) : null}
        <EmptyState
          title={
            failed
              ? "This lecture did not finish"
              : processing
                ? "The lecture is still being processed"
                : "No flashcards yet"
          }
          body={
            failed
              ? `The recording was processed up to the ${
                  lecture?.failed_stage ?? "unknown"
                } stage, which failed. ${
                  lecture?.failure_reason ?? ""
                }`.trim()
              : processing
                ? "The recording is still moving through transcription and notes. Flashcards can be generated once it finishes — this page updates on its own."
                : "Generate a deck from this lecture: one question and answer per idea the lecturer explained. Cards you have already seen are never duplicated."
          }
          action={
            failed || processing ? null : (
              <button type="button" onClick={generate} disabled={generating} className="btn btn-primary">
                {generating ? "Generating…" : "Generate flashcards"}
              </button>
            )
          }
        />
      </main>
    );
  }

  if (phase === "browse") {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        {error ? (
          <div className="mb-4">
            <ErrorState error={error} onRetry={generate} retryLabel="Try again" />
          </div>
        ) : null}
        <div className="card px-4 py-5 sm:px-6">
          <h1 className="font-serif text-lg font-semibold text-ink">
            {all.length} flashcards
          </h1>
          <p className="mt-1 text-sm text-muted">
            {dueCount > 0
              ? `${dueCount} due now. The rest come back when the schedule says they are due.`
              : "None are due yet. Studying again now is allowed, and harmless."}
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" onClick={start} className="btn btn-primary">
              {dueCount > 0 ? `Study ${dueCount} due` : "Study the deck"}
            </button>
            <button type="button" onClick={generate} disabled={generating} className="btn btn-secondary">
              {generating ? "Generating…" : "Generate more"}
            </button>
            <button
              type="button"
              onClick={() => onNavigate("/chat", { lecture: lectureId })}
              className="btn btn-secondary"
            >
              Ask a doubt about this lecture
            </button>
          </div>
        </div>
      </main>
    );
  }

  if (phase === "done") {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-6">
        <h1 className="sr-only">Flashcards</h1>
        <EmptyState
          title="Round complete"
          body={`You graded all ${deck.length} cards. Each one now has its next date; the due cards return here when that date arrives.`}
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <button type="button" onClick={start} className="btn btn-primary">
                Study again
              </button>
              <button
                type="button"
                onClick={() => setPhase("browse")}
                className="btn btn-secondary"
              >
                Back to the deck
              </button>
            </div>
          }
        />
      </main>
    );
  }

  const card = deck[position];
  if (!card) {
    // The deck emptied underneath us; the overview is the honest fallback.
    setPhase("browse");
    return null;
  }

  return (
    <main id="main" className="mx-auto max-w-3xl px-4 py-6">
      <p className="eyebrow" role="status">
        Card {position + 1} of {deck.length}
        {card.is_due ? " · was due" : ""}
      </p>
      <div className="card mt-2 px-4 py-6 sm:px-6">
        <h1 className="font-serif text-lg font-semibold leading-snug text-ink">
          {card.question}
        </h1>
        {revealed ? (
          <>
            <hr className="my-4 border-line" />
            <p className="whitespace-pre-line text-sm leading-6 text-body">
              {card.answer}
            </p>
            <div className="mt-5 flex flex-wrap gap-2">
              {GRADES.map((gradeOption) => (
                <button
                  key={gradeOption.quality}
                  type="button"
                  disabled={grading}
                  onClick={() => grade(gradeOption.quality)}
                  className="btn btn-secondary"
                >
                  {gradeOption.label}
                </button>
              ))}
            </div>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setRevealed(true)}
            className="btn btn-primary"
          >
            Show answer
          </button>
        )}
      </div>
      {error ? (
        <div className="mt-4">
          <ErrorState error={error} />
        </div>
      ) : null}
      <p className="mt-3 text-xs text-muted">
        “Again” restarts this card’s schedule; “Good” and “Easy” push the
        next review further out.
      </p>
    </main>
  );
}
