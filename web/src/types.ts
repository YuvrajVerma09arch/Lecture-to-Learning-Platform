/**
 * The shapes the FastAPI backend returns. Kept in one file so the
 * contract is visible at a glance; the API client in `api.ts` is typed
 * against these.
 */

export interface Lecture {
  id: string;
  title: string;
  original_filename: string;
  audio_seconds: number | null;
  language_code: string;
  status: string;
  failed_stage: string | null;
  failure_reason: string | null;
  created_at: number;
  updated_at: number;
}

export interface StageState {
  stage: string;
  status: string;
  detail?: string;
}

export interface CreditUsage {
  kind: string;
  estimated: number;
  actual_seconds: number | null;
}

export interface LectureDetail extends Lecture {
  stages: StageState[];
  segment_count: number;
  visual_dependent_count: number;
  recent_credit_usage: CreditUsage[];
}

export interface Segment {
  seq: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  visual_dependent: boolean;
}

export interface NotesPayload {
  lecture_id: string;
  title: string;
  status: string;
  /** The merged notes, in Markdown. Null until the notes pass finishes. */
  document: string | null;
  summary: string;
  segments: Segment[];
}

export interface RetrievedSegment {
  id: number;
  seq: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  score: number;
  semantic_score: number;
  excerpt_index: number;
  visual_dependent: boolean;
}

export interface ChatAnswer {
  covered: boolean;
  answer: string;
  cited_segments: number[];
  confidence: number;
  note: string;
  retrieved: RetrievedSegment[];
  used_llm: boolean;
  timings: Record<string, number>;
}

export interface ChatSearchResult {
  query: string;
  results: RetrievedSegment[];
  answerable: boolean;
}

export interface LivekitToken {
  token: string;
  url: string;
  room: string;
  expires_at: number;
}

export interface Flashcard {
  id: number;
  lecture_id: string;
  question: string;
  answer: string;
  due_at: number;
  is_due: boolean;
  repetitions: number;
  ease: number;
}

export interface FlashcardReview {
  id: number;
  repetitions: number;
  ease: number;
  interval_days: number;
  due_at: number;
}

export interface Readiness {
  status: string;
  checks: Record<string, { ok: boolean; error?: string; required_for?: string }>;
  time: number;
}
