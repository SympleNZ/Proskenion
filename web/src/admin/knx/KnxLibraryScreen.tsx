/*
 * Admin → KNX Library (§21.19): "Two tabs: Addresses and Devices." KNX is a
 * subsystem, not a device — it does not appear on the Devices screen
 * (§5.5 B42) — so this screen is its own admin entry, registered in
 * `ADMIN_SCREENS` (spec §21.6's routes).
 *
 * Both tabs share one fetch of the address library and the device groups:
 * the Devices tab's "expand to show its addresses" and the Addresses tab's
 * device-group filter and form both need the same two lists, and fetching
 * them once here keeps the two tabs looking at one consistent snapshot.
 */
import { Boxes } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";

import { useAddresses, useDeviceGroups } from "./api";
import { AddressesTab } from "./AddressesTab";
import { DevicesTab } from "./DevicesTab";

type Tab = "addresses" | "devices";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function KnxLibraryScreen() {
  const [tab, setTab] = useState<Tab>("addresses");
  const addresses = useAddresses();
  const deviceGroups = useDeviceGroups();

  return (
    <div className="view knx-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">KNX Library</h1>
          <p className="view-lede">
            Every group address the system touches, in either direction, is registered here before use (§7.1). KNX
            is a subsystem, not a device — its addresses and device groups live here, not on the Devices screen.
          </p>
        </div>
      </header>

      <div className="tab-strip" role="tablist" aria-label="KNX Library sections">
        <button
          type="button"
          role="tab"
          id="knx-tab-addresses"
          className="tab"
          aria-selected={tab === "addresses"}
          aria-current={tab === "addresses" ? "page" : undefined}
          aria-controls="knx-panel-addresses"
          onClick={() => setTab("addresses")}
        >
          <Boxes aria-hidden="true" className="size-4" /> Addresses
        </button>
        <button
          type="button"
          role="tab"
          id="knx-tab-devices"
          className="tab"
          aria-selected={tab === "devices"}
          aria-current={tab === "devices" ? "page" : undefined}
          aria-controls="knx-panel-devices"
          onClick={() => setTab("devices")}
        >
          Devices
        </button>
      </div>

      {addresses.isPending || deviceGroups.isPending ? (
        <div aria-busy="true" aria-label="Loading the KNX library">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-touch w-full" />
        </div>
      ) : addresses.isError || deviceGroups.isError ? (
        <ErrorState
          title="Could not load the KNX library"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(addresses.error ?? deviceGroups.error)}
          onRetry={() => {
            void addresses.refetch();
            void deviceGroups.refetch();
          }}
        />
      ) : (
        <>
          <div role="tabpanel" id="knx-panel-addresses" aria-labelledby="knx-tab-addresses" hidden={tab !== "addresses"}>
            {tab === "addresses" ? <AddressesTab addresses={addresses.data ?? []} deviceGroups={deviceGroups.data ?? []} /> : null}
          </div>
          <div role="tabpanel" id="knx-panel-devices" aria-labelledby="knx-tab-devices" hidden={tab !== "devices"}>
            {tab === "devices" ? <DevicesTab addresses={addresses.data ?? []} deviceGroups={deviceGroups.data ?? []} /> : null}
          </div>
        </>
      )}
    </div>
  );
}
