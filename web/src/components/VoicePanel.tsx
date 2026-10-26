/**
 * The voice tutor panel.
 *
 * It says what the room is doing right now -- listening, thinking or
 * speaking -- and shows the level of the microphone and of the tutor's
 * playback, both read from the real signal.
 */
import { AudioMeter } from "./AudioMeter";
import { ErrorState } from "./States";
import type { LiveKitRoom } from "../hooks/useLiveKitRoom";

export function VoicePanel({ room }: { room: LiveKitRoom }) {
  const state = describeState(room);

  return (
    <section
      aria-labelledby="voice-heading"
      className="mt-4 rounded-lg border border-line bg-white p-3"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 id="voice-heading" className="text-sm font-semibold text-ink">
          Voice tutor
        </h2>
        <span
          className={`rounded-md border px-2 py-0.5 text-xs font-medium ${
            room.micEnabled
              ? "border-accent text-accent-ink"
              : "border-strong-line text-muted"
          }`}
        >
          {room.micEnabled ? "Microphone on" : "Microphone off"}
        </span>
      </div>

      <p
        aria-live="polite"
        aria-atomic="true"
        className="mt-2 rounded-md border border-line bg-canvas px-3 py-2 text-sm text-body"
      >
        {state}
      </p>

      {room.interrupted ? (
        <p className="mt-2 rounded-md border border-accent bg-white px-3 py-2 text-sm text-accent-ink">
          You interrupted the tutor. It stopped and is listening to you.
        </p>
      ) : null}

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <AudioMeter
          meter={room.inputMeter}
          label="Microphone"
          active={room.micEnabled}
        />
        <AudioMeter
          meter={room.outputMeter}
          label="Tutor playback"
          active={room.agentSpeaking}
        />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {room.status === "idle" ? (
          <button
            type="button"
            onClick={() => void room.start()}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-ink"
          >
            Start voice
          </button>
        ) : null}
        {room.status === "connecting" ? (
          <button
            type="button"
            onClick={() => void room.stop()}
            className="rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink"
          >
            Cancel
          </button>
        ) : null}
        {room.status === "connected" || room.status === "reconnecting" ? (
          <>
            <button
              type="button"
              onClick={() => void room.toggleMic()}
              className="rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink hover:bg-canvas"
            >
              {room.micEnabled ? "Mute microphone" : "Unmute microphone"}
            </button>
            <button
              type="button"
              onClick={() => void room.stop()}
              className="rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink hover:bg-canvas"
            >
              Stop voice
            </button>
          </>
        ) : null}
      </div>

      {room.error ? (
        <div className="mt-3">
          <ErrorState
            error={new Error(room.error.message)}
            onRetry={() => {
              room.dismissError();
              void room.start();
            }}
            retryLabel="Retry the voice room"
          />
        </div>
      ) : null}
    </section>
  );
}

/**
 * The one line that says what the room is doing right now.
 *
 * The tutor's own announcement wins when it makes one; otherwise the
 * state is read from who is speaking.
 */
function describeState(room: LiveKitRoom): string {
  if (room.status === "connecting") return "Connecting to the tutor.";
  if (room.status === "reconnecting") return "Reconnecting to the tutor.";
  if (room.status === "error") {
    return "The voice room stopped. Use the buttons above to retry.";
  }
  if (room.status === "idle") {
    return "Not connected. Start the voice tutor to ask out loud.";
  }
  if (room.announced === "thinking") return "The tutor is working on your question.";
  if (room.announced === "speaking") return "The tutor is answering.";
  if (room.announced === "listening") return "Listening. The tutor will answer when you stop speaking.";
  if (room.interrupted) return "Listening. You interrupted the tutor.";
  if (room.agentSpeaking) return "The tutor is answering.";
  if (room.studentSpeaking) return "Listening. You are speaking.";
  if (room.thinking) return "The tutor is working on your question.";
  if (!room.agentPresent) {
    return "Listening. Waiting for the tutor to join the room.";
  }
  return "Listening. The tutor will answer when you stop speaking.";
}
