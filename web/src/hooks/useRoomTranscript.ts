/**
 * The conversation transcript.
 *
 * The tutor's words arrive as LiveKit transcription segments, as data
 * messages, or as chat messages; the student's own words arrive the
 * same way. This hook keeps the merged list.
 *
 * A partial transcript is folded into the previous line while it is
 * still being revised, so the screen shows one growing line rather
 * than a dozen fragments.
 */
import { useCallback, useState } from "react";
import type { Speaker } from "../lib/transcript";

export interface TranscriptEntry {
  id: string;
  speaker: Speaker;
  text: string;
  /** False while the recogniser is still revising the words. */
  final: boolean;
  at: number;
}

export interface RoomTranscript {
  entries: TranscriptEntry[];
  push: (speaker: Speaker, text: string, final: boolean) => void;
  clear: () => void;
}

export function useRoomTranscript(): RoomTranscript {
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);

  const push = useCallback((speaker: Speaker, text: string, final: boolean) => {
    const clean = text.trim();
    if (!clean) return;
    setEntries((current) => {
      const last = current[current.length - 1];
      if (last && last.speaker === speaker && !last.final) {
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

  const clear = useCallback(() => setEntries([]), []);

  return { entries, push, clear };
}
