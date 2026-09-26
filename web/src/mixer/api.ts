/*
 * Mixer configuration and control through TanStack Query (CONVENTIONS
 * "Interface", §21.2). `GET /mixer/state` carries the whole snapshot —
 * capabilities, Main, outputs, inputs, desk scenes — and is the fallback
 * until the live store holds a value for a given channel; from then on a
 * `mixer_state` frame wins, key by key, exactly as `GET /hdmi/state` and the
 * `hdmi_source` frame already do for video routing.
 *
 * Fader moves are a continuous write and go over the WebSocket `set` (§21.2),
 * never through here — `socket.ts` owns that. Mute, pan and desk-scene
 * recall are discrete REST actions (phase-4-contracts.md), which is what
 * this file wraps.
 */
import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { FaderLaw } from "@/lib/faderLaw";

import type { MixerChannelWriteResponse, MixerRecallResponse, MixerStateResponse } from "./types";

export const mixerKeys = {
  state: ["mixer", "state"] as const,
  faderLaw: (deviceId: number) => ["devices", deviceId, "fader-law"] as const,
};

export function useMixerState(): UseQueryResult<MixerStateResponse> {
  return useQuery({ queryKey: mixerKeys.state, queryFn: () => api<MixerStateResponse>("/mixer/state") });
}

/**
 * The driver's published fader law (§5.5, §21.2): fetched once per device
 * and cached with configuration state, never re-derived per drag. `null`
 * while there is no mixer configured — the query stays disabled rather than
 * requesting `/devices/null/fader-law`.
 */
export function useFaderLaw(deviceId: number | null): UseQueryResult<FaderLaw> {
  return useQuery({
    queryKey: mixerKeys.faderLaw(deviceId ?? -1),
    queryFn: () => api<{ fader_law: FaderLaw }>(`/devices/${deviceId}/fader-law`).then((body) => body.fader_law),
    enabled: deviceId !== null,
  });
}

export interface SetMixerMuteInput {
  channelId: number;
  muted: boolean;
}

/** Absolute mute only — a toggle is resolved client-side against the known state (phase-4-contracts.md). */
export function useSetMixerMute(): UseMutationResult<MixerChannelWriteResponse, unknown, SetMixerMuteInput> {
  return useMutation({
    mutationFn: ({ channelId, muted }: SetMixerMuteInput) =>
      api<MixerChannelWriteResponse>(`/mixer/channels/${channelId}/mute`, { method: "POST", body: { muted } }),
  });
}

export interface SetMixerPanInput {
  channelId: number;
  pan: number;
}

export function useSetMixerPan(): UseMutationResult<MixerChannelWriteResponse, unknown, SetMixerPanInput> {
  return useMutation({
    mutationFn: ({ channelId, pan }: SetMixerPanInput) =>
      api<MixerChannelWriteResponse>(`/mixer/channels/${channelId}/pan`, { method: "POST", body: { pan } }),
  });
}

export function useRecallDeskScene(): UseMutationResult<MixerRecallResponse, unknown, number> {
  return useMutation({
    mutationFn: (sceneId: number) => api<MixerRecallResponse>(`/mixer/desk-scenes/${sceneId}/recall`, { method: "POST" }),
  });
}
