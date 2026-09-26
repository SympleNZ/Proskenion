/*
 * Admin → Help (spec §21.24 *Help*): the same content the `?` sheet shows,
 * reached from the sidebar as an ordinary page instead of an overlay. Admin
 * only — the operator and hirer shells have no sidebar to reach it from,
 * only the `?` sheet itself (`shells/Shell.tsx`).
 */
import { HelpContent } from "./HelpContent";

export function HelpScreen() {
  return (
    <div className="view help-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Help</h1>
          <p className="view-lede">Also reachable from anywhere in the admin interface with the ? key.</p>
        </div>
      </header>
      <HelpContent tier="admin" />
    </div>
  );
}
