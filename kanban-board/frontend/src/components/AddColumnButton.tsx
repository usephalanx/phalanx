/**
 * Add-column button and inline form for creating a new board column.
 *
 * Toggles between a compact "+ Add column" button and an expanded
 * inline input with submit/cancel actions.
 */

import { useState } from "react";

interface AddColumnButtonProps {
  /** Whether a creation request is currently in flight. */
  loading: boolean;
  /** Callback fired when the user submits a new column name. */
  onSubmit: (name: string) => void;
}

/**
 * Renders either a placeholder add-column button or an inline creation form.
 */
export default function AddColumnButton({
  loading,
  onSubmit,
}: AddColumnButtonProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");

  /**
   * Handle form submission.
   */
  function handleSubmit(e: React.FormEvent): void {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
    setName("");
    setOpen(false);
  }

  /**
   * Cancel and reset the form.
   */
  function handleCancel(): void {
    setName("");
    setOpen(false);
  }

  /**
   * Handle keyboard events on the input.
   */
  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>): void {
    if (e.key === "Escape") {
      handleCancel();
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex h-fit w-72 flex-shrink-0 items-center gap-2 rounded-lg border-2 border-dashed border-gray-300 bg-gray-50 px-4 py-3 text-sm font-medium text-gray-500 transition hover:border-gray-400 hover:bg-gray-100 hover:text-gray-700"
        data-testid="add-column-btn"
      >
        <svg
          className="h-5 w-5"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
        Add column
      </button>
    );
  }

  return (
    <div
      className="w-72 flex-shrink-0 rounded-lg bg-gray-100 p-4"
      data-testid="add-column-form"
    >
      <form onSubmit={handleSubmit} className="space-y-2">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Column title"
          className="w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm shadow-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
          data-testid="add-column-input"
          autoFocus
          required
        />
        <div className="flex items-center gap-2">
          <button
            type="submit"
            disabled={loading || !name.trim()}
            className="rounded-md bg-primary-600 px-3 py-1 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="add-column-submit"
          >
            {loading ? "Adding\u2026" : "Add column"}
          </button>
          <button
            type="button"
            onClick={handleCancel}
            className="rounded-md px-3 py-1 text-sm text-gray-500 hover:bg-gray-200 hover:text-gray-700"
            data-testid="add-column-cancel"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}
