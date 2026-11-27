/**
 * The upload screen: hand a recording to the pipeline.
 *
 * The backend validates the file before anything is queued -- readable
 * audio, long enough to be a lecture, small enough to fit -- and this
 * screen surfaces exactly what it says. A successful upload returns
 * before any work starts, so the student is sent back to the notes to
 * watch the stages instead of waiting on this form.
 */
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, api } from "../api";
import { SESSION_LANGUAGES } from "../lib/languages";
import type { Lecture } from "../types";

const ACCEPT = ".wav,.mp3,.mp4,.m4a,.aac,.flac,.ogg,.opus,.webm,.amr,audio/*,video/mp4";

/** `recursion-lecture.mp3` -> `Recursion lecture`. */
function titleFromFilename(name: string): string {
  const base = name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
  if (!base) return "";
  return base.charAt(0).toUpperCase() + base.slice(1);
}

export function Upload({
  lectures,
  onUploaded,
}: {
  lectures: Lecture[];
  onUploaded: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState("en-IN");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pick = (chosen: File | null) => {
    setFile(chosen);
    setError(null);
    if (chosen && !title.trim()) setTitle(titleFromFilename(chosen.name));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.upload(file, title.trim() || titleFromFilename(file.name), language);
      onUploaded();
    } catch (err) {
      // The backend's own sentence is the useful one; anything else
      // gets the generic message ApiError already carries.
      setError(
        err instanceof ApiError ? err.message : "The upload could not be sent.",
      );
      setBusy(false);
    }
  };

  const hasLectures = lectures.length > 0;

  return (
    <main className="mx-auto max-w-3xl px-4 py-6">
      <h1 className="font-serif text-xl font-bold tracking-[0.02em] text-ink">
        <span className="text-accent">▸</span> Upload a lecture
      </h1>
      <p className="mt-1 max-w-prose text-sm text-muted">
        Add a recording and it becomes notes, flashcards and a voice tutor
        that answers only from what was said. Audio and video files up to
        500 MB, at least 5 seconds long.
      </p>

      <form
        onSubmit={submit}
        className="card mt-4 px-4 py-5 sm:px-6"
      >
        <label className="eyebrow block" htmlFor="lecture-file">
          Recording
        </label>
        <input
          id="lecture-file"
          type="file"
          accept={ACCEPT}
          className="input mt-2 cursor-pointer file:mr-3 file:cursor-pointer file:border-2 file:border-strong-line file:bg-accent file:px-3 file:py-1.5 file:font-serif file:text-xs file:font-bold file:uppercase file:tracking-[0.08em] file:text-on-accent hover:file:bg-accent-hover"
          onChange={(event) => pick(event.target.files?.[0] ?? null)}
        />
        {file && (
          <p className="mt-2 font-mono text-xs text-muted">
            {file.name} · {(file.size / (1024 * 1024)).toFixed(1)} MB
          </p>
        )}

        <label className="eyebrow mt-5 block" htmlFor="lecture-title">
          Title
        </label>
        <input
          id="lecture-title"
          type="text"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Recursion lecture"
          maxLength={120}
          className="input mt-2"
        />

        <label className="eyebrow mt-5 block" htmlFor="lecture-language">
          Language of the recording
        </label>
        <select
          id="lecture-language"
          value={language}
          onChange={(event) => setLanguage(event.target.value)}
          className="input mt-2"
        >
          {SESSION_LANGUAGES.map((item) => (
            <option key={item.code} value={item.code}>
              {item.label} ({item.code})
            </option>
          ))}
        </select>
        <p className="mt-2 text-xs text-muted">
          The transcription runs in this language, so pick the one the
          lecturer actually spoke.
        </p>

        {error && (
          <p
            role="alert"
            className="well mt-5 border-l-4 border-l-warn px-3 py-2 text-sm text-warn"
          >
            {error}
          </p>
        )}

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <button type="submit" disabled={!file || busy} className="btn btn-primary px-4 py-2.5">
            {busy ? "Uploading…" : "Start the lecture"}
          </button>
          {busy && (
            <span role="status" className="text-sm text-muted">
              Sending the recording
            </span>
          )}
        </div>
      </form>

      {!hasLectures && !busy && (
        <p className="mt-4 text-sm text-muted">
          Your first lecture will appear in Notes as soon as it is accepted.
        </p>
      )}
    </main>
  );
}
