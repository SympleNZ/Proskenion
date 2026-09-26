/*
 * Admin → Backup (spec §21.24 *Backup*, §13, contracts §5, §8): destinations
 * and media, history with downloads, restore with its confirmations, the
 * venue baseline card, and system images. Each block owns its own loading
 * and error state — the same composition `admin/hdmi/HdmiScreen.tsx` uses
 * for its sections — because a slow or failed images list must never block
 * "Back up now" from being usable.
 *
 * The backup and media banners (§21.26, contracts §6: `backup_failed_amber`,
 * `backup_failed_red`, `backup_media_absent`, `backup_untrusted`) need
 * nothing from this screen: `SystemBanners` already renders whatever the
 * server sends, generically, for every banner key (see its own module doc).
 */
import { useBackupStatus } from "./api";
import { BaselineCard } from "./BaselineCard";
import { DestinationsCard } from "./DestinationsCard";
import { HistoryCard } from "./HistoryCard";
import { ImagesCard } from "./ImagesCard";
import { RestoreCard } from "./RestoreCard";
import { SnapshotsCard } from "./SnapshotsCard";

export function BackupScreen() {
  const status = useBackupStatus();

  return (
    <div className="view backup-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Backup</h1>
          <p className="view-lede">
            Local copies, the backup USB and a network destination (§13.1), the venue baseline, and system images.
          </p>
        </div>
      </header>

      <DestinationsCard status={status.data} />
      <HistoryCard status={status.data} />
      <RestoreCard />
      <SnapshotsCard />
      <BaselineCard />
      <ImagesCard />
    </div>
  );
}
