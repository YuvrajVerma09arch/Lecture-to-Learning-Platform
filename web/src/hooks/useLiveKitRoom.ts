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
  Room,
  RoomEvent,
  Track,
  type LocalTrack,
  type RemoteTrack,
  type TranscriptionSegment,
} from "livekit-client";
import { api } from "../api";
import { createLevelMeter, type LevelMeter } from "../lib/audio";
import { classifyError, type RoomError } from "../lib/roomErrors";
import {
  parseActivity,
  parseMessage,
  type RoomActivity,
  type Speaker,
} from "../lib/transcript";
import { useRoomTranscript } from "./useRoomTranscript";

export type RoomStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "error";

export interface LiveKitRoom {
  status: RoomStatus;
  error: RoomError | null;
  entries: ReturnType<typeof useRoomTranscript>["entries"];
  micEnabled: boolean;
  agentPresent: boolean;
  agentSpeaking: boolean;
  studentSpeaking: boolean;
  interrupted: boolean;
  /** The tutor has the floor and is composing an answer. */
  thinking: boolean;
  /** The activity the tutor announced for itself, if it announced one. */
  announced: RoomActivity | null;
  inputMeter: LevelMeter | null;
  outputMeter: LevelMeter | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  toggleMic: () => Promise<void>;
  dismissError: () => void;
}

export function useLiveKitRoom(lectureId: string | null): LiveKitRoom {
  const [status, setStatus] = useState<RoomStatus>("idle");
  const [error, setError] = useState<RoomError | null>(null);
  const { entries, push, clear } = useRoomTranscript();
  const [micEnabled, setMicEnabled] = useState(false);
  const [agentPresent, setAgentPresent] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);
  const [studentSpeaking, setStudentSpeaking] = useState(false);
  const [interrupted, setInterrupted] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [announced, setAnnounced] = useState<RoomActivity | null>(null);

  const roomRef = useRef<Room | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const inputMeterRef = useRef<LevelMeter | null>(null);
  const outputMeterRef = useRef<LevelMeter | null>(null);
  const [inputMeter, setInputMeter] = useState<LevelMeter | null>(null);
  const [outputMeter, setOutputMeter] = useState<LevelMeter | null>(null);
  /** Set while the room is being closed on purpose, so a clean stop is
   * not reported as a failure. */
  const closingRef = useRef(false);
  const speakingRef = useRef({ agent: false, student: false });

  const pushEntry = useCallback(
    (speaker: Speaker, text: string, final: boolean) => {
      push(speaker, text, final);
      if (speaker === "tutor") {
        setThinking(false);
        setAnnounced(null);
      }
    },
    [push],
  );

  const teardown = useCallback(async () => {
    const room = roomRef.current;
    roomRef.current = null;
    closingRef.current = true;
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
    setThinking(false);
    setAnnounced(null);
    setMicEnabled(false);
  }, []);

  const meterFor = useCallback((stream: MediaStream): LevelMeter | null => {
    const context = audioContextRef.current ?? new AudioContext();
    audioContextRef.current = context;
    if (context.state === "suspended") {
      void context.resume().catch(() => undefined);
    }
    return createLevelMeter(stream, context);
  }, []);

  const start = useCallback(async () => {
    if (!lectureId || roomRef.current) return;
    closingRef.current = false;
    setError(null);
    setStatus("connecting");
    setInterrupted(false);
    setThinking(false);
    setAnnounced(null);
    clear();

    try {
      const token = await api.livekitToken(lectureId, "Student");
      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;

      room.on(
        RoomEvent.TranscriptionReceived,
        (segments: TranscriptionSegment[], participant) => {
          const local = room.localParticipant.identity;
          for (const segment of segments) {
            const speaker: Speaker =
              participant?.identity && participant.identity === local
                ? "student"
                : "tutor";
            pushEntry(speaker, segment.text, segment.final !== false);
          }
        },
      );

      room.on(RoomEvent.DataReceived, (payload, participant) => {
        const raw = new TextDecoder().decode(payload);
        const activity = parseActivity(raw);
        if (activity) {
          setAnnounced(activity);
          if (activity === "thinking") setThinking(true);
          if (activity === "speaking") setThinking(false);
          return;
        }
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
        const local = room.localParticipant.identity;
        const student = speakers.some((p) => p.identity === local);
        const agent = speakers.some((p) => p.identity !== local);
        const previous = speakingRef.current;
        if (student && previous.agent) setInterrupted(true);
        if (!agent && previous.agent) setInterrupted(false);
        // The student stopped talking and the tutor has not started: the
        // tutor is working on the answer.
        if (!student && previous.student && !agent) setThinking(true);
        if (agent) setThinking(false);
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
          const meter = meterFor(new MediaStream([track.mediaStreamTrack]));
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
        if (closingRef.current) return;
        setStatus("error");
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
      const source = (publication?.track as LocalTrack | undefined)
        ?.mediaStreamTrack;
      if (source) {
        const meter = meterFor(new MediaStream([source]));
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
  }, [lectureId, pushEntry, clear, meterFor, teardown]);

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
    thinking,
    announced,
    inputMeter,
    outputMeter,
    start,
    stop,
    toggleMic,
    dismissError,
  };
}
