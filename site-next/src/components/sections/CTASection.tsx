// ---------------------------------------------------------------------------
// CTASection — Full-width call-to-action banner with gradient background.
// Indigo-600 → Violet-600 gradient, white text, large heading + subtitle + CTA.
// ---------------------------------------------------------------------------

import React from 'react';

import Button from '@/components/ui/Button';
import Container from '@/components/ui/Container';
import { SECTION_IDS } from '@/lib/constants';

/** Props for the {@link CTASection} component. */
export interface CTASectionProps {
  /** Large heading displayed in the banner. */
  headline: string;
  /** Supporting subtitle text below the heading. */
  subheadline: string;
  /** Label for the primary CTA button. */
  ctaLabel: string;
  /** Link target for the CTA button. */
  ctaHref: string;
  /** Optional additional CSS classes for the outer section. */
  className?: string;
}

/**
 * A full-width CTA banner section with an indigo-to-violet gradient
 * background, white heading, subtitle, and a large white CTA button.
 *
 * All content is provided via props — no hardcoded strings.
 */
export default function CTASection({
  headline,
  subheadline,
  ctaLabel,
  ctaHref,
  className,
}: CTASectionProps): React.JSX.Element {
  return (
    <section
      id={SECTION_IDS.cta}
      className={className}
      aria-label="Call to action"
    >
      <Container>
        <div className="rounded-2xl bg-gradient-to-r from-indigo-600 to-violet-600 px-6 py-16 text-center shadow-lg sm:px-12 sm:py-20">
          <h2 className="mx-auto max-w-2xl text-3xl font-bold tracking-tight text-white sm:text-4xl lg:text-5xl">
            {headline}
          </h2>

          <p className="mx-auto mt-4 max-w-xl text-lg leading-relaxed text-white/80">
            {subheadline}
          </p>

          <div className="mt-8">
            <Button
              href={ctaHref}
              size="lg"
              className="bg-white text-indigo-600 shadow-md hover:bg-white/90 hover:text-indigo-700 hover:scale-[1.02] active:scale-[0.98]"
            >
              {ctaLabel}
            </Button>
          </div>
        </div>
      </Container>
    </section>
  );
}
