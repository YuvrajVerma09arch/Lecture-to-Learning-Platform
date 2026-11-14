/**
 * A small data-fetching hook.
 *
 * Every backend call goes through here so that loading, error and
 * offline states look the same on both screens, and so a stale response
 * can never overwrite a newer one.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

export interface AsyncResult<T> {
  status: "loading" | "ready" | "error";
  data: T | null;
  error: Error | null;
  reload: () => void;
  /** Re-fetch without flipping back to `loading`: for background polling. */
  refresh: () => void;
}

export function useAsync<T>(
  load: () => Promise<T>,
  deps: unknown[],
  enabled = true,
): AsyncResult<T> {
  const [status, setStatus] = useState<AsyncResult<T>["status"]>(
    enabled ? "loading" : "ready",
  );
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [nonce, setNonce] = useState(0);
  const requestId = useRef(0);
  // Set when the next fetch is a background refresh: it must not flip
  // the screen back to its loading state.
  const silent = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setStatus("ready");
      return;
    }
    const id = requestId.current + 1;
    requestId.current = id;
    let cancelled = false;
    if (!silent.current) {
      setStatus("loading");
    }
    silent.current = false;
    setError(null);
    load()
      .then((result) => {
        if (cancelled || requestId.current !== id) return;
        setData(result);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (cancelled || requestId.current !== id) return;
        setError(err instanceof Error ? err : new Error(String(err)));
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const refresh = useCallback(() => {
    silent.current = true;
    setNonce((n) => n + 1);
  }, []);
  return { status, data, error, reload, refresh };
}

/** True when the failure was the network, not the server. */
export function isOffline(error: Error | null): boolean {
  return error instanceof ApiError && error.offline;
}
