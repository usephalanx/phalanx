// ---------------------------------------------------------------------------
// StepCard — numbered step card for the "How It Works" section.
// Displays a numbered circle (indigo background), icon, title, description,
// and a connecting line to the next step via CSS pseudo-element.
// ---------------------------------------------------------------------------

import Icon from '@/components/ui/Icon';
import { cn } from '@/lib/cn';

/** Props for the {@link StepCard} component. */
export interface StepCardProps {
  /** Step number displayed inside the numbered circle. */
  stepNumber: number;
  /** Step title displayed in bold. */
  title: string;
  /** Step description displayed in muted text. */
  description: string;
  /** Lucide icon name rendered next to the step number. */
  icon: string;
  /** Whether to render the connecting line to the next step. Defaults to true. */
  showConnector?: boolean;
  /** Additional CSS classes merged with the base card styles. */
  className?: string;
}

/**
 * A reusable step card that renders a numbered indigo circle with a
 * Lucide icon, a bold title, and a muted description. When `showConnector`
 * is true (the default), a vertical dashed line connects this card to the
 * next step below.
 */
export default function StepCard({
  stepNumber,
  title,
  description,
  icon,
  showConnector = true,
  className,
}: StepCardProps) {
  return (
    <div
      className={cn('relative flex gap-5', className)}
      data-testid="step-card"
    >
      {/* Number circle + connector line */}
      <div className="flex flex-col items-center">
        <div
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-primary-600 text-sm font-bold text-white"
          aria-label={`Step ${stepNumber}`}
        >
          {stepNumber}
        </div>

        {showConnector && (
          <div
            className="mt-2 w-px flex-1 border-l-2 border-dashed border-primary-600/30"
            aria-hidden="true"
            data-testid="step-connector"
          />
        )}
      </div>

      {/* Content */}
      <div className="pb-10">
        <div className="mb-2 flex items-center gap-2">
          <Icon name={icon} className="h-5 w-5 text-primary-400" />
          <h3 className="text-lg font-bold text-white">{title}</h3>
        </div>
        <p className="text-sm leading-relaxed text-text-muted">{description}</p>
      </div>
    </div>
  );
}
