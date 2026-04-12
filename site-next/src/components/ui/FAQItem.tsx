// ---------------------------------------------------------------------------
// FAQItem — accordion component using native <details>/<summary> for
// zero-JS interactivity. Displays a question/answer pair with a chevron
// indicator that rotates on open.
// ---------------------------------------------------------------------------

import { cn } from '@/lib/cn';

/** Props for the {@link FAQItem} component. */
export interface FAQItemProps {
  /** The question displayed in the summary trigger. */
  question: string;
  /** The answer revealed when the accordion is expanded. */
  answer: string;
  /** Whether the item should be open by default. */
  defaultOpen?: boolean;
  /** Additional CSS classes merged with the root element. */
  className?: string;
}

/**
 * A single FAQ accordion item built on the native `<details>/<summary>`
 * elements for zero-JS expand/collapse. A chevron indicator rotates when
 * the item is open. The answer panel animates open via a CSS grid-rows
 * transition.
 */
export default function FAQItem({
  question,
  answer,
  defaultOpen = false,
  className,
}: FAQItemProps) {
  return (
    <details
      className={cn('group border-b border-border py-4', className)}
      open={defaultOpen || undefined}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-base font-semibold text-white transition-colors hover:text-primary-400 [&::-webkit-details-marker]:hidden">
        {question}
        <svg
          className="h-5 w-5 shrink-0 text-text-muted transition-transform duration-300 group-open:rotate-180"
          aria-hidden="true"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M19.5 8.25l-7.5 7.5-7.5-7.5"
          />
        </svg>
      </summary>

      <div className="grid grid-rows-[0fr] transition-[grid-template-rows] duration-300 group-open:grid-rows-[1fr]">
        <div className="overflow-hidden">
          <p className="pt-3 text-sm leading-relaxed text-text-secondary">
            {answer}
          </p>
        </div>
      </div>
    </details>
  );
}
