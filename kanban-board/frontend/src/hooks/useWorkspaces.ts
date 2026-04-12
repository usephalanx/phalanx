/**
 * React hooks for workspace data fetching and creation.
 */

import { useCallback, useEffect, useState } from "react";
import apiClient from "../api/client";
import type { Workspace, WorkspaceCreateRequest } from "../types/workspace";

/**
 * Hook return type for fetching workspaces.
 */
export interface UseWorkspacesResult {
  /** List of workspaces the user belongs to. */
  workspaces: Workspace[];
  /** Whether the initial fetch is in progress. */
  loading: boolean;
  /** Error message, or null if no error. */
  error: string | null;
  /** Re-fetch the workspace list. */
  refetch: () => void;
}

/**
 * Fetch all workspaces the current user is a member of.
 */
export function useWorkspaces(): UseWorkspacesResult {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  const refetch = useCallback(() => {
    setFetchKey((prev) => prev + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function fetchWorkspaces(): Promise<void> {
      setLoading(true);
      setError(null);
      try {
        const response = await apiClient.get<Workspace[]>("/workspaces");
        if (!cancelled) {
          setWorkspaces(response.data);
        }
      } catch {
        if (!cancelled) {
          setError("Failed to load workspaces.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void fetchWorkspaces();
    return () => {
      cancelled = true;
    };
  }, [fetchKey]);

  return { workspaces, loading, error, refetch };
}

/**
 * Hook return type for creating a workspace.
 */
export interface UseCreateWorkspaceResult {
  /** Create a new workspace with the given data. */
  createWorkspace: (data: WorkspaceCreateRequest) => Promise<Workspace>;
  /** Whether a creation request is in progress. */
  creating: boolean;
  /** Error message from the last creation attempt, or null. */
  error: string | null;
}

/**
 * Hook for creating a new workspace.
 */
export function useCreateWorkspace(): UseCreateWorkspaceResult {
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createWorkspace = useCallback(
    async (data: WorkspaceCreateRequest): Promise<Workspace> => {
      setCreating(true);
      setError(null);
      try {
        const response = await apiClient.post<Workspace>("/workspaces", data);
        return response.data;
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to create workspace.";
        setError(message);
        throw err;
      } finally {
        setCreating(false);
      }
    },
    [],
  );

  return { createWorkspace, creating, error };
}
