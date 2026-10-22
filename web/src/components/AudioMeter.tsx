/**
 * An audio level bar driven by the real signal.
 *
 * The bar is filled from an `AnalyserNode` on the live track, so it
 * shows the level of the microphone or of the tutor's audio, not an
 * animation.
 */
import { useEffect, useRef, useState } from "react";
import type { LevelMeter } from "../lib/audio";

export function AudioMeter({
  meter,
  label,
  active,
}: {
  meter: LevelMeter | null;
  label: string;
  active: boolean;
}) {
  const fillRef = useRef<HTMLDivElement>(null);
  const [percent, setPercent] = useState(0);

  useEffect(() => {
    if (!meter) {
      setPercent(0);
      return;
    }
    let frame = 0;
    let lastUpdate = 0;
    const loop = (time: number) => {
      const level = meter.getLevel();
      if (fillRef.current) {
        fillRef.current.style.width = `${Math.round(level * 100)}%`;
      }
      if (time - lastUpdate > 120) {
        lastUpdate = time;
        setPercent(Math.round(level * 100));
      }
      frame = window.requestAnimationFrame(loop);
    };
    frame = window.requestAnimationFrame(loop);
    return () => window.cancelAnimationFrame(frame);
  }, [meter]);

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">
          {label}
        </span>
        <span aria-hidden="true" className="text-xs text-muted">
          {meter ? `${percent}%` : "no signal"}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${label} level`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        className="mt-1 h-2 w-full overflow-hidden rounded-full border border-line bg-canvas"
      >
        <div
          ref={fillRef}
          className={`h-full rounded-full ${active ? "bg-accent" : "bg-strong-line"}`}
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}
