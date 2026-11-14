/**
 * The sign-in screen, shown only when Google sign-in is configured and
 * no session has come back yet. It is a full-page jump to Google, not a
 * popup, so it works the same on a phone as on a laptop.
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
    <main className="flex min-h-dvh flex-col items-center justify-center bg-canvas px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-5 flex items-center gap-2 text-ink">
          <span aria-hidden="true" className="h-4 w-4 bg-accent" />
          <span className="font-serif text-lg font-semibold tracking-tight">
            Lecture to Learning
          </span>
        </div>

        <div className="card px-6 py-8">
          <p className="eyebrow">Sign in</p>
          <h1 className="mt-2 font-serif text-2xl font-semibold leading-tight tracking-tight text-ink">
            Turn a lecture into something you can study.
          </h1>
          <p className="mt-3 text-sm text-muted">
            Notes, flashcards and a voice tutor that answers only from what
            was said in the recording.
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
            className="btn btn-primary mt-6 w-full py-2.5"
          >
            Continue with Google
          </a>

          <p className="mt-4 text-xs text-muted">
            Your account only stores your name and email from Google. Nothing
            else is shared.
          </p>
        </div>
      </div>
    </main>
  );
}
