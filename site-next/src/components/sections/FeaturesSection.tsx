// ---------------------------------------------------------------------------
// FeaturesSection — responsive grid of FeatureCards with a section heading.
// Receives all data via props — no hardcoded strings.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import FeatureCard from '@/components/ui/FeatureCard';
import SectionHeading from '@/components/ui/SectionHeading';
import { SECTION_IDS } from '@/lib/constants';
import type { Feature } from '@/data/content';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

/** Props for the {@link FeaturesSection} component. */
export interface FeaturesSectionProps {
  /** Section heading title. */
  title: string;
  /** Optional subtitle rendered below the heading. */
  subtitle?: string;
  /** Optional overline label rendered above the heading. */
  overline?: string;
  /** Array of feature data objects to render as FeatureCards. */
  features: Feature[];
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Features section displaying a SectionHeading and a responsive 2×3 grid of
 * FeatureCards. Each card shows an icon, title, and description.
 */
export function FeaturesSection({
  title,
  subtitle,
  overline,
  features,
}: FeaturesSectionProps): React.JSX.Element {
  return (
    <section id={SECTION_IDS.features} className="py-24">
      <Container>
        <SectionHeading title={title} subtitle={subtitle} overline={overline} />

        <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {features.map((feature) => (
            <FeatureCard
              key={feature.title}
              icon={feature.icon}
              title={feature.title}
              description={feature.description}
            />
          ))}
        </div>
      </Container>
    </section>
  );
}

export default FeaturesSection;
