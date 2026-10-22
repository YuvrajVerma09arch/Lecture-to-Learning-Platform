/**
 * A microphone level meter driven by the real audio.
 *
 * The level comes from a Web Audio `AnalyserNode` on the live track, so
 * the bar moves with the room and not with a timer. Nothing is drawn
 * when there is no audio.
 */

export interface LevelMeter {
  /** The current level, 0 to 1. */
  getLevel: () => number;
  /** Stop reading audio and release the analyser. */
  dispose: () => void;
}

/**
 * RMS of the time-domain data, mapped so that ordinary speech sits
 * around half the bar and loud speech reaches the top.
 */
function rmsToLevel(rms: number): number {
  const scaled = Math.min(1, Math.max(0, (rms - 0.01) * 6));
  return Math.min(1, scaled);
}

export function createLevelMeter(
  stream: MediaStream,
  context?: AudioContext,
): LevelMeter | null {
  const Ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!Ctor) return null;

  let ctx = context;
  let ownsContext = false;
  try {
    if (!ctx) {
      ctx = new Ctor();
      ownsContext = true;
    }
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.6;
    source.connect(analyser);
    const buffer = new Uint8Array(analyser.fftSize);
    let disposed = false;

    // A context created outside a user gesture starts suspended, and a
    // suspended context reports silence. The page has sticky activation
    // by the time a student presses "Start voice", so resuming works.
    if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);

    return {
      getLevel: () => {
        if (disposed) return 0;
        analyser.getByteTimeDomainData(buffer);
        let sum = 0;
        for (let i = 0; i < buffer.length; i += 1) {
          const sample = (buffer[i] - 128) / 128;
          sum += sample * sample;
        }
        return rmsToLevel(Math.sqrt(sum / buffer.length));
      },
      dispose: () => {
        disposed = true;
        try {
          source.disconnect();
          analyser.disconnect();
        } catch {
          // Already torn down.
        }
        if (ownsContext) {
          void ctx?.close();
        }
      },
    };
  } catch {
    return null;
  }
}
