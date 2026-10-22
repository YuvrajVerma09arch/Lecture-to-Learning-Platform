/** The lecture picker, shared by both screens. */
import type { Lecture } from "../types";
import { STATUS_LABELS } from "../lib/format";

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
      <div className="border-b border-line bg-canvas px-4 py-3">
        <p className="text-sm text-muted" role="status">
          Loading lectures
        </p>
      </div>
    );
  }

  if (lectures.length === 0) {
    return (
      <div className="border-b border-line bg-canvas px-4 py-3">
        <p className="text-sm text-muted">
          No lectures yet. A lecture appears here once its recording has been
          processed.
        </p>
      </div>
    );
  }

  return (
    <div className="border-b border-line bg-canvas px-4 py-3">
      <div className="mx-auto flex max-w-5xl flex-col gap-1 sm:flex-row sm:items-center sm:gap-3">
        <label
          htmlFor="lecture-picker"
          className="text-sm font-medium text-ink"
        >
          Lecture
        </label>
        <select
          id="lecture-picker"
          value={selectedId ?? ""}
          onChange={(event) => onSelect(event.target.value)}
          className="min-w-0 flex-1 rounded-md border border-strong-line bg-white px-3 py-2 text-sm text-ink"
        >
          {lectures.map((lecture) => (
            <option key={lecture.id} value={lecture.id}>
              {lecture.title}
            </option>
          ))}
        </select>
        {status ? (
          <p className="text-sm text-muted">
            <span className="font-medium text-ink">Status:</span> {status}
          </p>
        ) : null}
      </div>
    </div>
  );
}
