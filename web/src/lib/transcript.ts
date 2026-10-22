/**
 * Reading the tutor's words out of the room.
 *
 * The agent can reach the browser in three ways, and all three are
 * accepted so the screen works whichever one the agent worker uses:
 *
 * 1. LiveKit's own transcription events, where the agent's published
 *    audio carries a transcript.
 * 2. A data message with JSON such as
 *    `{"type": "agent_transcript", "text": "..."}`.
 * 3. A LiveKit chat message with plain text.
 *
 * The student's own words arrive the same way; the agent forwards what
 * it recognised from the microphone.
 */

export type Speaker = "student" | "tutor";

export interface ParsedMessage {
  speaker: Speaker;
  text: string;
}

const STUDENT_HINTS = ["user", "student", "stt", "question", "human"];
const TUTOR_HINTS = ["agent", "tutor", "assistant", "answer", "tts", "reply", "bot"];

function classify(role: string | undefined, fallback: Speaker): Speaker {
  if (!role) return fallback;
  const value = role.toLowerCase();
  if (STUDENT_HINTS.some((hint) => value.includes(hint))) return "student";
  if (TUTOR_HINTS.some((hint) => value.includes(hint))) return "tutor";
  return fallback;
}

/** Pull the text out of a JSON payload, whatever it calls the field. */
function textOf(value: Record<string, unknown>): string {
  for (const key of ["text", "transcript", "content", "message", "answer", "body"]) {
    const found = value[key];
    if (typeof found === "string" && found.trim()) return found.trim();
  }
  return "";
}

/** What the tutor says it is doing, when it says so itself. */
export type RoomActivity = "listening" | "thinking" | "speaking";

/**
 * Read an activity hint out of a data message.
 *
 * The agent may publish `{"type": "agent_state", "state": "thinking"}`.
 * When it does, that is the most accurate thing on screen, so it is
 * preferred over the state inferred from who is speaking.
 */
export function parseActivity(raw: string): RoomActivity | null {
  const trimmed = raw.trim();
  if (!trimmed.startsWith("{")) return null;
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    const state =
      typeof parsed.state === "string" ? parsed.state.toLowerCase() : "";
    if (state === "listening" || state === "thinking" || state === "speaking") {
      return state;
    }
  } catch {
    // Not a JSON message, so it carries no activity hint.
  }
  return null;
}

/**
 * Read one message from the room. `fallback` is the speaker to assume
 * when the payload does not say who is talking.
 */
export function parseMessage(raw: string, fallback: Speaker): ParsedMessage | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;

  let role: string | undefined;
  let text = "";

  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      if (typeof parsed === "object" && parsed !== null) {
        role =
          (typeof parsed.type === "string" && parsed.type) ||
          (typeof parsed.role === "string" && parsed.role) ||
          (typeof parsed.speaker === "string" && parsed.speaker) ||
          (typeof parsed.event === "string" && parsed.event) ||
          undefined;
        text = textOf(parsed);
        if (!text && typeof parsed.delta === "string") text = parsed.delta.trim();
      }
    } catch {
      return null;
    }
  } else {
    text = trimmed;
  }

  if (!text) return null;
  return { speaker: classify(role, fallback), text };
}
