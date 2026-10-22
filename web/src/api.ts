/**
 * A small typed client for the FastAPI backend.
 *
 * Every call goes through `request`, so error handling, the JSON
 * headers and the base URL are in one place. The backend serves the
 * built SPA from the same origin, so the default base is empty; the
 * Vite dev server proxies `/api` to port 8000.
 */
import type {
  ChatAnswer,
  ChatSearchResult,
  Flashcard,
  FlashcardReview,
  Lecture,
  LectureDetail,
  LivekitToken,
  NotesPayload,
  Readiness,
} from "./types";

const BASE: string = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

/** An API call that failed, with enough detail to show the student. */
export class ApiError extends Error {
  readonly status: number;
  readonly offline: boolean;

  constructor(message: string, status: number, offline = false) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.offline = offline;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
      ...init,
    });
  } catch {
    throw new ApiError(
      "Cannot reach the server. Check your connection and try again.",
      0,
      true,
    );
  }

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  return (await response.json()) as T;
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      if (first?.msg) return first.msg;
    }
  } catch {
    // A non-JSON error body: fall through to the generic message.
  }
  return `The server returned ${response.status}.`;
}

export const api = {
  health: () => request<{ status: string; time: number }>("/api/health"),

  ready: () => request<Readiness>("/api/ready"),

  lectures: () => request<{ lectures: Lecture[] }>("/api/lectures"),

  lecture: (id: string) => request<LectureDetail>(`/api/lectures/${id}`),

  notes: (id: string) => request<NotesPayload>(`/api/lectures/${id}/notes`),

  ask: (question: string, lectureId: string, topK = 20) =>
    request<ChatAnswer>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ question, lecture_id: lectureId, top_k: topK }),
    }),

  search: (q: string, lectureId?: string) => {
    const params = new URLSearchParams({ q });
    if (lectureId) params.set("lecture_id", lectureId);
    return request<ChatSearchResult>(`/api/chat/search?${params.toString()}`);
  },

  livekitToken: (lectureId: string, participantName: string) =>
    request<LivekitToken>("/api/livekit/token", {
      method: "POST",
      body: JSON.stringify({
        lecture_id: lectureId,
        participant_name: participantName,
      }),
    }),

  flashcards: (lectureId: string) =>
    request<{ cards: Flashcard[]; count: number }>(
      `/api/flashcards?lecture_id=${encodeURIComponent(lectureId)}`,
    ),

  reviewCard: (cardId: number, quality: number) =>
    request<FlashcardReview>(`/api/flashcards/${cardId}/review`, {
      method: "POST",
      body: JSON.stringify({ quality }),
    }),
};
