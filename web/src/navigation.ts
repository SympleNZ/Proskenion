/*
 * Navigation (spec §21.6). Three shells, one codebase. The operator tab strip
 * and the admin sidebar sections are declared here so the shells and the
 * routes render from one list, and a test can check the order.
 */
import {
  Boxes,
  Cable,
  Cpu,
  Database,
  FileText,
  HardDriveDownload,
  HeartPulse,
  KeyRound,
  LayoutGrid,
  Lightbulb,
  LifeBuoy,
  ListChecks,
  Mail,
  MonitorPlay,
  Network,
  Projector,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Theater,
  Users,
  Video,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  label: string;
  path: string;
  icon: LucideIcon;
}

/** Pages | Scenes | Mixer | Lighting | Stage Plan | Video | Projector — Pages is the default landing. */
export const OPERATOR_TABS: readonly NavItem[] = [
  { label: "Pages", path: "pages", icon: LayoutGrid },
  { label: "Scenes", path: "scenes", icon: Sparkles },
  { label: "Mixer", path: "mixer", icon: SlidersHorizontal },
  { label: "Lighting", path: "lighting", icon: Lightbulb },
  { label: "Stage Plan", path: "stage-plan", icon: Theater },
  { label: "Video", path: "video", icon: Video },
  { label: "Projector", path: "projector", icon: Projector },
];

export interface NavSection {
  label: string;
  items: readonly NavItem[];
}

export const ADMIN_NAV: readonly NavSection[] = [
  {
    label: "Control",
    items: [
      { label: "Pages", path: "pages", icon: LayoutGrid },
      { label: "Scenes", path: "scenes", icon: Sparkles },
      { label: "Rules", path: "rules", icon: ListChecks },
      { label: "Hirer Access", path: "hirer-access", icon: KeyRound },
    ],
  },
  {
    label: "Configure",
    items: [
      { label: "KNX Library", path: "knx-library", icon: Boxes },
      { label: "Lighting", path: "lighting", icon: Lightbulb },
      { label: "Mixer", path: "mixer", icon: SlidersHorizontal },
      { label: "HDMI", path: "hdmi", icon: MonitorPlay },
      { label: "Control Surface", path: "control-surface", icon: Cable },
    ],
  },
  {
    label: "System",
    items: [
      { label: "Network", path: "network", icon: Network },
      { label: "Certificates", path: "certificates", icon: ShieldCheck },
      { label: "Devices", path: "devices", icon: Cpu },
      { label: "Email", path: "email", icon: Mail },
      { label: "Backup", path: "backup", icon: Database },
      { label: "Updates", path: "updates", icon: HardDriveDownload },
      { label: "Health", path: "health", icon: HeartPulse },
      { label: "Logs", path: "logs", icon: ScrollText },
    ],
  },
  {
    label: "Account",
    items: [
      { label: "Users", path: "users", icon: Users },
      { label: "Help", path: "help", icon: LifeBuoy },
    ],
  },
];

export const ADMIN_ITEMS: readonly NavItem[] = ADMIN_NAV.flatMap((section) => section.items);

/** Present only when a control-surface device is configured (spec §21.25) — see `shells/AdminShell.tsx`. */
export const CONTROL_SURFACE_PATH = "control-surface";

export { FileText };
