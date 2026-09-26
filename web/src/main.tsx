import "./styles/index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AppToaster } from "./notifications/AppToaster";
import { registerServiceWorker } from "./pwa/serviceWorker";
import { SessionProvider } from "./session/SessionProvider";

/*
 * TanStack Query carries configuration data only — scene lists, the KNX
 * library, device configuration. Live state never goes through it (§21.2).
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Missing #root");

createRoot(rootElement).render(
  <StrictMode>
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <App />
          <AppToaster />
        </SessionProvider>
      </QueryClientProvider>
    </BrowserRouter>
  </StrictMode>,
);

// The offline shell (§21.28); best-effort, production builds only, never throws.
void registerServiceWorker();
