/*
 * The Display scale slider (spec §21.9): 1.0×–2.0× in tenths, rendered only
 * where it has authority — this component answers `null` itself rather than
 * relying on a caller to gate it, so the seam cannot be forgotten.
 */
import { useDisplayScale } from "./useDisplayScale";

export function DisplayScaleControl() {
  const { available, scale, setScale } = useDisplayScale();
  if (!available) return null;

  return (
    <div className="page-display-scale">
      <label htmlFor="page-display-scale-input">Display scale</label>
      <input
        id="page-display-scale-input"
        type="range"
        min={1}
        max={2}
        step={0.1}
        value={scale}
        onChange={(event) => setScale(Number(event.target.value))}
      />
      <span className="page-display-scale-value">{scale.toFixed(1)}×</span>
    </div>
  );
}
