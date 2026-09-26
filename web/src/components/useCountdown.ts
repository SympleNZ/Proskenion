/*
 * Rate-limit countdown (spec §16.1, §21.8): a live countdown replaces the
 * control's label and it re-enables at zero.
 */
import { useCallback, useEffect, useState } from "react";

export interface Countdown {
  active: boolean;
  /** Whole seconds remaining. */
  remaining: number;
  start(seconds: number): void;
  clear(): void;
}

export function useCountdown(): Countdown {
  const [deadline, setDeadline] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);

  useEffect(() => {
    if (deadline === null) return undefined;
    const update = () => {
      const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      setRemaining(left);
      if (left === 0) setDeadline(null);
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [deadline]);

  const start = useCallback((seconds: number) => {
    const s = Math.max(0, Math.ceil(seconds));
    if (s === 0) return;
    setRemaining(s);
    setDeadline(Date.now() + s * 1000);
  }, []);

  const clear = useCallback(() => {
    setDeadline(null);
    setRemaining(0);
  }, []);

  return { active: deadline !== null && remaining > 0, remaining, start, clear };
}
