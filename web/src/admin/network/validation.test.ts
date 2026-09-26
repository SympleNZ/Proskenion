/*
 * Client-side mirror of `proskenion/core/network.py`'s `validate()` — every
 * field checked, never just the first failure, and the gateway checked
 * against the submitted subnet.
 */
import { describe, expect, it } from "vitest";

import { isNetworkFormValid, validateNetworkForm, type NetworkFormValues } from "./validation";

const VALID: NetworkFormValues = {
  hostname: "auditorium",
  address: "10.2.30.45",
  prefixLength: "24",
  gateway: "10.2.30.1",
  dns: ["10.2.30.1"],
};

describe("validateNetworkForm", () => {
  it("accepts a valid submission", () => {
    const errors = validateNetworkForm(VALID);
    expect(errors).toEqual({});
    expect(isNetworkFormValid(errors)).toBe(true);
  });

  it("rejects an invalid hostname (uppercase, underscore)", () => {
    expect(validateNetworkForm({ ...VALID, hostname: "Bad_Host" }).hostname).toBeDefined();
  });

  it("rejects a malformed IPv4 address", () => {
    expect(validateNetworkForm({ ...VALID, address: "999.1.1.1" }).address).toBeDefined();
    expect(validateNetworkForm({ ...VALID, address: "not-an-address" }).address).toBeDefined();
  });

  it("rejects a prefix length outside 0–32", () => {
    expect(validateNetworkForm({ ...VALID, prefixLength: "33" }).prefix_length).toBeDefined();
    expect(validateNetworkForm({ ...VALID, prefixLength: "-1" }).prefix_length).toBeDefined();
    expect(validateNetworkForm({ ...VALID, prefixLength: "" }).prefix_length).toBeDefined();
  });

  it("rejects a gateway outside the submitted subnet", () => {
    const errors = validateNetworkForm({ ...VALID, gateway: "10.2.31.1" });
    expect(errors.gateway).toBeDefined();
  });

  it("accepts a gateway on the edge of the subnet", () => {
    const errors = validateNetworkForm({ ...VALID, address: "10.2.30.45", prefixLength: "24", gateway: "10.2.30.254" });
    expect(errors.gateway).toBeUndefined();
  });

  it("requires at least one DNS server", () => {
    expect(validateNetworkForm({ ...VALID, dns: [""] }).dns).toBeDefined();
    expect(validateNetworkForm({ ...VALID, dns: [] }).dns).toBeDefined();
  });

  it("rejects a malformed DNS server address", () => {
    expect(validateNetworkForm({ ...VALID, dns: ["not-an-address"] }).dns).toBeDefined();
  });

  it("reports every failing field at once, not just the first", () => {
    const errors = validateNetworkForm({
      hostname: "Bad Host!",
      address: "bad",
      prefixLength: "99",
      gateway: "bad",
      dns: [""],
    });
    expect(Object.keys(errors).sort()).toEqual(["address", "dns", "gateway", "hostname", "prefix_length"]);
  });
});
