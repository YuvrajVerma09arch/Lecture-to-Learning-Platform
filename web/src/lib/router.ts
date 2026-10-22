/**
 * A very small router.
 *
 * The app has two screens and no third-party router, so the URL is the
 * only state that has to survive a reload: the path picks the screen
 * and the query string carries the selected lecture and the source
 * timestamp the reader was asked to jump to.
 */
import { useCallback, useEffect, useState } from "react";

export interface Route {
  path: string;
  params: URLSearchParams;
}

function read(): Route {
  return {
    path: window.location.pathname,
    params: new URLSearchParams(window.location.search),
  };
}

export function useRoute(): [Route, (to: string, params?: Record<string, string>) => void] {
  const [route, setRoute] = useState<Route>(read);

  useEffect(() => {
    const onPop = () => setRoute(read());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback(
    (to: string, params?: Record<string, string>) => {
      const search = new URLSearchParams(params ?? {}).toString();
      const url = `${to}${search ? `?${search}` : ""}`;
      if (`${window.location.pathname}${window.location.search}` === url) return;
      window.history.pushState({}, "", url);
      setRoute(read());
      window.scrollTo({ top: 0 });
    },
    [],
  );

  return [route, navigate];
}

/** The lecture id in the URL, if there is one. */
export function lectureParam(route: Route): string | null {
  return route.params.get("lecture");
}
