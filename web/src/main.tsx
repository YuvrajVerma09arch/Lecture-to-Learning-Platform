import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Self-hosted scripts, so Devanagari and Gujarati render without a CDN.
import "@fontsource/noto-sans-devanagari/400.css";
import "@fontsource/noto-sans-devanagari/600.css";
import "@fontsource/noto-sans-gujarati/400.css";
import "@fontsource/noto-sans-gujarati/600.css";
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
