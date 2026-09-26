/*
 * Users through TanStack Query (spec §21.23, CONVENTIONS "Interface":
 * configuration is Query, never the live store). Every route here is served
 * by `proskenion/api/auth.py`.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  ChangeOperatorPasswordBody,
  ChangeOperatorPasswordResponse,
  ChangeOwnPasswordBody,
  ChangeOwnPasswordResponse,
  PasswordStatus,
} from "./types";

export const usersKeys = {
  passwordStatus: ["auth", "password-status"] as const,
};

/** Admin only — backs the two cards' "Password last changed" line and the identical-passwords note. */
export function usePasswordStatus(): UseQueryResult<PasswordStatus> {
  return useQuery({ queryKey: usersKeys.passwordStatus, queryFn: () => api<PasswordStatus>("/auth/password-status") });
}

/** The caller's own password: the admin's own card, or the operator's popover entry. */
export function useChangeOwnPassword(): UseMutationResult<ChangeOwnPasswordResponse, unknown, ChangeOwnPasswordBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ChangeOwnPasswordBody) => api<ChangeOwnPasswordResponse>("/auth/change-password", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: usersKeys.passwordStatus });
    },
  });
}

/** The operator card, from the admin's own session — the admin's own current password (see types.ts). */
export function useChangeOperatorPassword(): UseMutationResult<
  ChangeOperatorPasswordResponse,
  unknown,
  ChangeOperatorPasswordBody
> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ChangeOperatorPasswordBody) =>
      api<ChangeOperatorPasswordResponse>("/auth/operator-password", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: usersKeys.passwordStatus });
    },
  });
}
