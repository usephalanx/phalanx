// ---------------------------------------------------------------------------
// FeatureCard — card component displaying a feature with icon, title,
// and description. Used in the Features section of the landing page.
// ---------------------------------------------------------------------------

import { cn } from '@/lib/cn';

/** Props for the {@link FeatureCard} component. */
export interface FeatureCardProps {
  /** Icon content — an emoji string or SVG placeholder text. */
  icon: string;
  /** Feature title displayed in bold. */
  title: string;
  /** Feature description displayed in muted text. */
  description: string;
  /** Additional CSS classes merged with the base card styles. */
  className?: string;
}

/**
 * A reusable feature card that renders an icon inside a colored circle,
 * a bold title, and a muted description. Includes subtle border,
 * rounded corners, padding, and a hover shadow transition.
 */
export default function FeatureCard({
  icon,
  title,
  description,
  className,
}: FeatureCardProps) {
  return (
    <div
      className={cn(
        'rounded-xl border border-border bg-bg-card p-6 transition-shadow duration-300 hover:shadow-lg hover:shadow-primary-600/10',
        className,
      )}
    >
      <div
        className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-primary-600/15 text-xl"
        aria-hidden="true"
      >
        {icon}
      </div>
      <h3 className="mb-2 text-lg font-bold text-white">{title}</h3>
      <p className="text-sm leading-relaxed text-text-muted">{description}</p>
    </div>
  );
}
