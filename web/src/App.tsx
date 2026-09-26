/* Routes (spec §6.13). */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { BackupScreen } from "@/admin/backup/BackupScreen";
import { CertificatesScreen } from "@/admin/certs/CertificatesScreen";
import { DevicesScreen } from "@/admin/devices/DevicesScreen";
import { EmailScreen } from "@/admin/email/EmailScreen";
import { HdmiScreen } from "@/admin/hdmi/HdmiScreen";
import { HealthScreen } from "@/admin/health/HealthScreen";
import { HirerAccessScreen } from "@/admin/hirer/HirerAccessScreen";
import { KnxLibraryScreen } from "@/admin/knx/KnxLibraryScreen";
import { LightingScreen } from "@/admin/lighting/LightingScreen";
import { LogsScreen } from "@/admin/logs/LogsScreen";
import { MixerScreen } from "@/admin/mixer/MixerScreen";
import { NetworkScreen } from "@/admin/network/NetworkScreen";
import { storeArrivalToken, takeArrivalToken } from "@/admin/network/reconnect";
import { PagesScreen } from "@/admin/pages/PagesScreen";
import { RulesScreen } from "@/admin/rules/RulesScreen";
import { ScenesScreen } from "@/admin/scenes/ScenesScreen";
import { UpdatesScreen } from "@/admin/updates/UpdatesScreen";
import { UsersScreen } from "@/admin/users/UsersScreen";
import { HelpScreen } from "@/help/HelpScreen";
import { LightingView } from "@/lighting/LightingView";
import { ADMIN_ITEMS, OPERATOR_TABS } from "@/navigation";
import { LoginPage } from "@/pages/LoginPage";
import { AdminView, NotFoundPage, NotPermittedPage, OperatorView } from "@/pages/Placeholders";
import { startLiveSocket, stopLiveSocket } from "@/live/socket";
import { usePagesLiveInvalidation } from "@/live/usePagesLiveInvalidation";
import { startVersionWatch, stopVersionWatch } from "@/version/versionCheck";
import { MixerView } from "@/mixer/MixerView";
import { useOfflineSupport } from "@/offline/offlineSupport";
import { PagesView } from "@/pagesurface/PagesView";
import { ProjectorView } from "@/projector/ProjectorView";
import { RequireTier } from "@/routes/guards";
import { ScenesView } from "@/scenes/ScenesView";
import { useSession } from "@/session/context";
import { SetupWizard } from "@/setup/SetupWizard";
import { AdminShell } from "@/shells/AdminShell";
import { HirerShell } from "@/shells/HirerShell";
import { OperatorShell } from "@/shells/OperatorShell";
import { StagePlanView } from "@/stageplan/StagePlanView";
import { VideoView } from "@/video/VideoView";

/** Admin routes that have their own screen; the rest still render their empty state. */
const ADMIN_SCREENS: Readonly<Record<string, ReactNode>> = {
  backup: <BackupScreen />,
  certificates: <CertificatesScreen />,
  devices: <DevicesScreen />,
  email: <EmailScreen />,
  hdmi: <HdmiScreen />,
  health: <HealthScreen />,
  help: <HelpScreen />,
  "hirer-access": <HirerAccessScreen />,
  "knx-library": <KnxLibraryScreen />,
  lighting: <LightingScreen />,
  logs: <LogsScreen />,
  mixer: <MixerScreen />,
  network: <NetworkScreen />,
  pages: <PagesScreen />,
  rules: <RulesScreen />,
  scenes: <ScenesScreen />,
  updates: <UpdatesScreen />,
  users: <UsersScreen />,
};

/** Operator routes that have their own screen; the rest still render their empty state. */
const OPERATOR_SCREENS: Readonly<Record<string, ReactNode>> = {
  pages: <PagesView />,
  lighting: <LightingView />,
  "stage-plan": <StagePlanView />,
  scenes: <ScenesView />,
  mixer: <MixerView />,
  video: <VideoView />,
  projector: <ProjectorView />,
};

function OperatorEntry() {
  const { session } = useSession();
  return <OperatorShell tier={session?.tier ?? "operator"} />;
}

/**
 * The live socket for the signed-in surfaces (§21.2, §16.8). It opens once a
 * session exists and closes when it ends, so device status in the status bar
 * and on the Health screen is the live state store rather than a placeholder.
 * Live state is never TanStack Query and Query is never live state — except
 * `usePagesLiveInvalidation`, the one deliberate bridge, for `pages_changed`.
 */
function LiveConnection() {
  const { status, session } = useSession();
  const connected = status === "authenticated";
  usePagesLiveInvalidation();
  // §21.27's offline state: queries pause rather than fail while the socket
  // is down, mutations never queue, and last-known values are saved.
  useOfflineSupport(useQueryClient(), connected ? (session?.tier ?? null) : null);
  useEffect(() => {
    if (!connected) return undefined;
    startLiveSocket();
    startVersionWatch();
    return () => {
      stopLiveSocket();
      stopVersionWatch();
    };
  }, [connected]);
  return null;
}

export function App() {
  // §10.8, contracts §5 (wave 3): a confirm token arriving in the URL
  // fragment (reconnect.ts's own doc explains why the fragment) is captured
  // here, at the very top of the tree, before RequireTier below can redirect
  // to /login and drop it — sessionStorage survives that redirect and
  // whatever sign-in follows; the fragment itself would not. Idempotent by
  // construction (see takeArrivalToken): safe to run on every render.
  const arrivalToken = takeArrivalToken(window.location, window.history);
  if (arrivalToken) storeArrivalToken(window.sessionStorage, arrivalToken);

  return (
    <>
      <LiveConnection />
      <Routes>
        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/setup" element={<SetupWizard />} />

        <Route
          path="/app"
          element={
            <RequireTier allow={["admin", "operator"]}>
              <OperatorEntry />
            </RequireTier>
          }
        >
          <Route index element={<Navigate to="pages" replace />} />
          {OPERATOR_TABS.map((tab) => (
            <Route key={tab.path} path={tab.path} element={OPERATOR_SCREENS[tab.path] ?? <OperatorView tab={tab} />} />
          ))}
          <Route path="*" element={<Navigate to="pages" replace />} />
        </Route>

        <Route
          path="/admin"
          element={
            <RequireTier allow={["admin"]} denied={<NotPermittedPage />}>
              <AdminShell />
            </RequireTier>
          }
        >
          <Route index element={<Navigate to="scenes" replace />} />
          {ADMIN_ITEMS.map((item) => (
            <Route key={item.path} path={item.path} element={ADMIN_SCREENS[item.path] ?? <AdminView item={item} />} />
          ))}
          <Route path="*" element={<Navigate to="scenes" replace />} />
        </Route>

        <Route
          path="/hire/*"
          element={
            <RequireTier allow={["hirer"]}>
              <HirerShell />
            </RequireTier>
          }
        />

        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </>
  );
}
