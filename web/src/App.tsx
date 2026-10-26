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
import { Notes } from "./pages/Notes";

// The chat screen pulls in the LiveKit client, which is a large
// download on a phone. It is loaded only when the student asks a doubt.
const Chat = lazy(() =>
  import("./pages/Chat").then((module) => ({ default: module.Chat })),
);

const NAV = [
  { path: "/notes", label: "Notes" },
  { path: "/chat", label: "Doubt chat" },
];

export function App() {
  const [route, navigate] = useRoute();
  const lectures = useAsync(() => api.lectures().then((r) => r.lectures), []);

  const list = lectures.data ?? [];
  const requested = lectureParam(route);
  const selectedId =
    requested ??
    list.find((lecture) => lecture.status === "ready")?.id ??
    list[0]?.id ??
    null;

  const path = route.path === "/chat" ? "/chat" : "/notes";

  // A bare URL has no screen of its own; send it to the notes.
  useEffect(() => {
    if (route.path !== "/" && route.path !== "/notes" && route.path !== "/chat") {
      navigate("/notes", route.params.get("lecture") ? { lecture: route.params.get("lecture") as string } : undefined);
    }
  }, [route, navigate]);

  const selectLecture = (id: string) => {
    navigate(path, { lecture: id });
  };

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
