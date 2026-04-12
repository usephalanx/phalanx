/**
 * React hooks for board data fetching and creation.
 */

import { useCallback, useEffect, useState } from "react";
import apiClient from "../api/client";
import type { Board, BoardCreateRequest } from "../types/board";

/**
 * Hook return type for fetching boards.
 */
export interface UseBoardsResult {
  /** List of boards in the workspace. */
  boards: Board[];
  /** Whether the initial fetch is in progress. */
  loading: boolean;
  /** Error message, or null if no error. */
  error: string | null;
  /** Re-fetch the board list. */
  refetch: () => void;
}

/**
 * Fetch all boards for a given workspace.
 *
 * @param workspaceId - The workspace ID to fetch boards for.
 */
export function useBoards(workspaceId: string | undefined): UseBoardsResult {
  const [boards, setBoards] = useState<Board[]>([]);
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

    async function fetchBoards(): Promise<void> {
      setLoading(true);
      setError(null);
      try {
        const response = await apiClient.get<Board[]>(
          `/workspaces/${workspaceId}/boards`,
        );
        if (!cancelled) {
          setBoards(response.data);
        }
      } catch {
        if (!cancelled) {
          setError("Failed to load boards.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void fetchBoards();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, fetchKey]);

  return { boards, loading, error, refetch };
}

/**
 * Hook return type for creating a board.
 */
export interface UseCreateBoardResult {
  /** Create a new board in the given workspace. */
  createBoard: (data: BoardCreateRequest) => Promise<Board>;
  /** Whether a creation request is in progress. */
  creating: boolean;
  /** Error message from the last creation attempt, or null. */
  error: string | null;
}

/**
 * Hook for creating a new board within a workspace.
 *
 * @param workspaceId - The workspace ID to create the board in.
 */
export function useCreateBoard(
  workspaceId: string | undefined,
): UseCreateBoardResult {
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createBoard = useCallback(
    async (data: BoardCreateRequest): Promise<Board> => {
      if (!workspaceId) {
        throw new Error("Workspace ID is required to create a board.");
      }
      setCreating(true);
      setError(null);
      try {
        const response = await apiClient.post<Board>(
          `/workspaces/${workspaceId}/boards`,
          data,
        );
        return response.data;
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to create board.";
        setError(message);
        throw err;
      } finally {
        setCreating(false);
      }
    },
    [workspaceId],
  );

  return { createBoard, creating, error };
}
