/**
 * The voice room.
 *
 * The backend mints a LiveKit token; this hook joins the room named
 * after the lecture, publishes the microphone, listens to the tutor's
 * audio and keeps the student's recognised speech and the tutor's
 * answers as text.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  LocalTrack,
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type TranscriptionSegment,
} from "livekit-client";
import { api } from "../api";
import { createLevelMeter, type LevelMeter } from "../lib/audio";
import { parseMessage, type Speaker } from "../lib/transcript";

export type RoomStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "error";

export type ErrorKind = "permission" | "network" | "unavailable" | "unknown";

export interface RoomError {
  kind: ErrorKind;
  message: string;
  detail?: string;
}

export interface TranscriptEntry {
  id: string;
  speaker: Speaker;
  text: string;
  final: boolean;
  at: number;
}

export interface LiveKitRoom {
  status: RoomStatus;
  error: RoomError | null;
  entries: TranscriptEntry[];
  micEnabled: boolean;
  agentPresent: boolean;
  agentSpeaking: boolean;
  studentSpeaking: boolean;
  interrupted: boolean;
  inputMeter: LevelMeter | null;
  outputMeter: LevelMeter | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  toggleMic: () => Promise<void>;
  dismissError: () => void;
}

function classifyError(error: unknown): RoomError {
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
      message: "Microphone access was refused. Allow it in the browser to speak to the tutor.",
      detail: message,
    };
  }
  if (
    name === "NotFoundError" ||
    lower.includes("no device") ||
    lower.includes("notfounderror")
  ) {
    return {
      kind: "unavailable",
      message: "No microphone was found on this device. Use the text box to ask instead.",
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
      message: "The connection to the tutor dropped. Check your network and try again.",
      detail: message,
    };
  }
  if (lower.includes("livekit is not configured") || lower.includes("503")) {
    return {
      kind: "unavailable",
      message: "Voice chat is not configured on this server. Use the text box to ask instead.",
      detail: message,
    };
  }
  return {
    kind: "unknown",
    message: "The voice room could not be started.",
    detail: message,
  };
}

export function useLiveKitRoom(lectureId: string | null): LiveKitRoom {
  const [status, setStatus] = useState<RoomStatus>("idle");
  const [error, setError] = useState<RoomError | null>(null);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [micEnabled, setMicEnabled] = useState(false);
  const [agentPresent, setAgentPresent] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);
  const [studentSpeaking, setStudentSpeaking] = useState(false);
  const [interrupted, setInterrupted] = useState(false);

  const roomRef = useRef<Room | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const inputMeterRef = useRef<LevelMeter | null>(null);
  const outputMeterRef = useRef<LevelMeter | null>(null);
  const [inputMeter, setInputMeter] = useState<LevelMeter | null>(null);
  const [outputMeter, setOutputMeter] = useState<LevelMeter | null>(null);
  const speakingRef = useRef({ agent: false, student: false });

  const pushEntry = useCallback((speaker: Speaker, text: string, final: boolean) => {
    const clean = text.trim();
    if (!clean) return;
    setEntries((current) => {
      const last = current[current.length - 1];
      if (last && last.speaker === speaker && !last.final) {
        // A partial transcript that is still being updated.
        return [
          ...current.slice(0, -1),
          { ...last, text: clean, final, at: Date.now() },
        ];
      }
      if (last && last.speaker === speaker && last.final && last.text === clean) {
        return current;
      }
      return [
        ...current,
        {
          id: `${Date.now()}-${current.length}`,
          speaker,
          text: clean,
          final,
          at: Date.now(),
        },
      ];
    });
  }, []);

  const handleTranscription = useCallback(
    (segments: TranscriptionSegment[], participantIdentity: string | undefined, localIdentity: string) => {
      for (const segment of segments) {
        const speaker: Speaker =
          participantIdentity && participantIdentity === localIdentity
            ? "student"
            : "tutor";
        pushEntry(speaker, segment.text, segment.final !== false);
      }
    },
    [pushEntry],
  );

  const teardown = useCallback(async () => {
    const room = roomRef.current;
    roomRef.current = null;
    if (room) {
      try {
        await room.disconnect();
      } catch {
        // The room was already closed.
      }
    }
    inputMeterRef.current?.dispose();
    outputMeterRef.current?.dispose();
    inputMeterRef.current = null;
    outputMeterRef.current = null;
    setInputMeter(null);
    setOutputMeter(null);
    if (audioContextRef.current) {
      void audioContextRef.current.close().catch(() => undefined);
      audioContextRef.current = null;
    }
    setStatus("idle");
    setAgentPresent(false);
    setAgentSpeaking(false);
    setStudentSpeaking(false);
    setMicEnabled(false);
  }, []);

  const start = useCallback(async () => {
    if (!lectureId || roomRef.current) return;
    setError(null);
    setStatus("connecting");
    setInterrupted(false);
    setEntries([]);

    try {
      const token = await api.livekitToken(lectureId, "Student");
      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;

      room.on(RoomEvent.TranscriptionReceived, (segments, participant) => {
        handleTranscription(
          segments,
          participant?.identity,
          room.localParticipant.identity,
        );
      });

      room.on(RoomEvent.DataReceived, (payload, participant) => {
        const raw = new TextDecoder().decode(payload);
        const parsed = parseMessage(
          raw,
          participant?.identity === room.localParticipant.identity
            ? "student"
            : "tutor",
        );
        if (parsed) pushEntry(parsed.speaker, parsed.text, true);
      });

      room.on(RoomEvent.ChatMessage, (message, participant) => {
        const parsed = parseMessage(
          message.message ?? "",
          participant?.identity === room.localParticipant.identity
            ? "student"
            : "tutor",
        );
        if (parsed) pushEntry(parsed.speaker, parsed.text, true);
      });

      room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
        const localId = room.localParticipant.identity;
        const student = speakers.some((p) => p.identity === localId);
        const agent = speakers.some((p) => p.identity !== localId);
        const previous = speakingRef.current;
        if (student && previous.agent) setInterrupted(true);
        if (!agent && previous.agent) setInterrupted(false);
        speakingRef.current = { agent, student };
        setStudentSpeaking(student);
        setAgentSpeaking(agent);
      });

      room.on(RoomEvent.ParticipantConnected, () => setAgentPresent(true));
      room.on(RoomEvent.ParticipantDisconnected, () => {
        setAgentPresent(false);
        setAgentSpeaking(false);
      });

      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind !== Track.Kind.Audio) return;
        setAgentPresent(true);
        try {
          const source = track.mediaStreamTrack;
          const context = audioContextRef.current ?? new AudioContext();
          audioContextRef.current = context;
          const meter = createLevelMeter(new MediaStream([source]), context);
          if (meter) {
            outputMeterRef.current?.dispose();
            outputMeterRef.current = meter;
            setOutputMeter(meter);
          }
        } catch {
          // Without a meter the screen still shows the playback state.
        }
      });

      room.on(RoomEvent.Reconnecting, () => setStatus("reconnecting"));
      room.on(RoomEvent.Reconnected, () => setStatus("connected"));
      room.on(RoomEvent.Disconnected, () => {
        setStatus((current) => (current === "idle" ? current : "error"));
        setError({
          kind: "network",
          message: "The voice room closed before the answer finished.",
        });
      });
      room.on(RoomEvent.MediaDevicesError, (deviceError) => {
        setError(classifyError(deviceError));
      });

      await room.connect(token.url, token.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      setMicEnabled(true);
      setStatus("connected");

      const publication = room.localParticipant.getTrackPublication(
        Track.Source.Microphone,
      );
      const track = publication?.track as LocalTrack | undefined;
      const source = track?.mediaStreamTrack;
      if (source) {
        const context = audioContextRef.current ?? new AudioContext();
        audioContextRef.current = context;
        const meter = createLevelMeter(new MediaStream([source]), context);
        if (meter) {
          inputMeterRef.current = meter;
          setInputMeter(meter);
        }
      }
    } catch (caught) {
      await teardown();
      setError(classifyError(caught));
      setStatus("error");
    }
  }, [lectureId, pushEntry, handleTranscription, teardown]);

  const stop = useCallback(async () => {
    await teardown();
    setStatus("idle");
  }, [teardown]);

  const toggleMic = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    try {
      const next = !room.localParticipant.isMicrophoneEnabled;
      await room.localParticipant.setMicrophoneEnabled(next);
      setMicEnabled(next);
    } catch (caught) {
      setError(classifyError(caught));
    }
  }, []);

  const dismissError = useCallback(() => setError(null), []);

  useEffect(() => {
    return () => {
      void teardown();
    };
  }, [teardown]);

  return {
    status,
    error,
    entries,
    micEnabled,
    agentPresent,
    agentSpeaking,
    studentSpeaking,
    interrupted,
    inputMeter,
    outputMeter,
    start,
    stop,
    toggleMic,
    dismissError,
  };
}
