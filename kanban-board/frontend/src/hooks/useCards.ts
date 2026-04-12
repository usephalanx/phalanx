/**
 * React hooks for card CRUD operations.
 *
 * Provides a hook to create a new card within a column via the API.
 */

import { useCallback, useState } from "react";
import apiClient from "../api/client";
import type { Card, CardCreateRequest } from "../types/board";

/**
 * Hook return type for creating a card.
 */
export interface UseCreateCardResult {
  /** Create a new card in the given column. */
  createCard: (columnId: string, data: CardCreateRequest) => Promise<Card>;
  /** Whether a creation request is in progress. */
  creating: boolean;
  /** Error message from the last creation attempt, or null. */
  error: string | null;
}

/**
 * Hook for creating a new card within a column.
 */
export function useCreateCard(): UseCreateCardResult {
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createCard = useCallback(
    async (columnId: string, data: CardCreateRequest): Promise<Card> => {
      setCreating(true);
      setError(null);
      try {
        const response = await apiClient.post<Card>(
          `/columns/${columnId}/cards`,
          data,
        );
        return response.data;
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to create card.";
        setError(message);
        throw err;
      } finally {
        setCreating(false);
      }
    },
    [],
  );

  return { createCard, creating, error };
}
