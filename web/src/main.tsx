import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Self-hosted scripts, so Devanagari and Gujarati render without a CDN.
import "@fontsource/noto-sans-devanagari/400.css";
import "@fontsource/noto-sans-devanagari/600.css";
import "@fontsource/noto-sans-gujarati/400.css";
import "@fontsource/noto-sans-gujarati/600.css";
// The pixel faces, self-hosted so the CSP's `font-src 'self'` still
// passes: Press Start 2P for display, Silkscreen for headings and
// labels, VT323 for body copy (the only one legible at reading sizes).
import "@fontsource/press-start-2p/400.css";
import "@fontsource/silkscreen/400.css";
import "@fontsource/silkscreen/700.css";
import "@fontsource/vt323/400.css";
import "katex/dist/katex.min.css";

import "./index.css";
import { App } from "./App";

const container = document.getElementById("root");
if (!container) {
  throw new Error("The page has no #root element to mount into.");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
