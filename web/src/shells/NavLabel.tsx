/*
 * The accessible name of a navigation item, with the current one announced
 * in words. `NavLink` already sets `aria-current="page"` on the active item,
 * but Narrator in Edge did not announce it on the appliance (1 Oct 2026,
 * docs/hardware/accessibility-check.md §1), so the active item also carries
 * visually hidden text — "Pages, current page" — beside `aria-current`
 * (§24.3: state in words, never only in styling or an attribute).
 *
 * Use as the children function of a `NavLink`:
 *   <NavLink to="pages">{({ isActive }) => <NavLabel active={isActive}>Pages</NavLabel>}</NavLink>
 */
import type { ReactNode } from "react";

export function NavLabel({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <>
      {children}
      {active ? <span className="sr-only">, current page</span> : null}
    </>
  );
}
