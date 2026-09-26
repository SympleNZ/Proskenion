/* `/system/network*` wire types (phase-6-contracts.md §5). */

export interface NetworkConfig {
  hostname: string | null;
  address: string | null;
  prefix_length: number | null;
  gateway: string | null;
  dns: string[];
}

export interface NetworkUpdateBody {
  hostname: string;
  address: string;
  prefix_length: number;
  gateway: string;
  dns: string[];
}

/** `202` from `POST /system/network` (contracts §5 step 1). */
export interface NetworkChangeResult {
  confirm_token: string;
  applied_at: string;
  reverts_at: string;
  address: string;
  hostname: string;
  dns_updated: boolean;
}

/** `GET /system/network/state`. */
export interface NetworkState {
  pending: boolean;
  applied_at: string | null;
  reverts_at: string | null;
  previous_address: string | null;
}

export interface NetworkConfirmResult {
  confirmed: boolean;
}
