/*
 * Admin → Users (spec §21.23), served by `proskenion/api/auth.py`. Two fixed
 * staff tiers, no creating and no deleting — these types mirror the API's
 * response models exactly.
 */

export interface TierPasswordStatus {
  /** ISO 8601 with offset, or `null` — the seed placeholder has never been replaced ("Not recorded"). */
  password_changed_at: string | null;
}

// -- GET /auth/password-status -------------------------------------------------

export interface PasswordStatus {
  admin: TierPasswordStatus;
  operator: TierPasswordStatus;
  /**
   * Whether the two staff passwords are currently the same — computed and
   * cached at change time, never by comparing two stored hashes (§21.23).
   */
  identical: boolean;
}

// -- POST /auth/change-password: own password, either tier ---------------------

export interface ChangeOwnPasswordBody {
  current_password: string;
  new_password: string;
}

export interface ChangeOwnPasswordResponse {
  tier: "admin" | "operator";
  expires_at: string;
}

// -- POST /auth/operator-password: an admin resets the operator's -------------
//
// `current_password` here is the *admin's own* — see proskenion/api/auth.py's
// module docstring for why: the admin's own card and the operator's own
// popover entry both go through change-password above, proving their own
// current password; this one lets an admin who does not (or whose operator
// cannot) know the operator's password set a new one anyway, proving the
// admin's own identity instead.

export interface ChangeOperatorPasswordBody {
  current_password: string;
  new_password: string;
}

export interface ChangeOperatorPasswordResponse {
  tier: "operator";
  password_changed_at: string | null;
}
