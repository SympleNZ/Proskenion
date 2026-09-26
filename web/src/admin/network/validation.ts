/*
 * Client-side mirror of `proskenion/core/network.py`'s `validate()` (§21.24:
 * "Apply stays disabled until valid"). Format, and the gateway on the
 * submitted subnet — everything checkable without asking the server. The
 * one check left out is the device-address collision (§3.1), which needs
 * the configured device list; the server remains authoritative for that and
 * answers `validation_failed` with the same field name (`address`), which
 * the screen folds into the same error slot as these.
 */

export interface NetworkFormValues {
  hostname: string;
  address: string;
  prefixLength: string;
  gateway: string;
  /** Up to four, blanks ignored (contracts §5: `dns: list[str]`, 1–4 entries). */
  dns: string[];
}

export interface NetworkFormErrors {
  hostname?: string;
  address?: string;
  prefix_length?: string;
  gateway?: string;
  dns?: string;
}

const HOSTNAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const OCTET = "(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)";
const IPV4_RE = new RegExp(`^${OCTET}\\.${OCTET}\\.${OCTET}\\.${OCTET}$`);

function parseIPv4(value: string): number | null {
  const trimmed = value.trim();
  if (!IPV4_RE.test(trimmed)) return null;
  const parts = trimmed.split(".").map(Number);
  return (((parts[0] ?? 0) << 24) | ((parts[1] ?? 0) << 16) | ((parts[2] ?? 0) << 8) | (parts[3] ?? 0)) >>> 0;
}

function subnetMask(prefixLength: number): number {
  return prefixLength === 0 ? 0 : (0xffffffff << (32 - prefixLength)) >>> 0;
}

export function nonEmptyDns(dns: readonly string[]): string[] {
  return dns.map((entry) => entry.trim()).filter((entry) => entry.length > 0);
}

/** Every field, never just the first failure (`core/network.py`'s own rule). */
export function validateNetworkForm(values: NetworkFormValues): NetworkFormErrors {
  const errors: NetworkFormErrors = {};

  const hostname = values.hostname.trim().toLowerCase();
  if (!HOSTNAME_RE.test(hostname)) {
    errors.hostname = "Enter a valid hostname (lowercase letters, digits and hyphens)";
  }

  const prefixLength = Number(values.prefixLength);
  const prefixValid = values.prefixLength.trim() !== "" && Number.isInteger(prefixLength) && prefixLength >= 0 && prefixLength <= 32;
  if (!prefixValid) {
    errors.prefix_length = "Enter a mask between /0 and /32";
  }

  const addressNum = parseIPv4(values.address);
  if (addressNum === null) {
    errors.address = "Enter a valid IPv4 address";
  }

  const gatewayNum = parseIPv4(values.gateway);
  if (gatewayNum === null) {
    errors.gateway = "Enter a valid gateway address";
  }

  if (addressNum !== null && gatewayNum !== null && prefixValid) {
    const mask = subnetMask(prefixLength);
    if ((addressNum & mask) !== (gatewayNum & mask)) {
      errors.gateway = `The gateway must be on the same subnet as ${values.address}/${values.prefixLength}`;
    }
  }

  const dns = nonEmptyDns(values.dns);
  const badDns = dns.filter((entry) => parseIPv4(entry) === null);
  if (dns.length === 0) {
    errors.dns = "At least one DNS server is required";
  } else if (badDns.length > 0) {
    errors.dns = `${badDns.map((entry) => `"${entry}"`).join(", ")} is not a valid DNS server address`;
  } else if (dns.length > 4) {
    errors.dns = "At most four DNS servers are permitted";
  }

  return errors;
}

export function isNetworkFormValid(errors: NetworkFormErrors): boolean {
  return Object.keys(errors).length === 0;
}
