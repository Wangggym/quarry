import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.tsx";
// Self-hosted (no external CDN dependency — matches the old GUI's icon set
// without its jsdelivr-CDN tech debt; see epic issue for #14).
import "@tabler/icons-webfont/dist/tabler-icons.min.css";
import "./App.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
