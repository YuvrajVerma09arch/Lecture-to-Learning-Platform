/** The lecture picker, shared by both screens. */
import type { Lecture } from "../types";
import { STATUS_LABELS } from "../lib/format";

/** One tone per verdict: settled-ok, settled-bad, still moving. */
function chipTone(status: string): string {
  if (status === "ready") return "chip chip-ok";
  if (status === "failed") return "chip chip-warn";
  return "chip chip-accent";
}

export function LectureBar({
  lectures,
  selectedId,
  onSelect,
  loading,
}: {
  lectures: Lecture[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
}) {
  const selected = lectures.find((lecture) => lecture.id === selectedId) ?? null;
  const status = selected ? STATUS_LABELS[selected.status] ?? selected.status : null;

  if (loading) {
    return (
      <div className="border-b border-line bg-surface-2 px-4 py-3">
        <p className="eyebrow" role="status">
          Loading lectures
        </p>
      </div>
    );
  }

  if (lectures.length === 0) {
    return (
      <div className="border-b border-line bg-surface-2 px-4 py-3">
        <p className="text-sm text-muted">
          No lectures yet. A lecture appears here once its recording has been
          processed.
        </p>
      </div>
    );
  }

  const detail = [
    selected?.failed_stage ? `at the ${selected.failed_stage} stage` : null,
    selected?.failure_reason ?? null,
  ]
    .filter(Boolean)
    .join(" — ");

  return (
    <div className="border-b border-line bg-surface-2 px-4 py-3">
      <div className="mx-auto flex max-w-5xl flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
        <label
          htmlFor="lecture-picker"
          className="eyebrow shrink-0"
        >
          Lecture
        </label>
        <select
          id="lecture-picker"
          value={selectedId ?? ""}
          onChange={(event) => onSelect(event.target.value)}
          className="input min-w-0 flex-1"
        >
          {lectures.map((lecture) => (
            <option key={lecture.id} value={lecture.id}>
              {lecture.title} ({lecture.language_code})
            </option>
          ))}
        </select>
        {status ? (
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
            <span className={chipTone(selected?.status ?? "")}>{status}</span>
            {detail ? <span className="min-w-0">{detail}</span> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
