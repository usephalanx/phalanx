/**
 * Inline add-card form rendered at the bottom of each column.
 *
 * Toggles between a "+ Add a card" button and an expanded form with
 * title input, optional description textarea, and submit/cancel buttons.
 */

import { useState } from "react";

interface AddCardFormProps {
  /** Whether a creation request is currently in flight. */
  loading: boolean;
  /** Callback fired when the user submits a new card. */
  onSubmit: (title: string, description: string) => void;
}

/**
 * Collapsible form for creating a new card within a column.
 */
export default function AddCardForm({
  loading,
  onSubmit,
}: AddCardFormProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  /**
   * Handle form submission. Trims title and delegates to parent.
   */
  function handleSubmit(e: React.FormEvent): void {
    e.preventDefault();
    const trimmedTitle = title.trim();
    if (!trimmedTitle) return;
    onSubmit(trimmedTitle, description.trim());
    setTitle("");
    setDescription("");
    setOpen(false);
  }

  /**
   * Cancel and reset the form.
   */
  function handleCancel(): void {
    setTitle("");
    setDescription("");
    setOpen(false);
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-2 flex w-full items-center gap-1 rounded-md px-2 py-1.5 text-sm text-gray-500 hover:bg-gray-200 hover:text-gray-700"
        data-testid="add-card-btn"
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
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
        Add a card
      </button>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 space-y-2" data-testid="add-card-form">
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Card title"
        className="w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm shadow-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
        data-testid="add-card-title"
        autoFocus
        required
      />
      <textarea
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="Description (optional)"
        rows={2}
        className="w-full resize-none rounded-md border border-gray-300 px-2.5 py-1.5 text-sm shadow-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
        data-testid="add-card-description"
      />
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={loading || !title.trim()}
          className="rounded-md bg-primary-600 px-3 py-1 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
          data-testid="add-card-submit"
        >
          {loading ? "Adding\u2026" : "Add card"}
        </button>
        <button
          type="button"
          onClick={handleCancel}
          className="rounded-md px-3 py-1 text-sm text-gray-500 hover:bg-gray-200 hover:text-gray-700"
          data-testid="add-card-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
