/*
 * A health level as colour *and* a word (spec §24.1, §11.2). Colour is never
 * the only signal, so the dot carries the appearance and the accessible name
 * carries the word.
 */
import { Circle, TriangleAlert, X } from "lucide-react";

import { cn } from "@/lib/utils";

import { LEVEL_APPEARANCE, LEVEL_WORDS, type Level } from "./levels";

export interface LevelDotProps {
  level: Level;
  /** What the dot describes, e.g. "CPU temperature". Becomes "CPU temperature: Healthy". */
  subject: string;
  size?: "sm" | "lg";
  className?: string;
}

export function LevelDot({ level, subject, size = "sm", className }: LevelDotProps) {
  const appearance = LEVEL_APPEARANCE[level];
  return (
    <span
      className={cn("status-dot", size === "lg" && "status-dot-lg", className)}
      data-status={appearance}
      data-level={level}
      role="img"
      aria-label={`${subject}: ${LEVEL_WORDS[level]}`}
    >
      {appearance === "degraded" && <TriangleAlert aria-hidden="true" strokeWidth={3} />}
      {appearance === "error" && <X aria-hidden="true" strokeWidth={3} />}
      {appearance === "unconfigured" && <Circle aria-hidden="true" strokeWidth={2.5} />}
    </span>
  );
}

/** The dot with its word written out, for places a label is not already beside it. */
export function LevelBadge({ level, subject, className }: LevelDotProps) {
  return (
    <span className={cn("level-badge", className)} data-level={level}>
      <LevelDot level={level} subject={subject} />
      <span>{LEVEL_WORDS[level]}</span>
    </span>
  );
}
