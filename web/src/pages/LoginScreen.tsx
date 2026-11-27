/**
 * The sign-in screen, shown only when Google sign-in is configured and
 * no session has come back yet. It is a full-page jump to Google, not a
 * popup, so it works the same on a phone as on a laptop.
 *
 * It is also the app's attract screen: a synthwave console boot, a
 * marquee of what the product does, a pair of CSS pixel sprites, and a
 * blinking "press start" prompt over the real sign-in link.
 *
 * A failed callback lands here with `?error=<code>`; the code is mapped
 * to a sentence the student can act on, and then dropped from the URL so
 * a reload does not replay it.
 */
import { useEffect } from "react";
import { api } from "../api";

const ERROR_MESSAGES: Record<string, string> = {
  access_denied: "You didn't finish signing in with Google. Try again when you're ready.",
  state_mismatch: "That sign-in link expired. Please start again.",
  signin_failed: "Google couldn't sign you in. Please try again.",
};

const MARQUEE =
  "★ TRANSCRIBE ANY LECTURE ★ GENERATE STUDY NOTES ★ BUILD FLASHCARD DECKS ★ ASK YOUR VOICE TUTOR ★ ANSWERS GROUNDED IN THE RECORDING ★";

/**
 * A cassette sprite, drawn as inline SVG rectangles on a 10x6 cell grid
 * scaled up with `shape-rendering: crispEdges`. Inline so the CSP never
 * sees an image request; crisp edges keep it pixelated.
 */
function CassetteSprite() {
  const cell = 8;
  const pixels: Array<[number, number, string]> = [];
  const shell = "var(--color-accent)";
  const label = "var(--color-on-accent)";
  const reel = "var(--color-canvas)";
  // Outer shell.
  for (let x = 0; x < 10; x++) {
    pixels.push([x, 0, shell], [x, 5, shell]);
  }
  for (let y = 1; y < 5; y++) {
    pixels.push([0, y, shell], [9, y, shell]);
  }
  // Label band.
  for (let x = 1; x < 9; x++) pixels.push([x, 1, label], [x, 2, label]);
  // Reel windows.
  pixels.push([2, 3, reel], [3, 3, reel], [6, 3, reel], [7, 3, reel]);
  pixels.push([2, 4, label], [3, 4, label], [6, 4, label], [7, 4, label]);
  return (
    <svg
      aria-hidden="true"
      className="float"
      width={cell * 10}
      height={cell * 6}
      viewBox={`0 0 ${cell * 10} ${cell * 6}`}
      style={{ shapeRendering: "crispEdges" }}
    >
      {pixels.map(([x, y, fill]) => (
        <rect key={`${x}-${y}`} x={x * cell} y={y * cell} width={cell} height={cell} fill={fill} />
      ))}
    </svg>
  );
}

/** A 1-up sprout sprite, inline SVG on a 10x7 grid. */
function SproutSprite() {
  const cell = 8;
  const cap = "var(--color-cyan)";
  const stem = "var(--color-on-accent)";
  const glow = "var(--color-cyan)";
  const pixels: Array<[number, number, string]> = [];
  // Cap.
  for (let x = 2; x < 8; x++) pixels.push([x, 0, cap], [x, 6, cap]);
  for (let x = 1; x < 9; x++) pixels.push([x, 1, cap], [x, 5, cap]);
  // Spots.
  pixels.push([3, 2, stem], [6, 2, stem], [4, 3, stem], [5, 3, stem]);
  for (let x = 1; x < 9; x++) pixels.push([x, 4, cap]);
  // Stem.
  pixels.push([4, 7, glow], [5, 7, glow]);
  return (
    <svg
      aria-hidden="true"
      className="float"
      style={{ shapeRendering: "crispEdges", animationDelay: "0.8s" }}
      width={cell * 10}
      height={cell * 8}
      viewBox={`0 0 ${cell * 10} ${cell * 8}`}
    >
      {pixels.map(([x, y, fill]) => (
        <rect key={`${x}-${y}`} x={x * cell} y={y * cell} width={cell} height={cell} fill={fill} />
      ))}
    </svg>
  );
}

function stripErrorParam() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("error")) return;
  params.delete("error");
  const search = params.toString();
  window.history.replaceState(
    {},
    "",
    `${window.location.pathname}${search ? `?${search}` : ""}`,
  );
}

export function LoginScreen() {
  // Where to come back to: the screen the student asked for, minus the
  // error code we are about to clear.
  useEffect(() => {
    stripErrorParam();
  }, []);

  const params = new URLSearchParams(window.location.search);
  const code = params.get("error");
  const message = code
    ? (ERROR_MESSAGES[code] ?? "Sign-in didn't work. Please try again.")
    : null;
  // Come back to the same screen, without the error code riding along.
  params.delete("error");
  const search = params.toString();
  const next = `${window.location.pathname}${search ? `?${search}` : ""}`;

  return (
    <main className="relative flex min-h-dvh flex-col items-center justify-center px-4 py-10">
      <div className="app-bg" aria-hidden="true" />
      <div className="crt-overlay" aria-hidden="true" />

      <div className="relative z-10 w-full max-w-lg">
        {/* Marquee ticker above the console. */}
        <p className="marquee mb-6 border-y-2 border-strong-line bg-canvas-deep py-1 font-serif text-[0.625rem] font-bold uppercase tracking-[0.18em] text-muted">
          <span>{MARQUEE}&nbsp;&nbsp;&nbsp;{MARQUEE}</span>
        </p>

        {/* The console panel — a chunky NES dialog box. */}
        <div className="card px-5 py-7 sm:px-8 sm:py-9">
          <div className="flex items-center justify-between gap-3">
            <CassetteSprite />
            <SproutSprite />
          </div>

          <p className="eyebrow mt-5 text-accent-ink">1-UP · PRESS START</p>
          <h1 className="pixel-display glow mt-3 text-lg leading-relaxed text-ink sm:text-xl">
            LECTURE
            <br />
            TO LEARNING
          </h1>
          <p className="mt-4 text-base leading-relaxed text-body sm:text-lg">
            Turn any lecture recording into study notes, flashcards and a
            voice tutor that answers only from what was said.
          </p>

          {message && (
            <p
              role="alert"
              className="well mt-5 border-l-4 border-l-warn px-3 py-2 text-sm text-warn"
            >
              {message}
            </p>
          )}

          <a
            href={api.login(next)}
            className="btn btn-primary mt-6 w-full py-3 text-sm"
          >
            <span aria-hidden="true">▶</span> Continue with Google
          </a>

          <p className="mt-3 text-center font-serif text-[0.625rem] font-bold uppercase tracking-[0.12em] text-muted">
            <span className="blink text-accent">▮</span> no password · google
            sign-in only
          </p>

          <p className="mt-4 text-center text-xs text-muted">
            Your account only stores your name and email from Google. Nothing
            else is shared.
          </p>
        </div>

        {/* A little status bar, like a console's footer. */}
        <ul className="mt-4 grid grid-cols-3 gap-2 text-center font-serif text-[0.5625rem] font-bold uppercase tracking-[0.1em] text-muted">
          <li className="well px-1 py-2">Notes</li>
          <li className="well px-1 py-2">Flashcards</li>
          <li className="well px-1 py-2">Voice tutor</li>
        </ul>
      </div>
    </main>
  );
}
