/*
 * Admin → System → Logs (spec §21.24): "Three tabs: Scene Execution,
 * Security and System." Scene Execution is the existing viewer
 * (`GET /scenes/log`, already built for §21.16), reached with a link rather
 * than duplicated. Security is the `security_events` viewer (§6.14). System
 * is the raw structured log file — `GET /system/logs` and its export
 * (§16.7, §4.10) — with a level filter, a module filter, a date range and
 * an export, closing the gap `docs/handover/operations.md` "Findings" #1
 * used to record. DEBUG toggling (§4.10) is not a log itself, so it sits
 * below all three tabs rather than inside any one of them.
 */
import { useState } from "react";

import { LogViewer } from "@/admin/scenes/LogViewer";
import { Button } from "@/components/ui/Button";

import { DebugLoggingCard } from "./DebugLoggingCard";
import { SecurityLogViewer } from "./SecurityLogViewer";
import { SystemLogViewer } from "./SystemLogViewer";

type TabKey = "scene-execution" | "security" | "system";

const TABS: readonly { key: TabKey; label: string }[] = [
  { key: "scene-execution", label: "Scene Execution" },
  { key: "security", label: "Security" },
  { key: "system", label: "System" },
];

export function LogsScreen() {
  const [tab, setTab] = useState<TabKey>("scene-execution");
  const [sceneLogOpen, setSceneLogOpen] = useState(false);

  return (
    <div className="view logs-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Logs</h1>
          <p className="view-lede">
            Scene execution, the security audit trail and the raw application log, retained 90 days (§4.10, §6.14, §8.16).
          </p>
        </div>
      </header>

      <div className="tab-strip" role="tablist" aria-label="Logs sections">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            id={`logs-tab-${item.key}`}
            aria-selected={tab === item.key}
            aria-controls={`logs-panel-${item.key}`}
            className="tab"
            data-state={tab === item.key ? "active" : "inactive"}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id="logs-panel-scene-execution"
        aria-labelledby="logs-tab-scene-execution"
        hidden={tab !== "scene-execution"}
      >
        {tab === "scene-execution" ? (
          <>
            <p className="field-help">Every scene run, with per-action detail, in the same viewer the Scenes screen uses.</p>
            <Button onClick={() => setSceneLogOpen(true)}>View scene execution log</Button>
            <LogViewer open={sceneLogOpen} onOpenChange={setSceneLogOpen} sceneId={undefined} />
          </>
        ) : null}
      </div>

      <div role="tabpanel" id="logs-panel-security" aria-labelledby="logs-tab-security" hidden={tab !== "security"}>
        {tab === "security" ? <SecurityLogViewer /> : null}
      </div>

      <div role="tabpanel" id="logs-panel-system" aria-labelledby="logs-tab-system" hidden={tab !== "system"}>
        {tab === "system" ? <SystemLogViewer /> : null}
      </div>

      <DebugLoggingCard />
    </div>
  );
}
