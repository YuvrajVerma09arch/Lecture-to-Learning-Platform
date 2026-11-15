import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies the API to the local FastAPI process, so the
// browser talks to one origin in development and in production alike.
// The backend also sets CORS for :5173, so a direct call would work too.
const API_TARGET = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
    },
    // Never cacheable: Safari once held a stale copy of this page for
    // localhost:5173 and rendered a white screen instead of the app.
    headers: {
      "Cache-Control": "no-store",
    },
  },
  build: {
    outDir: "dist",
    // The backend mounts /assets, so the bundle must land there.
    assetsDir: "assets",
    sourcemap: false,
  },
});
