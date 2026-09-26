/*
 * HSV ⇄ RGB (spec §21.18's fixture sheet, §9.1: "The picker presents HSV but
 * stores and transmits RGB, matching DMX reality with no conversion layer in
 * the protocol path"). The conversion lives here, at the edge of the UI, and
 * nowhere near a protocol client — `w` (white) has no HSV analogue and is
 * carried alongside rather than folded into the model.
 */

export interface Hsv {
  /** Degrees, 0–360. */
  h: number;
  /** Percent, 0–100. */
  s: number;
  /** Percent, 0–100. */
  v: number;
}

export interface Rgb {
  /** 0–255 (§9.2's one exception to the 0–100 scale). */
  r: number;
  g: number;
  b: number;
}

export function hsvToRgb({ h, s, v }: Hsv): Rgb {
  const hue = ((h % 360) + 360) % 360;
  const sat = Math.max(0, Math.min(100, s)) / 100;
  const val = Math.max(0, Math.min(100, v)) / 100;
  const c = val * sat;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = val - c;
  let rp: number;
  let gp: number;
  let bp: number;
  if (hue < 60) [rp, gp, bp] = [c, x, 0];
  else if (hue < 120) [rp, gp, bp] = [x, c, 0];
  else if (hue < 180) [rp, gp, bp] = [0, c, x];
  else if (hue < 240) [rp, gp, bp] = [0, x, c];
  else if (hue < 300) [rp, gp, bp] = [x, 0, c];
  else [rp, gp, bp] = [c, 0, x];
  return {
    r: Math.round((rp + m) * 255),
    g: Math.round((gp + m) * 255),
    b: Math.round((bp + m) * 255),
  };
}

export function rgbToHsv({ r, g, b }: Rgb): Hsv {
  const rp = Math.max(0, Math.min(255, r)) / 255;
  const gp = Math.max(0, Math.min(255, g)) / 255;
  const bp = Math.max(0, Math.min(255, b)) / 255;
  const max = Math.max(rp, gp, bp);
  const min = Math.min(rp, gp, bp);
  const delta = max - min;

  let h = 0;
  if (delta !== 0) {
    if (max === rp) h = 60 * (((gp - bp) / delta) % 6);
    else if (max === gp) h = 60 * ((bp - rp) / delta + 2);
    else h = 60 * ((rp - gp) / delta + 4);
  }
  if (h < 0) h += 360;

  const s = max === 0 ? 0 : (delta / max) * 100;
  const v = max * 100;

  return { h: Math.round(h), s: Math.round(s), v: Math.round(v) };
}
