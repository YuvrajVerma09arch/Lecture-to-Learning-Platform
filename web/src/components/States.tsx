/**
 * The shared state screens: loading, empty, failed and offline.
 *
 * Every message here is honest about what is and is not known. Nothing
 * is shown as real output that is not.
 */
import type { ReactNode } from "react";
import { ApiError } from "../api";

export function LoadingState({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="rounded-lg border border-line bg-white px-4 py-6 text-sm text-muted"
    >
      {label}
    </div>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-strong-line bg-white px-4 py-8 text-center">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      <p className="mx-auto mt-2 max-w-prose text-sm text-muted">{body}</p>
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  retryLabel = "Try again",
}: {
  error: Error;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const offline = error instanceof ApiError && error.offline;
  const detail = error instanceof ApiError ? error.message : error.message;
  return (
    <div
      role="alert"
      className="rounded-lg border border-strong-line bg-white px-4 py-5"
    >
      <h2 className="text-base font-semibold text-ink">
        {offline ? "No connection" : "Something went wrong"}
      </h2>
      <p className="mt-2 text-sm text-body">{detail}</p>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink hover:bg-canvas"
        >
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

/** A small label for a state that is not an error. */
export function Note({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent";
}) {
  const tones = {
    neutral: "border-strong-line bg-canvas text-muted",
    accent: "border-accent bg-white text-accent-ink",
  };
  return (
    <p className={`rounded-md border px-3 py-2 text-sm ${tones[tone]}`}>{children}</p>
  );
}
