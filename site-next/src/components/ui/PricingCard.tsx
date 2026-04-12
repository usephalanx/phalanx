// ---------------------------------------------------------------------------
// PricingCard — displays a single pricing tier with name, price, feature list,
// optional "Popular" badge, and a CTA button. Used in the Pricing section.
// ---------------------------------------------------------------------------

import { cn } from '@/lib/cn';
import Button from '@/components/ui/Button';

/** Props for the {@link PricingCard} component. */
export interface PricingCardProps {
  /** Tier name displayed as the card heading (e.g. "Pro", "Enterprise"). */
  tierName: string;
  /** Price string displayed prominently (e.g. "$49/mo", "Custom"). */
  price: string;
  /** List of features included in this tier. */
  features: string[];
  /** When true, applies a highlight ring and shows a "Popular" badge. */
  highlighted?: boolean;
  /** Label text for the call-to-action button. */
  ctaText: string;
  /** Optional href — when provided the CTA renders as a link. */
  ctaHref?: string;
  /** Optional click handler for the CTA button. */
  onCtaClick?: React.MouseEventHandler<HTMLButtonElement>;
  /** Additional CSS classes merged with the base card styles. */
  className?: string;
}

/**
 * A pricing card component that renders a tier name, price, feature checklist,
 * and a CTA button. When `highlighted` is true, the card receives an indigo
 * ring and a "Popular" badge for visual emphasis.
 */
export default function PricingCard({
  tierName,
  price,
  features,
  highlighted = false,
  ctaText,
  ctaHref,
  onCtaClick,
  className,
}: PricingCardProps) {
  return (
    <div
      className={cn(
        'relative flex flex-col rounded-xl border border-border bg-bg-card p-6 transition-shadow duration-300 hover:shadow-lg hover:shadow-primary-600/10',
        highlighted && 'ring-2 ring-indigo-500',
        className,
      )}
    >
      {/* Popular badge */}
      {highlighted && (
        <span className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-indigo-500 px-3 py-0.5 text-xs font-semibold text-white">
          Popular
        </span>
      )}

      {/* Tier name */}
      <h3 className="mb-2 text-lg font-bold text-white">{tierName}</h3>

      {/* Price */}
      <p className="mb-6 text-3xl font-extrabold tracking-tight text-white">
        {price}
      </p>

      {/* Feature list */}
      <ul className="mb-8 flex flex-1 flex-col gap-3" role="list">
        {features.map((feature) => (
          <li key={feature} className="flex items-start gap-2 text-sm text-text-secondary">
            <svg
              className="mt-0.5 h-4 w-4 shrink-0 text-indigo-400"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path
                fillRule="evenodd"
                d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
                clipRule="evenodd"
              />
            </svg>
            <span>{feature}</span>
          </li>
        ))}
      </ul>

      {/* CTA */}
      <Button
        variant={highlighted ? 'primary' : 'secondary'}
        href={ctaHref}
        onClick={onCtaClick}
        className="w-full"
      >
        {ctaText}
      </Button>
    </div>
  );
}
