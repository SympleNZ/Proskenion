/*
 * Admin → Rules (spec §21.17). Two tabs, Rules and Derived status, exactly
 * as the mock-up draws it — the daily control map on one screen. Only one
 * tab's data-fetching is ever live: `visible` gates the Rules tab's state
 * poll and the Derived status tab's server-sent events connection, so
 * switching tabs never leaves a second connection or timer running behind
 * the one that is showing.
 */
import { useState } from "react";

import { DerivedStatusTab } from "./DerivedStatusTab";
import { RulesTab } from "./RulesTab";

type TabKey = "rules" | "derived-status";

const TABS: readonly { key: TabKey; label: string }[] = [
  { key: "rules", label: "Rules" },
  { key: "derived-status", label: "Derived status" },
];

export function RulesScreen() {
  const [tab, setTab] = useState<TabKey>("rules");

  return (
    <div className="view rules-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Rules</h1>
          <p className="view-lede">
            The daily control map: when something happens, do something (§8). Anything needing steps or delays is a
            scene instead.
          </p>
        </div>
      </header>

      <div className="tab-strip" role="tablist" aria-label="Rules sections">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            id={`rules-tab-${item.key}`}
            aria-selected={tab === item.key}
            aria-controls={`rules-panel-${item.key}`}
            className="tab"
            data-state={tab === item.key ? "active" : "inactive"}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id="rules-panel-rules" aria-labelledby="rules-tab-rules" hidden={tab !== "rules"}>
        {tab === "rules" ? <RulesTab visible={tab === "rules"} /> : null}
      </div>
      <div role="tabpanel" id="rules-panel-derived-status" aria-labelledby="rules-tab-derived-status" hidden={tab !== "derived-status"}>
        {tab === "derived-status" ? <DerivedStatusTab visible={tab === "derived-status"} /> : null}
      </div>
    </div>
  );
}
