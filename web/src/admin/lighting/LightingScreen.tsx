/*
 * Admin → Lighting (spec §21.18 *Lighting configuration*). §21.18 draws four
 * tabs — Stage Plan, Fixtures, Bars, Groups — and the task brief adds two
 * more it does not draw: Colour presets and Fixture profiles. Six flat tabs
 * read better at 1280×800 than nesting the last two inside a "Library" tab —
 * each already holds its own filter/summary row, and a third level of
 * navigation would cost more header space than it saves (task report).
 *
 * A native ARIA tablist (§24.2, §24.3) rather than router sub-routes: the
 * screen's data (channels, bars, groups, profiles) is shared across tabs
 * through one TanStack Query cache regardless of which is shown, so a route
 * change buys nothing here that a plain tab switch does not already give.
 */
import { useId, useState, type KeyboardEvent } from "react";

import { BarsTab } from "./BarsTab";
import { FixturesTab } from "./FixturesTab";
import { GroupsTab } from "./GroupsTab";
import { PresetsTab } from "./PresetsTab";
import { ProfilesTab } from "./ProfilesTab";
import { StagePlanTab } from "./StagePlanTab";

const TABS = [
  { id: "stage-plan", label: "Stage Plan" },
  { id: "fixtures", label: "Fixtures" },
  { id: "bars", label: "Bars" },
  { id: "groups", label: "Groups" },
  { id: "presets", label: "Colour presets" },
  { id: "profiles", label: "Fixture profiles" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function LightingScreen() {
  const [active, setActive] = useState<TabId>("stage-plan");
  const idPrefix = useId();

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const index = TABS.findIndex((tab) => tab.id === active);
    let next: number;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    else return;
    event.preventDefault();
    const target = TABS[next];
    if (!target) return;
    setActive(target.id);
    document.getElementById(`${idPrefix}-tab-${target.id}`)?.focus();
  }

  return (
    <div className="view lighting-view">
      <header className="view-head">
        <h1 className="view-title">Lighting</h1>
      </header>

      <div role="tablist" aria-label="Lighting configuration" className="tab-strip" onKeyDown={handleKeyDown}>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            id={`${idPrefix}-tab-${tab.id}`}
            type="button"
            role="tab"
            className="tab"
            aria-selected={active === tab.id}
            aria-controls={`${idPrefix}-panel-${tab.id}`}
            tabIndex={active === tab.id ? 0 : -1}
            onClick={() => setActive(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div id={`${idPrefix}-panel-stage-plan`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-stage-plan`} hidden={active !== "stage-plan"}>
        {active === "stage-plan" ? <StagePlanTab /> : null}
      </div>
      <div id={`${idPrefix}-panel-fixtures`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-fixtures`} hidden={active !== "fixtures"}>
        {active === "fixtures" ? <FixturesTab /> : null}
      </div>
      <div id={`${idPrefix}-panel-bars`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-bars`} hidden={active !== "bars"}>
        {active === "bars" ? <BarsTab /> : null}
      </div>
      <div id={`${idPrefix}-panel-groups`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-groups`} hidden={active !== "groups"}>
        {active === "groups" ? <GroupsTab /> : null}
      </div>
      <div id={`${idPrefix}-panel-presets`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-presets`} hidden={active !== "presets"}>
        {active === "presets" ? <PresetsTab /> : null}
      </div>
      <div id={`${idPrefix}-panel-profiles`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-profiles`} hidden={active !== "profiles"}>
        {active === "profiles" ? <ProfilesTab /> : null}
      </div>
    </div>
  );
}
