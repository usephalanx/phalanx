/**
 * Board detail page — displays columns as vertical lanes with drag-and-drop
 * support, card components, add-card forms, and an add-column button.
 *
 * Fetches the full board (columns + cards) via GET /api/boards/{id}.
 * Columns are rendered as fixed-width lanes in a horizontal scroll container.
 * Cards within each column are sorted by position.
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { DragDropContext, Droppable, Draggable } from "@hello-pangea/dnd";
import type { DropResult } from "@hello-pangea/dnd";
import apiClient from "../api/client";
import type { BoardDetail, Card, ColumnWithCards } from "../types/board";
import CardComponent from "../components/CardComponent";
import ColumnHeader from "../components/ColumnHeader";
import AddCardForm from "../components/AddCardForm";
import AddColumnButton from "../components/AddColumnButton";
import { useCreateColumn, useUpdateColumn, useDeleteColumn } from "../hooks/useColumns";
import { useCreateCard } from "../hooks/useCards";

/**
 * Board page component that shows columns with cards and supports
 * drag-and-drop reordering, inline column editing, and card/column creation.
 */
export default function BoardPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const [board, setBoard] = useState<BoardDetail | null>(null);
  const [columns, setColumns] = useState<ColumnWithCards[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Column CRUD hooks
  const { createColumn, creating: creatingColumn } = useCreateColumn(id);
  const { updateColumn } = useUpdateColumn();
  const { deleteColumn } = useDeleteColumn();

  // Card creation hook
  const { createCard, creating: creatingCard } = useCreateCard();

  /**
   * Fetch the full board with columns and cards from the API.
   */
  const fetchBoard = useCallback(async (): Promise<void> => {
    try {
      setLoading(true);
      const response = await apiClient.get<BoardDetail>(`/boards/${id}`);
      setBoard(response.data);
      // Sort cards within each column by position
      const sortedColumns = response.data.columns.map((col) => ({
        ...col,
        cards: [...col.cards].sort((a, b) => a.position - b.position),
      }));
      setColumns(sortedColumns);
    } catch {
      setError("Failed to load board.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    let cancelled = false;

    async function load(): Promise<void> {
      try {
        const response = await apiClient.get<BoardDetail>(`/boards/${id}`);
        if (!cancelled) {
          setBoard(response.data);
          const sortedColumns = response.data.columns.map((col) => ({
            ...col,
            cards: [...col.cards].sort((a, b) => a.position - b.position),
          }));
          setColumns(sortedColumns);
        }
      } catch {
        if (!cancelled) {
          setError("Failed to load board.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [id]);

  /**
   * Handle column drag-and-drop reordering.
   */
  function handleDragEnd(result: DropResult): void {
    if (!result.destination) return;
    if (result.destination.index === result.source.index) return;

    const reordered = Array.from(columns);
    const [moved] = reordered.splice(result.source.index, 1);
    if (!moved) return;
    reordered.splice(result.destination.index, 0, moved);

    setColumns(reordered);

    // Persist the new order to the backend
    const columnIds = reordered.map((col) => col.id);
    void apiClient
      .patch("/columns/reorder", { column_ids: columnIds })
      .catch(() => {
        // Revert on failure
        setColumns(columns);
      });
  }

  /**
   * Handle adding a new column to the board.
   */
  async function handleAddColumn(name: string): Promise<void> {
    try {
      const newCol = await createColumn({ name });
      // Append new column with empty cards array
      const colWithCards: ColumnWithCards = { ...newCol, cards: [] };
      setColumns((prev) => [...prev, colWithCards]);
    } catch {
      // Error is handled inside the hook
    }
  }

  /**
   * Handle editing a column title.
   */
  async function handleEditColumn(columnId: string, newTitle: string): Promise<void> {
    try {
      await updateColumn(columnId, { name: newTitle });
      setColumns((prev) =>
        prev.map((col) =>
          col.id === columnId ? { ...col, name: newTitle } : col,
        ),
      );
    } catch {
      // Error is handled inside the hook
    }
  }

  /**
   * Handle deleting a column.
   */
  async function handleDeleteColumn(columnId: string): Promise<void> {
    const confirmed = window.confirm(
      "Delete this column and all its cards? This cannot be undone.",
    );
    if (!confirmed) return;

    try {
      await deleteColumn(columnId);
      setColumns((prev) => prev.filter((col) => col.id !== columnId));
    } catch {
      // Error is handled inside the hook
    }
  }

  /**
   * Handle adding a new card to a column.
   */
  async function handleAddCard(
    columnId: string,
    title: string,
    description: string,
  ): Promise<void> {
    try {
      const newCard: Card = await createCard(columnId, {
        title,
        description: description || null,
      });
      setColumns((prev) =>
        prev.map((col) =>
          col.id === columnId
            ? { ...col, cards: [...col.cards, newCard] }
            : col,
        ),
      );
    } catch {
      // Error is handled inside the hook
    }
  }

  // ── Loading state ──────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary-500 border-t-transparent" />
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────────────────────

  if (error || !board) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <div className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          {error ?? "Board not found."}
        </div>
      </div>
    );
  }

  // ── Board content ──────────────────────────────────────────────────────────

  return (
    <div className="flex flex-1 flex-col">
      {/* Breadcrumb bar */}
      <div className="border-b border-gray-200 bg-white px-4 py-3">
        <div className="flex items-center gap-2 text-sm">
          <Link
            to={`/workspaces/${board.workspace_id}`}
            className="text-gray-500 hover:text-gray-700"
          >
            Workspace
          </Link>
          <span className="text-gray-300">/</span>
          <span className="font-medium text-gray-900">{board.name}</span>
        </div>
      </div>

      {/* Board columns — horizontal scroll container */}
      <div className="flex-1 overflow-x-auto p-4">
        <DragDropContext onDragEnd={handleDragEnd}>
          <Droppable droppableId="board-columns" direction="horizontal">
            {(provided) => (
              <div
                ref={provided.innerRef}
                {...provided.droppableProps}
                className="flex items-start gap-4"
              >
                {columns.map((column, index) => (
                  <Draggable
                    key={column.id}
                    draggableId={`column-${column.id}`}
                    index={index}
                  >
                    {(dragProvided) => (
                      <div
                        ref={dragProvided.innerRef}
                        {...dragProvided.draggableProps}
                        {...dragProvided.dragHandleProps}
                        className="w-72 flex-shrink-0 rounded-lg bg-gray-100 p-4"
                        data-testid={`column-${column.id}`}
                      >
                        {/* Column header with title, count, edit/delete */}
                        <ColumnHeader
                          title={column.name}
                          cardCount={column.cards.length}
                          onEdit={(newTitle) =>
                            void handleEditColumn(column.id, newTitle)
                          }
                          onDelete={() => void handleDeleteColumn(column.id)}
                        />

                        {/* Cards list sorted by position */}
                        <div className="space-y-2">
                          {column.cards.length === 0 && (
                            <p className="text-center text-xs text-gray-400">
                              No cards yet
                            </p>
                          )}
                          {column.cards.map((card) => (
                            <CardComponent key={card.id} card={card} />
                          ))}
                        </div>

                        {/* Add card form at the bottom */}
                        <AddCardForm
                          loading={creatingCard}
                          onSubmit={(title, description) =>
                            void handleAddCard(column.id, title, description)
                          }
                        />
                      </div>
                    )}
                  </Draggable>
                ))}
                {provided.placeholder}

                {/* Add column button at the end */}
                <AddColumnButton
                  loading={creatingColumn}
                  onSubmit={(name) => void handleAddColumn(name)}
                />
              </div>
            )}
          </Droppable>
        </DragDropContext>

        {columns.length === 0 && (
          <p className="mt-8 text-center text-gray-500">
            No columns yet. Add a column to get started.
          </p>
        )}
      </div>
    </div>
  );
}
