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
      className="card px-4 py-6 text-sm text-muted"
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
    <div className="card border-dashed px-4 py-8 text-center">
      <h2 className="font-serif text-lg font-semibold text-ink">{title}</h2>
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
      className="card border-l-4 border-l-accent px-4 py-5"
    >
      <h2 className="font-serif text-lg font-semibold text-ink">
        {offline ? "No connection" : "Something went wrong"}
      </h2>
      <p className="mt-2 text-sm text-body">{detail}</p>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="btn btn-secondary mt-4"
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
    neutral: "well text-muted",
    accent: "border-2 border-accent bg-accent-soft text-accent-ink",
  };
  return (
    <p className={`px-3 py-2 text-sm ${tones[tone]}`}>{children}</p>
  );
}
