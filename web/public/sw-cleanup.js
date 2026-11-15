/*
 * Drops any service worker left behind by an older build of this frontend.
 *
 * The old worker cached a stale page on this origin (white screen in Safari
 * that asked for /src/main.jsx). This app does not use service workers, so
 * anything registered here is a leftover: unregister it silently. The next
 * load then comes straight from the network. A second, independent path —
 * public/sw.js — handles the harder case where the stale worker serves the
 * cached page and this file never gets to run.
 */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then(
    function (registrations) {
      registrations.forEach(function (registration) {
        registration.unregister();
      });
    },
    function () {
      /* No service worker API or a private window: nothing to clean. */
    }
  );
}
