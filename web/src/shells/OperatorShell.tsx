/*
 * Operator shell (spec §21.6): a tab strip below the brand on desktop and
 * tablet, a rail on a phone in landscape — never at the bottom. Pages is the
 * default landing.
 */
import { NavLink } from "react-router-dom";

import type { Tier } from "@/api/auth";
import { DeviceList } from "@/components/DeviceList";
import { OPERATOR_TABS } from "@/navigation";

import { Shell } from "./Shell";

function TabStrip() {
  return (
    <nav className="tab-strip flex-1" aria-label="Operator views">
      {OPERATOR_TABS.map((tab) => (
        <NavLink key={tab.path} to={tab.path} className="tab">
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}

/** Phone in landscape: the same tabs as a rail, with device state in its overflow. */
function Rail() {
  return (
    <nav className="nav-rail" aria-label="Operator views">
      {OPERATOR_TABS.map((tab) => (
        <NavLink key={tab.path} to={tab.path} className="nav-item">
          <tab.icon aria-hidden="true" className="size-4" />
          <span>{tab.label}</span>
        </NavLink>
      ))}
      <details className="rail-devices">
        <summary>Devices</summary>
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
