/*
 * Self-destructing service worker — a cleanup patch, not part of the app.
 *
 * An older frontend that used this origin (localhost:5173) registered a PWA
 * service worker. It is still alive in some browsers and keeps serving a
 * stale cached shell, so the page renders white and asks for files that no
 * longer exist (e.g. /src/main.jsx → 404).
 *
 * This app never calls serviceWorker.register(), so a fresh browser never
 * installs this file. Only a browser that ALREADY has a registration fetches
 * /sw.js for its periodic update check — and when it does, this worker
 * replaces the stale one and immediately unregisters itself, so every
 * subsequent load comes from the network.
 */
self.addEventListener("install", () => {
  // Activate on this visit instead of waiting for the next one.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      await self.registration.unregister();
      // Reload open tabs so they drop the cached shell. navigate() is not
      // everywhere yet; a failure just means the user reloads by hand.
      try {
        const windows = await self.clients.matchAll({ type: "window" });
        await Promise.all(
          windows.map((client) => {
            try {
              return client.navigate(client.url);
            } catch (_e) {
              return undefined;
            }
          })
        );
      } catch (_e) {
        /* ignore */
      }
    })()
  );
});

// Deliberately no fetch handler: with the stale worker gone, every request
// falls through to the network.
