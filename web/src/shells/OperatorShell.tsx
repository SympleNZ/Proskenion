/*
 * Operator shell (spec §21.6): a tab strip below the brand on desktop and
 * tablet, a rail on a phone in landscape — never at the bottom. Pages is the
 * default landing.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";

import type { Tier } from "@/api/auth";
import { DeviceList } from "@/components/DeviceList";
import { OPERATOR_TABS } from "@/navigation";

import { NavLabel } from "./NavLabel";
import { Shell } from "./Shell";

function TabStrip() {
  return (
    <nav className="tab-strip flex-1" aria-label="Operator views">
      {OPERATOR_TABS.map((tab) => (
        <NavLink key={tab.path} to={tab.path} className="tab">
          {({ isActive }) => <NavLabel active={isActive}>{tab.label}</NavLabel>}
        </NavLink>
      ))}
    </nav>
  );
}

/** How long the expanded rail stays open after the last touch (§21.9: "about two seconds"). */
export const RAIL_COLLAPSE_MS = 2000;

/**
 * Phone in landscape (§21.9 "Phone landscape"): the same tabs as a 56 px
 * rail of two-character labels that floats over the surface rather than
 * insetting it, expanding to 176 px on touch and collapsing about two
 * seconds after the last one. Device state — the dropped status bar's job —
 * sits in its overflow (§21.7). Hidden by CSS at every other height.
 */
function Rail() {
  const [open, setOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const touch = useCallback(() => {
    setOpen(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(false), RAIL_COLLAPSE_MS);
  }, []);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  return (
    <nav className="nav-rail" aria-label="Operator views" data-open={open || undefined} onPointerDown={touch}>
      {OPERATOR_TABS.map((tab) => (
        <NavLink key={tab.path} to={tab.path} className="nav-item rail-item">
          {({ isActive }) => (
            <>
              <b aria-hidden="true">{tab.short}</b>
              <span>
                <NavLabel active={isActive}>{tab.label}</NavLabel>
              </span>
            </>
          )}
        </NavLink>
      ))}
      <details className="rail-devices">
        <summary className="rail-item">
          <b aria-hidden="true">DV</b>
          <span>Devices</span>
        </summary>
        <DeviceList />
      </details>
    </nav>
  );
}

export function OperatorShell({ tier }: { tier: Tier }) {
  return (
    <Shell
      tier={tier}
      manifest="staff"
      header={
        <header className="shell-header short:hidden">
          <span className="brand">Proskenion</span>
          <TabStrip />
        </header>
      }
      aside={<Rail />}
    />
  );
}
