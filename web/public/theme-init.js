/*
 * Runs before the first paint so a saved dark theme never flashes the
 * light one. Kept as a file (not an inline script) because the backend's
 * CSP is `script-src 'self'`. The React side of the toggle only reads
 * and writes what this script applies.
 *
 * `?theme=dark` / `?theme=light` forces a theme for one page load
 * without writing storage — handy for screenshots and demos.
 */
(function () {
  try {
    var forced = new URLSearchParams(window.location.search).get("theme");
    var saved = localStorage.getItem("theme");
    var dark =
      forced === "dark" ||
      (forced !== "light" &&
        (saved === "dark" ||
          (saved !== "light" &&
            window.matchMedia("(prefers-color-scheme: dark)").matches)));
    if (dark) document.documentElement.classList.add("dark");
  } catch (e) {
    // Private mode without storage: fall back to the light theme.
  }
})();
