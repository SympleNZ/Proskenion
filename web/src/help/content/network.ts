/* Network (spec §21.24, §10.8). */
import type { HelpEntry } from "../types";

export const network = {
  "network.hostname": {
    term: "Hostname",
    body: "The name this controller answers to on the network, alongside its address.",
  },
  "network.address": {
    term: "IP address",
    body: "This controller's static address on the venue network.",
  },
  "network.prefix": {
    term: "Mask (prefix length)",
    body: "The subnet size, as a prefix length — /24 for a typical 255.255.255.0 network.",
  },
  "network.gateway": {
    term: "Gateway",
    body: "The router this controller sends traffic through to reach anything outside its own subnet.",
  },
  "network.dns": {
    term: "DNS servers",
    body: "Up to four name servers, tried in order. At least one is required.",
  },
  "network.apply": {
    term: "Apply",
    body: "Applies this network change. Every connected session drops the instant it takes effect, and this browser is sent to check the new address — the change reverts automatically after three minutes if nothing confirms it took.",
  },
  "network.confirm": {
    term: "Confirm this address",
    body: "Confirms the change was applied correctly, so it does not revert automatically.",
  },
} as const satisfies Record<string, HelpEntry>;
