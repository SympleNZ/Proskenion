/*
 * Placeholder views (spec §21.27 empty states). Every screen the shells route
 * to renders its empty state until its own task lands, with an action only
 * where the current tier can resolve it.
 */
import {
  Blocks,
  Boxes,
  Database,
  Lightbulb,
  Lock,
  MonitorPlay,
  Projector,
  ScrollText,
  SlidersHorizontal,
  Sparkles,
  Theater,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import type { NavItem } from "@/navigation";

interface EmptyCopy {
  icon: LucideIcon;
  title: string;
  detail: string;
  action?: string;
}

const OPERATOR_EMPTY: Record<string, EmptyCopy> = {
  pages: { icon: Blocks, title: "No pages yet", detail: "An admin builds pages from the channels and buttons the room needs to hand." },
  scenes: { icon: Sparkles, title: "No scenes configured", detail: "Scenes are created in Admin. Operators run them from here." },
  mixer: { icon: SlidersHorizontal, title: "No mixer channels configured", detail: "Channels appear here once the mixer is configured in Admin." },
  lighting: { icon: Lightbulb, title: "No fixtures configured", detail: "Registered fixtures and groups appear here." },
  "stage-plan": { icon: Theater, title: "No fixtures on the stage plan", detail: "The stage plan shows fixtures where they hang." },
  video: { icon: MonitorPlay, title: "No HDMI routing configured", detail: "Sources and outputs appear here once the matrix is configured." },
  projector: { icon: Projector, title: "No projector configured", detail: "Power, input and shutter controls appear here once a projector is configured." },
};

const ADMIN_EMPTY: Record<string, EmptyCopy> = {
  scenes: { icon: Sparkles, title: "No scenes yet", detail: "A scene sets lighting, audio and video together.", action: "Create the first scene" },
  "knx-library": { icon: Boxes, title: "No group addresses registered", detail: "Group addresses give KNX lighting and blinds their names.", action: "Add, or import from ETS" },
  lighting: { icon: Lightbulb, title: "No fixtures configured", detail: "DMX fixtures and groups are registered here.", action: "Add a fixture" },
  mixer: { icon: SlidersHorizontal, title: "No mixer channels configured", detail: "Name the channels operators will see.", action: "Configure channels" },
  backup: { icon: Database, title: "No backups yet", detail: "Backups capture configuration, scenes and the KNX library.", action: "Back up now" },
  logs: { icon: ScrollText, title: "No security events recorded", detail: "Sign-ins, PIN changes and rejected requests appear here." },
};

const GENERIC: EmptyCopy = { icon: Wrench, title: "Nothing to show yet", detail: "This screen is built in a later task." };

export function OperatorView({ tab }: { tab: NavItem }) {
  const copy = OPERATOR_EMPTY[tab.path] ?? GENERIC;
  return (
    <div className="flex flex-col gap-6">
      <h1 className="view-title">{tab.label}</h1>
      <EmptyState icon={copy.icon} title={copy.title} detail={copy.detail} />
    </div>
  );
}

export function AdminView({ item }: { item: NavItem }) {
  const copy = ADMIN_EMPTY[item.path] ?? GENERIC;
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="view-title">{item.label}</h1>
        <p className="view-lede">This screen is built in a later task.</p>
      </div>
      <EmptyState
        icon={copy.icon}
        title={copy.title}
        detail={copy.detail}
        action={
          copy.action ? (
            <Button variant="primary" disabled aria-disabled="true">
              {copy.action}
            </Button>
          ) : undefined
        }
      />
    </div>
  );
}

export function NotPermittedPage() {
  return (
    <main className="centre">
      <EmptyState
        icon={Lock}
        title="Not permitted"
        detail="Admin configuration needs the admin password. Sign out and sign in as admin to continue."
        action={
          <Link to="/app" className="btn btn-secondary">
            Back to the operator view
          </Link>
        }
      />
    </main>
  );
}

export function NotFoundPage() {
  return (
    <main className="centre">
      <EmptyState
        icon={Wrench}
        title="Not found"
        detail="There is nothing at this address."
        action={
          <Link to="/" className="btn btn-secondary">
            Start again
          </Link>
        }
      />
    </main>
  );
}
