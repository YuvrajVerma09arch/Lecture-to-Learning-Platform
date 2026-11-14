/**
 * The shell: the two screens, the lecture they are about, and the
 * navigation between them. The URL holds the selected lecture, so a
 * reload or a shared link lands on the same place.
 */
import { lazy, Suspense, useEffect } from "react";
import { api } from "./api";
import { AppHeader } from "./components/AppHeader";
import { LectureBar } from "./components/LectureBar";
import { ErrorState } from "./components/States";
import { useAsync } from "./hooks/useAsync";
import { lectureParam, useRoute } from "./lib/router";
import { Flashcards } from "./pages/Flashcards";
import { LoginScreen } from "./pages/LoginScreen";
import { Notes } from "./pages/Notes";
import { Upload } from "./pages/Upload";

// The chat screen pulls in the LiveKit client, which is a large
// download on a phone. It is loaded only when the student asks a doubt.
const Chat = lazy(() =>
  import("./pages/Chat").then((module) => ({ default: module.Chat })),
);

const NAV = [
  { path: "/notes", label: "Notes" },
  { path: "/flashcards", label: "Flashcards" },
  { path: "/chat", label: "Doubt chat" },
  { path: "/upload", label: "Upload" },
];

const SCREENS = ["/notes", "/flashcards", "/chat", "/upload"];

/** A lecture is only finished once it has stopped moving. */
const isSettled = (status: string) => status === "ready" || status === "failed";

export function App() {
  const [route, navigate] = useRoute();
  // The boot probe: who we are, and whether Google sign-in is configured.
  const auth = useAsync(() => api.me(), []);
  const lectures = useAsync(() => api.lectures().then((r) => r.lectures), []);

  const list = lectures.data ?? [];
  const requested = lectureParam(route);
  const selectedId =
    requested ??
    list.find((lecture) => lecture.status === "ready")?.id ??
    list[0]?.id ??
    null;

  const path: string = SCREENS.includes(route.path) ? route.path : "/notes";

  // A bare URL has no screen of its own; send it to the notes.
  useEffect(() => {
    if (route.path !== "/" && !SCREENS.includes(route.path)) {
      const lecture = route.params.get("lecture");
      navigate("/notes", lecture ? { lecture } : undefined);
    }
  }, [route, navigate]);

  // While any lecture is still moving, poll the list so its status in
  // the bar climbs from "processing" to "ready" without a reload. A
  // refresh, not a reload: the screen must not flicker every few seconds.
  const processing = list.some((lecture) => !isSettled(lecture.status));
  useEffect(() => {
    if (!processing) return;
    const timer = window.setInterval(() => lectures.refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [processing, lectures.refresh]);

  const selectLecture = (id: string) => {
    navigate(path, { lecture: id });
  };

  const signOut = async () => {
    await api.logout();
    window.location.assign("/");
  };

  // The upload returns before any work starts, so the student goes
  // straight to the notes, where the new lecture's stages are shown.
  const uploaded = () => {
    lectures.reload();
    navigate("/notes");
  };

  // Sign-in is only a gate when it is configured: in open mode `user` is
  // null forever, and blocking on it would never let anyone in. While
  // the probe is in flight nothing is shown, so the app never flashes
  // for a moment before the sign-in screen takes over.
  if (auth.status === "loading") {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-canvas">
        <p role="status" className="text-sm text-muted">
          Loading Lecture to Learning
        </p>
      </main>
    );
  }
  // A failed probe falls through: the app still renders, and every other
  // screen reports its own connection errors.
  if (auth.data?.google_oauth && !auth.data.user) {
    return <LoginScreen />;
  }

  return (
    <div className="min-h-dvh bg-canvas">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-white"
      >
        Skip to the main content
      </a>
      <AppHeader
        currentPath={path}
        items={NAV}
        onNavigate={(target) =>
          navigate(target, selectedId ? { lecture: selectedId } : undefined)
        }
        user={auth.data?.user ?? null}
        onLogout={signOut}
      />
      <LectureBar
        lectures={list}
        selectedId={selectedId}
        onSelect={selectLecture}
        loading={lectures.status === "loading"}
      />

      {lectures.status === "error" && lectures.error ? (
        <main className="mx-auto max-w-5xl px-4 py-6">
          <ErrorState error={lectures.error} onRetry={lectures.reload} />
        </main>
      ) : path === "/chat" ? (
        <Suspense
          fallback={
            <main className="mx-auto max-w-3xl px-4 py-6">
              <p role="status" className="text-sm text-muted">
                Loading the doubt chat
              </p>
            </main>
          }
        >
          <Chat
            lectureId={selectedId}
            lectures={list}
            onNavigate={navigate}
          />
        </Suspense>
      ) : path === "/flashcards" ? (
        // Keyed on the lecture: switching lectures starts a fresh deck
        // instead of showing the previous lecture's cards.
        <Flashcards
          key={selectedId ?? "none"}
          lectureId={selectedId}
          lectures={list}
          onNavigate={navigate}
        />
      ) : path === "/upload" ? (
        <Upload lectures={list} onUploaded={uploaded} />
      ) : (
        <Notes
          lectureId={selectedId}
          lectures={list}
          route={route}
          onNavigate={navigate}
        />
      )}
    </div>
  );
}
