import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles/console.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Missing #root container");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
