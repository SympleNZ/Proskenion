/*
 * Whether the document is currently visible. Used to stop the derived-status
 * monitor's server-sent events connection, and the rule-state poll, when
 * this admin screen is left open on a background tab — an appliance should
 * not keep doing work for a tab nobody is looking at.
 */
import { useEffect, useState } from "react";

export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => (typeof document === "undefined" ? true : !document.hidden));
  useEffect(() => {
    const onChange = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}
