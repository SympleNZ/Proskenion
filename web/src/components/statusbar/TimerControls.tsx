/*
 * Shared timer controls (spec §21.7): start, stop, reset; H:MM:SS in the mono
 * face. Operator and admin only. The timer is the server's (§16): each button
 * posts to it and the display follows the `timer` frame every client
 * receives. Elapsed is counted against the appliance's clock, so two tablets
 * whose own clocks disagree still show one number. Controls are disabled
 * while the socket is down (§21.26 "WebSocket disconnected") and while a
 * press is in flight.
 */
import { Pause, Play, RotateCcw } from "lucide-react";

import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { useControlsEnabled } from "@/live/store";
import { elapsedMs, useTimer, useTimerAction, type TimerAction } from "@/live/timer";
import { formatElapsed } from "@/lib/time";
import { useNow } from "@/lib/useNow";
import { useSession } from "@/session/context";

export function TimerControls() {
  const timer = useTimer();
  const now = useNow();
  const { serverTimeOffset } = useSession();
  const enabled = useControlsEnabled();
  const action = useTimerAction();
  const elapsed = formatElapsed(elapsedMs(timer, now + serverTimeOffset));
  const disabled = !enabled || action.isPending;

  const press = (which: TimerAction) => action.mutate(which, { onError: (error) => presentError(error) });

  return (
    <div className="timer" role="group" aria-label="Timer" aria-busy={action.isPending}>
      <Button
        variant="ghost"
        size="icon"
        aria-label={timer.running ? "Stop timer" : "Start timer"}
        aria-pressed={timer.running}
        disabled={disabled}
        onClick={() => press(timer.running ? "stop" : "start")}
      >
        {timer.running ? <Pause aria-hidden="true" className="size-5" /> : <Play aria-hidden="true" className="size-5" />}
      </Button>
      <output className="timer-display" data-running={timer.running} aria-label="Elapsed time" aria-live="off">
        {elapsed}
      </output>
      <Button variant="ghost" size="icon" aria-label="Reset timer" disabled={disabled} onClick={() => press("reset")}>
        <RotateCcw aria-hidden="true" className="size-5" />
      </Button>
    </div>
  );
}
