/*
 * The desk-scene band (spec §21.13 "Desk scene band"): persistent, above the
 * mixer area, below the status bar. A pill per scene, the last recalled one
 * highlighted, with its name shown at the right. Recall is a discrete REST
 * action (§21.2), not a continuous write.
 *
 * Without scene recall (§5.5's stub, or a driver that never implements it)
 * the whole row is disabled with the reason rather than hidden — an
 * operator should see that desk scenes exist and why they cannot be used,
 * not wonder whether the row vanished by accident.
 */
import { useState } from "react";

import { presentError } from "@/api/errors";

import { useRecallDeskScene } from "./api";
import type { MixerCapabilities, MixerDeskScene, MixerLastRecalledScene } from "./types";

export interface DeskSceneBandProps {
  scenes: readonly MixerDeskScene[];
  lastRecalledScene: MixerLastRecalledScene | null;
  capabilities: MixerCapabilities;
  connected: boolean;
}

export function DeskSceneBand({ scenes, lastRecalledScene, capabilities, connected }: DeskSceneBandProps) {
  const recall = useRecallDeskScene();
  // The REST response is the freshest word on what was last recalled — held
  // locally rather than round-tripped through the query cache, since no
  // frame updates `last_recalled_scene` and a second recall may follow
  // before `GET /mixer/state` would ever be refetched. Adjusted during
  // render (React's own pattern) rather than in an effect when the query's
  // own value moves — a refetch, for instance.
  const [syncedFromProp, setSyncedFromProp] = useState(lastRecalledScene);
  const [lastRecalled, setLastRecalled] = useState(lastRecalledScene);
  if (lastRecalledScene !== syncedFromProp) {
    setSyncedFromProp(lastRecalledScene);
    setLastRecalled(lastRecalledScene);
  }

  if (scenes.length === 0) return null;

  const unsupported = !capabilities.scene_recall;
  const reason = unsupported ? "This mixer does not support scene recall" : !connected ? "Mixer disconnected" : undefined;
  const disabled = unsupported || !connected || recall.isPending;

  function handleRecall(scene: MixerDeskScene): void {
    recall.mutate(scene.id, {
      onSuccess: (response) => setLastRecalled(response.last_recalled_scene),
      onError: (error) => presentError(error),
    });
  }

  return (
    <section className="mixer-desk-scenes" aria-label="Desk scenes">
      <div className="mixer-desk-scenes-header">
        <h2 className="lighting-section-title">Desk scenes</h2>
        {lastRecalled ? <span className="mixer-desk-scenes-last">Last: {lastRecalled.name}</span> : null}
      </div>
      <div className="mixer-scene-pill-row" role="group" aria-label="Desk scenes" title={reason}>
        {scenes.map((scene) => (
          <button
            key={scene.id}
            type="button"
            className="mixer-scene-pill"
            data-active={lastRecalled?.id === scene.id || undefined}
            disabled={disabled}
            title={reason}
            onClick={() => handleRecall(scene)}
          >
            {scene.name}
          </button>
        ))}
      </div>
      {reason ? (
        <p className="mixer-scene-recall-reason">
          {reason}
        </p>
      ) : null}
    </section>
  );
}
