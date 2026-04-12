/**
 * Column header component with title, card count badge, and edit/delete actions.
 *
 * Used at the top of each column lane on the Kanban board.
 */

import { useState } from "react";

interface ColumnHeaderProps {
  /** The column title. */
  title: string;
  /** Number of cards in this column. */
  cardCount: number;
  /** Callback fired when the user confirms an edit to the column title. */
  onEdit: (newTitle: string) => void;
  /** Callback fired when the user confirms column deletion. */
  onDelete: () => void;
}

/**
 * Renders the column header row with:
 * - Column title (inline-editable on edit click)
 * - Card count badge
 * - Edit (pencil) button
 * - Delete (trash) button
 */
export default function ColumnHeader({
  title,
  cardCount,
  onEdit,
  onDelete,
}: ColumnHeaderProps): JSX.Element {
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(title);

  /**
   * Commit the inline edit if the value is non-empty.
   */
  function handleEditSubmit(): void {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== title) {
      onEdit(trimmed);
    }
    setEditing(false);
  }

  /**
   * Handle keyboard events inside the inline edit input.
   */
  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>): void {
    if (e.key === "Enter") {
      handleEditSubmit();
    } else if (e.key === "Escape") {
      setEditValue(title);
      setEditing(false);
    }
  }

  return (
    <div className="mb-3 flex items-center justify-between gap-2">
      {/* Left: title + card count */}
      <div className="flex min-w-0 items-center gap-2">
        {editing ? (
          <input
            type="text"
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={handleEditSubmit}
            onKeyDown={handleKeyDown}
            className="w-full rounded border border-gray-300 px-2 py-0.5 text-sm font-semibold text-gray-800 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
            data-testid="column-title-input"
            autoFocus
          />
        ) : (
          <h2
            className="truncate font-semibold text-gray-800"
            data-testid="column-title"
          >
            {title}
          </h2>
        )}
        <span
          className="inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full bg-gray-200 px-1.5 text-xs font-medium text-gray-600"
          data-testid="column-card-count"
        >
          {cardCount}
        </span>
      </div>

      {/* Right: action buttons */}
      <div className="flex items-center gap-1">
        {/* Edit button */}
        <button
          type="button"
          onClick={() => {
            setEditValue(title);
            setEditing(true);
          }}
          className="rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600"
          aria-label="Edit column"
          data-testid="column-edit-btn"
        >
          <svg
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
          </svg>
        </button>

        {/* Delete button */}
        <button
          type="button"
          onClick={onDelete}
          className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-500"
          aria-label="Delete column"
          data-testid="column-delete-btn"
        >
          <svg
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="3 6 5 6 21 6" />
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
          </svg>
        </button>
      </div>
    </div>
  );
}
