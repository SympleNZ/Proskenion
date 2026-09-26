/*
 * The Devices screen (spec §21.24 *Devices*): one card per configured driver
 * instance, on a single scrollable page, grouped by what the thing is.
 *
 * The forms are generated, not hand-written, so nothing here knows a driver
 * name — adding a driver needs no interface work.
 */
import { Cpu } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";

import { AddDeviceSheet } from "./AddDeviceSheet";
import { useDevices, useDrivers } from "./api";
import { DeviceCard } from "./DeviceCard";
import { categoryLabel, type Device, type Driver } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function CardSkeleton() {
  return (
    <div className="card device-card" aria-hidden="true">
      <Skeleton className="h-8 w-full" />
      <Skeleton className="h-touch w-full" />
      <Skeleton className="h-touch w-full" />
    </div>
  );
}

export function DevicesScreen() {
  const drivers = useDrivers();
  const devices = useDevices();
  const [adding, setAdding] = useState(false);

  if (drivers.isPending || devices.isPending) {
    return (
      <div className="view devices-view" aria-busy="true" aria-label="Loading the configured devices">
        <h1 className="view-title">Devices</h1>
        <CardSkeleton />
        <CardSkeleton />
      </div>
    );
  }

  if (drivers.isError || devices.isError) {
    const error = drivers.error ?? devices.error;
    return (
      <div className="view devices-view">
        <h1 className="view-title">Devices</h1>
        <ErrorState
          title="Could not load the devices"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(error)}
          onRetry={() => {
            void drivers.refetch();
            void devices.refetch();
          }}
        />
      </div>
    );
  }

  const allDrivers: Driver[] = drivers.data?.drivers ?? [];
  const configured: Device[] = devices.data?.devices ?? [];
  const categories = [...new Set([...configured.map((device) => device.category)])].sort();

  return (
    <div className="view devices-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Devices</h1>
          <p className="view-lede">
            One card per configured driver instance. The forms are generated from each driver&apos;s declared schema, so
            adding a driver needs no interface work.
          </p>
        </div>
        <Button variant="primary" helpId="devices.open-add" onClick={() => setAdding(true)}>
          Add a device
        </Button>
      </header>

      {configured.length === 0 ? (
        <EmptyState
          icon={Cpu}
          title="No devices configured"
          detail="A device is a mixer, a projector, a lighting output, an HDMI matrix or a control surface. Nothing needs to be online to configure one."
          action={
            <Button variant="primary" helpId="devices.open-add" onClick={() => setAdding(true)}>
              Add the first device
            </Button>
          }
        />
      ) : (
        categories.map((category) => {
          const inCategory = configured.filter((device) => device.category === category);
          const alternatives = allDrivers.filter((driver) => driver.category === category);
          return (
            <section className="device-group" key={category} aria-labelledby={`category-${category}`}>
              <h2 className="section-title" id={`category-${category}`}>
                {categoryLabel(category)}
              </h2>
              {inCategory.map((device) => (
                <DeviceCard
                  key={device.id}
                  device={device}
                  driver={allDrivers.find((driver) => driver.key === device.driver_key && driver.category === category)}
                  alternatives={alternatives}
                />
              ))}
            </section>
          );
        })
      )}

      <AddDeviceSheet open={adding} onOpenChange={setAdding} drivers={allDrivers} />
    </div>
  );
}
