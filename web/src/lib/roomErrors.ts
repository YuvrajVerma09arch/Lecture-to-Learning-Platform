/**
 * What can go wrong in the voice room, and what to tell the student.
 *
 * The browser throws several different errors for the same class of
 * problem -- a refused microphone, a missing microphone, a dropped
 * socket. They are grouped here so the chat screen can show one clear
 * message and one clear recovery action.
 */

export type ErrorKind = "permission" | "network" | "unavailable" | "unknown";

export interface RoomError {
  kind: ErrorKind;
  message: string;
  detail?: string;
}

export function classifyError(error: unknown): RoomError {
  const name = error instanceof Error ? error.name : "";
  const message = error instanceof Error ? error.message : String(error);
  const lower = message.toLowerCase();

  if (
    name === "NotAllowedError" ||
    name === "PermissionDeniedError" ||
    lower.includes("permission denied") ||
    lower.includes("not allowed")
  ) {
    return {
      kind: "permission",
      message:
        "Microphone access was refused. Allow it in the browser to speak to the tutor, or type your question below.",
      detail: message,
    };
  }

  if (
    name === "NotFoundError" ||
    name === "DevicesNotFoundError" ||
    lower.includes("no device") ||
    lower.includes("notfounderror")
  ) {
    return {
      kind: "unavailable",
      message:
        "No microphone was found on this device. Use the text box to ask instead.",
      detail: message,
    };
  }

  if (lower.includes("livekit is not configured") || lower.includes("503")) {
    return {
      kind: "unavailable",
      message:
        "Voice chat is not configured on this server. Use the text box to ask instead.",
      detail: message,
    };
  }

  if (
    lower.includes("failed to fetch") ||
    lower.includes("network") ||
    lower.includes("websocket") ||
    lower.includes("timed out") ||
    lower.includes("connection")
  ) {
    return {
      kind: "network",
      message:
        "The connection to the tutor dropped. Check your network and try again.",
      detail: message,
    };
  }

  return {
    kind: "unknown",
    message: "The voice room could not be started.",
    detail: message,
  };
}
