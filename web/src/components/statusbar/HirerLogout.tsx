/*
 * The hirer has one button (spec §21.7): a single Log out, no menu, no chip.
 * It confirms, and the confirmation says what will happen in those terms.
 */
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { useSession } from "@/session/context";

export const HIRER_LOGOUT_CONFIRMATION = "You will need the venue PIN to get back in.";

export function HirerLogout() {
  const { signOut } = useSession();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="ghost" onClick={() => setOpen(true)}>
        Log out
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Log out?"
        description={HIRER_LOGOUT_CONFIRMATION}
        confirmLabel="Log out"
        destructive
        onConfirm={() => void signOut()}
      />
    </>
  );
}
