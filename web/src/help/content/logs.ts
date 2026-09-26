/* Logs (spec §21.24). Three tabs: Scene Execution, Security, System. */
import type { HelpEntry } from "../types";

export const logs = {
  "logs.security.event-type": {
    term: "Event type",
    body: "Shows only this kind of security event. Does not change what is recorded, only what is shown.",
  },
  "logs.security.outcome": {
    term: "Outcome",
    body: "Shows only successes, or only failures.",
  },
  "logs.security.ip": {
    term: "Client IP",
    body: "Shows only events from this address.",
  },
  "logs.security.from": {
    term: "From",
    body: "The earliest event to show.",
  },
  "logs.security.to": {
    term: "To",
    body: "The latest event to show.",
  },
  "logs.system.level": {
    term: "Level",
    body: "Shows this level and everything more severe — WARNING also shows ERROR and CRITICAL.",
  },
  "logs.system.module": {
    term: "Module",
    body: "Shows only this logger and the loggers below it, e.g. proskenion.core matches proskenion.core.mixer.",
  },
  "logs.system.from": {
    term: "From",
    body: "The earliest log line to show.",
  },
  "logs.system.to": {
    term: "To",
    body: "The latest log line to show.",
  },
  "logs.debug-logging": {
    term: "Debug logging",
    body: "Turns on the more detailed DEBUG level for one module at a time, live, with no restart — and reverts the same way. INFO is the default; DEBUG can be noisy, so turn a module back off once you're done with it.",
  },
} as const satisfies Record<string, HelpEntry>;
