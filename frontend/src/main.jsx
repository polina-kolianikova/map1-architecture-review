import React from "react";
import { createRoot } from "react-dom/client";
import "mapbox-gl/dist/mapbox-gl.css";
import "./styles.css";
import { App } from "./App.jsx";
import { ACTIVE_LOCALE } from "./hooks/useI18n";

document.documentElement.lang = ACTIVE_LOCALE;

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
