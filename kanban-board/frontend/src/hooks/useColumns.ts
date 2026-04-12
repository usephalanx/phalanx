/**
 * React hooks for column CRUD operations.
 *
 * Provides hooks to create, update, and delete columns via the API.
 */

import { useCallback, useState } from "react";
import apiClient from "../api/client";
import type { Column, ColumnCreateRequest, ColumnUpdateRequest } from "../types/board";

/**
 * Hook return type for creating a column.
 */
export interface UseCreateColumnResult {
  /** Create a new column in the given board. */
  createColumn: (data: ColumnCreateRequest) => Promise<Column>;
  /** Whether a creation request is in progress. */
  creating: boolean;
  /** Error message from the last creation attempt, or null. */
  error: string | null;
}

/**
 * Hook for creating a new column within a board.
 *
 * @param boardId - The board ID to create the column in.
 */
export function useCreateColumn(
  boardId: string | undefined,
): UseCreateColumnResult {
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createColumn = useCallback(
    async (data: ColumnCreateRequest): Promise<Column> => {
      if (!boardId) {
        throw new Error("Board ID is required to create a column.");
      }
      setCreating(true);
      setError(null);
      try {
        const response = await apiClient.post<Column>(
          `/boards/${boardId}/columns`,
          data,
        );
        return response.data;
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to create column.";
        setError(message);
        throw err;
      } finally {
        setCreating(false);
      }
    },
    [boardId],
  );

  return { createColumn, creating, error };
}

/**
 * Hook return type for updating a column.
 */
export interface UseUpdateColumnResult {
  /** Update an existing column. */
  updateColumn: (columnId: string, data: ColumnUpdateRequest) => Promise<Column>;
  /** Whether an update request is in progress. */
  updating: boolean;
  /** Error message from the last update attempt, or null. */
  error: string | null;
}

/**
 * Hook for updating an existing column.
 */
export function useUpdateColumn(): UseUpdateColumnResult {
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateColumn = useCallback(
    async (columnId: string, data: ColumnUpdateRequest): Promise<Column> => {
      setUpdating(true);
      setError(null);
      try {
        const response = await apiClient.put<Column>(
          `/columns/${columnId}`,
          data,
        );
        return response.data;
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to update column.";
        setError(message);
        throw err;
      } finally {
        setUpdating(false);
      }
    },
    [],
  );

  return { updateColumn, updating, error };
}

/**
 * Hook return type for deleting a column.
 */
export interface UseDeleteColumnResult {
  /** Delete a column by ID. */
  deleteColumn: (columnId: string) => Promise<void>;
  /** Whether a deletion request is in progress. */
  deleting: boolean;
  /** Error message from the last deletion attempt, or null. */
  error: string | null;
}

/**
 * Hook for deleting a column.
 */
export function useDeleteColumn(): UseDeleteColumnResult {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const deleteColumn = useCallback(async (columnId: string): Promise<void> => {
    setDeleting(true);
    setError(null);
    try {
      await apiClient.delete(`/columns/${columnId}`);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to delete column.";
      setError(message);
      throw err;
    } finally {
      setDeleting(false);
    }
  }, []);

  return { deleteColumn, deleting, error };
}
