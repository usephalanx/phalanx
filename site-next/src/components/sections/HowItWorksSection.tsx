// ---------------------------------------------------------------------------
// HowItWorksSection — four-step flow showing how Phalanx works, from Slack
// command to shipped code. Renders SectionHeading + a responsive StepCard grid.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import SectionHeading from '@/components/ui/SectionHeading';
import StepCard from '@/components/ui/StepCard';
import { SECTION_IDS } from '@/lib/constants';
import type { HowItWorksStep } from '@/data/content';

/** Props for the {@link HowItWorksSection} component. */
export interface HowItWorksSectionProps {
  /** Section heading title. */
  title: string;
  /** Optional subtitle rendered below the heading. */
  subtitle?: string;
  /** Optional overline label rendered above the heading. */
  overline?: string;
  /** Array of step data objects to render as StepCards. */
  steps: HowItWorksStep[];
}

/**
 * "How It Works" landing page section.
 *
 * Displays a section heading followed by a responsive grid of numbered step
 * cards (1 col → 2 cols on md → 4 cols on lg). Each card shows a step number,
 * icon, title, and description. Connector lines are hidden in grid layout.
 */
export function HowItWorksSection({
  title,
  subtitle,
  overline,
  steps,
}: HowItWorksSectionProps): React.JSX.Element {
  return (
    <section
      id={SECTION_IDS.howItWorks}
      data-testid="how-it-works-section"
      className="py-section"
    >
      <Container>
        <SectionHeading
          title={title}
          subtitle={subtitle}
          overline={overline}
        />

        <div className="grid grid-cols-1 gap-8 md:grid-cols-2 lg:grid-cols-4">
          {steps.map((step) => (
            <StepCard
              key={step.step}
              stepNumber={step.step}
              title={step.title}
              description={step.description}
              icon={step.icon}
              showConnector={false}
            />
          ))}
        </div>
      </Container>
    </section>
  );
}

export default HowItWorksSection;
