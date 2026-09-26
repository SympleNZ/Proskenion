/*
 * The inline help system (spec §19.1 "Inline help is written for every new
 * admin control"). One entry per control, keyed by a stable id, so the text
 * lives apart from the components that show it and can be reviewed in one
 * place (`web/src/help/content/`).
 */
export interface HelpEntry {
  /** The control's name, as a screen reader announces it ahead of the body. */
  term: string;
  /** What the control does and when you would change it — not how it is built. */
  body: string;
}
