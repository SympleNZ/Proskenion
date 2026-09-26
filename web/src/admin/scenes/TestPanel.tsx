/*
 * "Test whole scene" and its result (spec §21.16). Respects delays; the
 * backend runs the whole thing before answering (there is no live streaming
 * transport for this in Phase 2), so the per-action rows appear together once
 * the run completes. The critical warning gates the request itself, exactly
 * as it must gate a "Test group" button elsewhere in the timeline (§8.14).
 */
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { presentError } from "@/api/errors";

import { useTestScene } from "./api";
import { DOMAIN_LABELS, type Domain, type RunResult } from "./types";
import { useCriticalTestGate } from "./useCriticalTestGate";

export interface TestPanelProps {
  sceneId: number;
  sceneName: string;
  critical: boolean;
  result: RunResult | null;
  onResult: (result: RunResult) => void;
}

function domainLabel(domain: string): string {
  return DOMAIN_LABELS[domain as Domain] ?? domain;
}

export function ActionResultList({ result }: { result: RunResult }) {
  const rows = [...result.actions].sort((a, b) => a.delay_ms - b.delay_ms || a.sort_order - b.sort_order);
  return (
    <div className="test-result" role="status" aria-live="polite" data-outcome={result.result === "failed" ? "failed" : "passed"}>
      <ul className="test-stages">
        {rows.map((report) => (
          <li key={report.action_id} className="test-stage" data-outcome={report.result === "failed" ? "failed" : report.result === "sent" || report.result === "confirmed" ? "passed" : "not-attempted"}>
            <span className="test-stage-icon" aria-hidden="true">
              {report.marker}
            </span>
            <span className="test-stage-name">
              {report.delay_ms} ms · {domainLabel(report.domain)}
            </span>
            <span className="test-stage-detail">{report.reason ?? report.result}</span>
          </li>
        ))}
      </ul>
      <p className="test-summary">
        {result.result[0]?.toUpperCase()}
        {result.result.slice(1)} · {(result.duration_ms / 1000).toFixed(1)} s
      </p>
    </div>
  );
}

export function TestPanel({ sceneId, sceneName, critical, result, onResult }: TestPanelProps) {
  const test = useTestScene();
  const gate = useCriticalTestGate(critical);

  function run() {
    test.mutate(sceneId, { onSuccess: onResult, onError: (error) => void presentError(error) });
  }

  return (
    <div className="scene-timeline">
      <Button type="button" variant="secondary" loading={test.isPending} onClick={() => gate.requestRun(run)}>
        Test whole scene
      </Button>
      {result ? <ActionResultList result={result} /> : null}
      <ConfirmDialog
        open={gate.open}
        onOpenChange={(open) => {
          if (!open) gate.cancel();
        }}
        title={`Test "${sceneName}"?`}
        description="This is a critical scene. Testing will cancel any running scenes and disable external control."
        confirmLabel="Continue"
        destructive
        onConfirm={gate.confirm}
      />
    </div>
  );
}
