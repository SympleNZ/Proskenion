/*
 * Admin shell (spec §21.6): sidebar on desktop, drawer behind a hamburger on
 * mobile. Admin tier only.
 *
 * §21.6 does not show a way back to the operator interface from here, and
 * none existed until this fix (Simon, 25 Sep 2026, v0.1.2): the admin
 * sidebar and the mobile header both carry a "Main interface" link to
 * `/app`, so leaving admin is never a matter of editing the URL by hand.
 * An admin can always reach the main interface — the same role rule that
 * already lets the account chip's own "Admin" item show unconditionally for
 * `tier === "admin"` (`components/statusbar/AccountChip.tsx`) — so this
 * link carries no permission check of its own.
 */
import { ArrowLeft, Menu as MenuIcon } from "lucide-react";
import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { useDevices } from "@/admin/devices/api";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { ADMIN_NAV, CONTROL_SURFACE_PATH } from "@/navigation";

import { Shell } from "./Shell";

/**
 * Present only when a control-surface device is configured; otherwise the
 * navigation item is absent, not disabled (spec §21.25). The configurator
 * itself is Phase 9 and not built yet — `control-surface` still routes to
 * the generic admin placeholder — but the item's visibility does not wait
 * for that: nothing else on this screen needs a dedicated endpoint, since
 * `GET /devices` (already fetched for the Devices screen) already carries
 * every device's category.
 */
function useHasControlSurface(): boolean {
  const devicesQuery = useDevices();
  return (devicesQuery.data?.devices ?? []).some((device) => device.category === "control_surface");
}

/** The link back to `/app`, present on both the sidebar and the mobile
 * header — "Main interface" is the shared accessible name both use, so a
 * test (or a screen reader user) finds it the same way at either width. */
function MainInterfaceLink({ className, iconOnly, onNavigate }: { className: string; iconOnly?: boolean; onNavigate?: () => void }) {
  return (
    <NavLink to="/app" className={className} onClick={onNavigate} aria-label="Main interface">
      <ArrowLeft aria-hidden="true" className="size-4" />
      {!iconOnly && <span>Main interface</span>}
    </NavLink>
  );
}

export function AdminNav({ onNavigate }: { onNavigate?: () => void }) {
  const hasControlSurface = useHasControlSurface();
  return (
    <nav aria-label="Admin">
      {ADMIN_NAV.map((section) => {
        const items = section.items.filter(
          (item) => item.path !== CONTROL_SURFACE_PATH || hasControlSurface,
        );
        if (items.length === 0) return null;
        return (
          <div key={section.label}>
            <div className="nav-group-label">{section.label}</div>
            <ul>
              {items.map((item) => (
                <li key={item.path}>
                  <NavLink to={item.path} className="nav-item" onClick={onNavigate}>
                    <item.icon aria-hidden="true" className="size-4" />
                    <span>{item.label}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

function Sidebar() {
  return (
    <aside className="admin-sidebar" data-testid="admin-sidebar">
      <div className="brand flex-col items-start gap-1">
        <span>Proskenion</span>
        <span className="brand-host font-regular">Admin</span>
      </div>
      <MainInterfaceLink className="nav-item admin-main-link" />
      <AdminNav />
    </aside>
  );
}

function MobileHeader() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  return (
    <header className="shell-header admin-mobile-header">
      <Sheet open={open} onOpenChange={setOpen}>
        <Button variant="ghost" size="icon" className="admin-menu-button" aria-label="Open admin menu" onClick={() => setOpen(true)}>
          <MenuIcon aria-hidden="true" className="size-5" />
        </Button>
        <SheetContent side="left" title="Admin" hideTitle className="admin-drawer" key={location.pathname}>
          <div className="brand px-5 py-4">Proskenion</div>
          <MainInterfaceLink className="nav-item admin-main-link" onNavigate={() => setOpen(false)} />
          <AdminNav onNavigate={() => setOpen(false)} />
        </SheetContent>
      </Sheet>
      <span className="brand">Admin</span>
      {/* Visible without opening the drawer, so leaving admin is never
          gated behind the hamburger menu on a phone. */}
      <MainInterfaceLink className="btn btn-ghost btn-icon admin-header-main-link" iconOnly />
    </header>
  );
}

export function AdminShell() {
  return <Shell tier="admin" manifest="staff" header={<MobileHeader />} aside={<Sidebar />} className="admin-layout-shell" />;
}
