import React from "react";
import ReactDOM from "react-dom/client";
import { initI18n } from "./i18n";
import { AppRuntimeGuard } from "./components/AppRuntimeGuard";
import {
  captureError,
  initErrorReporter,
  markFrontendReady,
  recordFrontendStage,
} from "./utils/errorReporter";
import { setBootSplashStage } from "./utils/bootSplash";
import { purgeLegacyAccountCaches } from "./utils/accountCachePrivacy";

const isClaudePreview = new URLSearchParams(window.location.search).get('preview') === 'claude';

if (!isClaudePreview) {
  purgeLegacyAccountCaches();
  initErrorReporter();
  recordFrontendStage("script_loaded");
  setBootSplashStage("script_loaded");
  void initI18n();
}

void (async () => {
  const { default: App } = isClaudePreview
    ? await import('./ClaudePreviewApp')
    : await import("./LocalApp");

  const rootElement = document.getElementById("root");
  if (!rootElement) {
    const error = new Error("Root element not found");
    captureError(error, { source: "frontend_boot", phase: "root_lookup" });
    throw error;
  }

  if (isClaudePreview) {
    ReactDOM.createRoot(rootElement).render(<React.StrictMode><App /></React.StrictMode>);
    document.getElementById('app-boot-splash')?.remove();
    return;
  }
  recordFrontendStage("react_mount_start");
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <AppRuntimeGuard>
        <App />
      </AppRuntimeGuard>
    </React.StrictMode>,
  );

  window.requestAnimationFrame(() => {
    setBootSplashStage("react_mounted");
    markFrontendReady("react_mounted");
  });
})();
