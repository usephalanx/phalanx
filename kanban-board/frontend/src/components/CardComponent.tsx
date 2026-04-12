/**
 * Card component that displays a single Kanban card within a column.
 *
 * Shows the card title, a truncated description preview, and an assignee
 * avatar circle (initials-based placeholder when no avatar image is available).
 */

import type { Card } from "../types/board";

/** Maximum number of characters to show in the description preview. */
const DESCRIPTION_PREVIEW_LENGTH = 80;

interface CardComponentProps {
  /** The card data to render. */
  card: Card;
}

/**
 * Truncate a string to the given length, appending an ellipsis if needed.
 */
function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) {
    return text;
  }
  return text.slice(0, maxLength).trimEnd() + "\u2026";
}

/**
 * A single Kanban card rendered inside a column lane.
 *
 * Displays:
 * - Card title (bold)
 * - Description preview (truncated, gray text)
 * - Assignee avatar placeholder (if assignee_id is present)
 */
export default function CardComponent({ card }: CardComponentProps): JSX.Element {
  return (
    <div
      className="rounded-md border border-gray-200 bg-white p-3 shadow-sm transition hover:shadow-md"
      data-testid={`card-${card.id}`}
    >
      {/* Title */}
      <p className="text-sm font-medium text-gray-800">{card.title}</p>

      {/* Description preview */}
      {card.description && (
        <p className="mt-1 text-xs leading-relaxed text-gray-500">
          {truncate(card.description, DESCRIPTION_PREVIEW_LENGTH)}
        </p>
      )}

      {/* Assignee avatar */}
      {card.assignee_id && (
        <div className="mt-2 flex items-center gap-1.5">
          <div
            className="flex h-6 w-6 items-center justify-center rounded-full bg-primary-100 text-[10px] font-semibold text-primary-700"
            title={`Assigned to user ${card.assignee_id}`}
            data-testid={`card-${card.id}-avatar`}
          >
            <svg
              className="h-3.5 w-3.5"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
              <circle cx="12" cy="7" r="4" />
            </svg>
          </div>
        </div>
      )}
    </div>
  );
}
