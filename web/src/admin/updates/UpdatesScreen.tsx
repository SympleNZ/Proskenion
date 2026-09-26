/*
 * Admin → Updates (spec §21.24 *Updates*). Three cards: the application
 * package (upload, review, apply, roll back), the operating system (A/B
 * slots and trial), and restart/reboot. `update_ready`, `update_rolled_back`
 * and `os_trial` are rendered by the shell's `SystemBanners`
 * (`shells/Shell.tsx`) from the live `banner` frame already — nothing here
 * duplicates that; the rolled-back detail shown inline in `UpdateSection`
 * is additional, not a replacement for it.
 */
import { OsSection } from "./OsSection";
import { RestartRebootCard } from "./RestartRebootCard";
import { UpdateSection } from "./UpdateSection";

export function UpdatesScreen() {
  return (
    <div className="view updates-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Updates</h1>
          <p className="view-lede">
            Every action here either restarts the application or reboots the appliance. An update is never
            applied automatically without a package uploaded and reviewed here first.
          </p>
        </div>
      </header>

      <UpdateSection />
      <OsSection />
      <RestartRebootCard />
    </div>
  );
}
