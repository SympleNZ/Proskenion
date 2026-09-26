/*
 * Two-stage test reporting (spec §5.3, §21.24).
 *
 * A transport that opens but does not respond says "connected, but the device
 * did not reply" rather than a single pass or fail — for a serial device the
 * port opens whether or not anything is attached, so an open port is not
 * evidence.
 */
import { Check, Minus, X } from "lucide-react";

import { Skeleton } from "@/components/ui/EmptyState";

import { connectedWithoutReply, NO_REPLY, stageWord, STAGE_LABELS, STAGE_MEANING, type StageName } from "./testReport";
import type { TestReport, TestStage } from "./types";

function StageRow({ name, stage }: { name: StageName; stage: TestStage }) {
  const word = stageWord(stage);
  return (
    <li className="test-stage" data-outcome={word.toLowerCase().replace(/ /g, "-")}>
      <span className="test-stage-icon" aria-hidden="true">
        {!stage.attempted ? (
          <Minus className="size-4" strokeWidth={3} />
        ) : stage.ok ? (
          <Check className="size-4" strokeWidth={3} />
        ) : (
          <X className="size-4" strokeWidth={3} />
        )}
      </span>
      <span className="test-stage-name">{STAGE_LABELS[name]}</span>
      <span className="test-stage-word">{word}</span>
      <span className="test-stage-detail">{stage.detail ?? STAGE_MEANING[name]}</span>
    </li>
  );
}

export interface TestResultProps {
  report?: TestReport | undefined;
  pending?: boolean;
  /** A test that could not run at all — the request failed rather than the device. */
  failure?: string | undefined;
}

export function TestResult({ report, pending = false, failure }: TestResultProps) {
  if (pending) {
    return (
      <div className="test-result" role="status" aria-live="polite" aria-busy="true">
        <p>Connecting, then probing…</p>
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (failure) {
    return (
      <div className="test-result" role="status" aria-live="polite" data-outcome="failed">
        <p className="test-summary">{failure}</p>
      </div>
    );
  }

  if (!report) return null;

  const noReply = connectedWithoutReply(report.connect, report.probe);
  const summary = noReply ? NO_REPLY : report.message;

  return (
    <div className="test-result" role="status" aria-live="polite" data-outcome={report.ok ? "passed" : "failed"}>
      <p className="test-summary">{summary}</p>
      {noReply && report.message !== NO_REPLY ? <p className="test-detail">{report.message}</p> : null}
      <ul className="test-stages">
        <StageRow name="connect" stage={report.connect} />
        <StageRow name="probe" stage={report.probe} />
      </ul>
      {noReply ? (
        <p className="test-detail">
          The transport opened, so the address or path is right. Check the cable, the power and the device itself.
        </p>
      ) : null}
      {!report.connect.ok ? (
        <p className="test-detail">The transport could not be opened. Check the address, the path or the permissions.</p>
      ) : null}
    </div>
  );
}
