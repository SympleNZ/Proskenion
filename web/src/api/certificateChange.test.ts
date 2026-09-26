/* Which host a certificate covers, and which name to send the user to. */
import { describe, expect, it } from "vitest";

import { hostCovered, preferredName } from "./certificateChange";

describe("hostCovered", () => {
  const names = ["auditorium.obhs.school.nz", "10.2.30.251"];

  it("covers the names and addresses the certificate lists, and nothing else", () => {
    expect(hostCovered("auditorium.obhs.school.nz", names)).toBe(true);
    expect(hostCovered("AUDITORIUM.obhs.school.nz", names)).toBe(true);
    expect(hostCovered("10.2.30.251", names)).toBe(true);
    // The restored real certificate on the bare IP: never acceptable there.
    expect(hostCovered("10.2.30.251", ["auditorium.obhs.school.nz"])).toBe(false);
    expect(hostCovered("auditorium", names)).toBe(false);
  });

  it("lets a wildcard cover exactly one label", () => {
    expect(hostCovered("av.school.nz", ["*.school.nz"])).toBe(true);
    expect(hostCovered("a.b.school.nz", ["*.school.nz"])).toBe(false);
    expect(hostCovered("school.nz", ["*.school.nz"])).toBe(false);
  });
});

describe("preferredName", () => {
  it("prefers a DNS name to an address or a wildcard", () => {
    expect(preferredName(["10.2.30.251", "*.school.nz", "auditorium.obhs.school.nz"])).toBe("auditorium.obhs.school.nz");
    expect(preferredName(["10.2.30.251"])).toBe("10.2.30.251");
    expect(preferredName([])).toBeNull();
  });
});
