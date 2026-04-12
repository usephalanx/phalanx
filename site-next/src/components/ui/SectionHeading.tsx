// ---------------------------------------------------------------------------
// SectionHeading — consistent section title block with overline, heading,
// and optional subtitle used across all landing page sections.
// ---------------------------------------------------------------------------

import { cn } from '@/lib/cn';

/** Props for the {@link SectionHeading} component. */
export interface SectionHeadingProps {
  /** The main heading text. */
  title: string;
  /** Optional subtitle displayed below the heading. */
  subtitle?: string;
  /** Optional overline label displayed above the heading. */
  overline?: string;
  /** Whether content is center-aligned — defaults to `true`. */
  centered?: boolean;
  /** Additional CSS classes merged with the base styles. */
  className?: string;
}

/**
 * A reusable section heading block that renders an overline label,
 * a large bold heading (`text-3xl md:text-4xl`), and a muted subtitle
 * paragraph. Used at the top of each landing page section.
 */
export default function SectionHeading({
  title,
  subtitle,
  overline,
  centered = true,
  className,
}: SectionHeadingProps) {
  return (
    <div
      className={cn(
        'mb-12',
        centered && 'text-center',
        className,
      )}
    >
      {overline && (
        <p className="mb-3 text-sm font-semibold uppercase tracking-widest text-primary-400">
          {overline}
        </p>
      )}
      <h2 className="text-3xl font-bold tracking-tight text-white md:text-4xl">
        {title}
      </h2>
      {subtitle && (
        <p className="mt-4 max-w-2xl text-lg leading-relaxed text-text-secondary mx-auto">
          {subtitle}
        </p>
      )}
    </div>
  );
}
