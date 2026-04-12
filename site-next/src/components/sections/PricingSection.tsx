// ---------------------------------------------------------------------------
// PricingSection — pricing section displaying a SectionHeading and a responsive
// grid of PricingCards. Center tier is highlighted. Receives all data via props.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import PricingCard from '@/components/ui/PricingCard';
import SectionHeading from '@/components/ui/SectionHeading';
import { SECTION_IDS } from '@/lib/constants';
import type { PricingTier } from '@/data/content';

/** Props for the {@link PricingSection} component. */
export interface PricingSectionProps {
  /** Section heading title. */
  title: string;
  /** Optional subtitle rendered below the heading. */
  subtitle?: string;
  /** Optional overline label rendered above the heading. */
  overline?: string;
  /** Array of pricing tier data objects to render as PricingCards. */
  tiers: PricingTier[];
}

/**
 * Pricing landing page section.
 *
 * Displays a section heading followed by a responsive grid of pricing cards
 * (1 col on mobile → 3 cols on md). The highlighted tier receives visual
 * emphasis via the PricingCard `highlighted` prop.
 */
export function PricingSection({
  title,
  subtitle,
  overline,
  tiers,
}: PricingSectionProps): React.JSX.Element {
  return (
    <section
      id={SECTION_IDS.pricing}
      data-testid="pricing-section"
      className="py-section"
    >
      <Container>
        <SectionHeading
          title={title}
          subtitle={subtitle}
          overline={overline}
        />

        <div className="grid grid-cols-1 gap-8 md:grid-cols-3">
          {tiers.map((tier) => (
            <PricingCard
              key={tier.id}
              tierName={tier.name}
              price={tier.price}
              features={tier.features}
              highlighted={tier.highlighted}
              ctaText={tier.ctaLabel}
              ctaHref={tier.ctaHref}
            />
          ))}
        </div>
      </Container>
    </section>
  );
}

export default PricingSection;
