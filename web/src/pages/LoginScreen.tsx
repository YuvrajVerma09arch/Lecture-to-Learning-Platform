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
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-4 py-10">
      <div className="clay rounded-xl border border-line bg-white px-6 py-8">
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          Lecture to Learning
        </h1>
        <p className="mt-2 text-sm text-muted">
          Sign in to turn a lecture recording into notes, flashcards and a
          voice tutor that answers only from the lecture.
        </p>

        {message && (
          <p
            role="alert"
            className="clay-inset mt-4 px-3 py-2 text-sm text-body"
          >
            {message}
          </p>
        )}

        <a
          href={api.login(next)}
          className="clay-btn clay-accent mt-6 block w-full rounded-md px-4 py-2.5 text-center text-sm font-medium"
        >
          Continue with Google
        </a>

        <p className="mt-4 text-xs text-muted">
          Your account only stores your name and email from Google. Nothing
          else is shared.
        </p>
      </div>
    </main>
  );
}
