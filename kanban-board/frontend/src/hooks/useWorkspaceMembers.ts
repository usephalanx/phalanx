/**
 * React hooks for workspace member data fetching and inviting.
 */

import { useCallback, useEffect, useState } from "react";
import apiClient from "../api/client";
import type { MemberAddRequest, WorkspaceMember } from "../types/workspace";

/**
 * Hook return type for fetching workspace members.
 */
export interface UseWorkspaceMembersResult {
  /** List of members in the workspace. */
  members: WorkspaceMember[];
  /** Whether the initial fetch is in progress. */
  loading: boolean;
  /** Error message, or null if no error. */
  error: string | null;
  /** Re-fetch the member list. */
  refetch: () => void;
}

/**
 * Fetch all members of a workspace.
 *
 * @param workspaceId - The workspace ID to fetch members for.
 */
export function useWorkspaceMembers(
  workspaceId: string | undefined,
): UseWorkspaceMembersResult {
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  const refetch = useCallback(() => {
    setFetchKey((prev) => prev + 1);
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setLoading(false);
      return;
    }

    let cancelled = false;

    async function fetchMembers(): Promise<void> {
      setLoading(true);
      setError(null);
      try {
        const response = await apiClient.get<WorkspaceMember[]>(
          `/workspaces/${workspaceId}/members`,
        );
        if (!cancelled) {
          setMembers(response.data);
        }
      } catch {
        if (!cancelled) {
          setError("Failed to load members.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void fetchMembers();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, fetchKey]);

  return { members, loading, error, refetch };
}

/**
 * Hook return type for inviting a workspace member.
 */
export interface UseInviteMemberResult {
  /** Invite a user to the workspace by email. */
  inviteMember: (data: MemberAddRequest) => Promise<WorkspaceMember>;
  /** Whether an invite request is in progress. */
  inviting: boolean;
  /** Error message from the last invite attempt, or null. */
  error: string | null;
}

/**
 * Hook for inviting a new member to a workspace.
 *
 * @param workspaceId - The workspace ID to invite the member to.
 */
export function useInviteMember(
  workspaceId: string | undefined,
): UseInviteMemberResult {
  const [inviting, setInviting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inviteMember = useCallback(
    async (data: MemberAddRequest): Promise<WorkspaceMember> => {
      if (!workspaceId) {
        throw new Error("Workspace ID is required to invite a member.");
      }
      setInviting(true);
      setError(null);
      try {
        const response = await apiClient.post<WorkspaceMember>(
          `/workspaces/${workspaceId}/members`,
          data,
        );
        return response.data;
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to invite member.";
        setError(message);
        throw err;
      } finally {
        setInviting(false);
      }
    },
    [workspaceId],
  );

  return { inviteMember, inviting, error };
}
