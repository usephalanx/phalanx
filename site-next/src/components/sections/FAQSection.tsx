// ---------------------------------------------------------------------------
// FAQSection — FAQ section displaying a SectionHeading and a list of FAQItems
// in a centered, max-w-3xl container. Receives all data via props.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import FAQItem from '@/components/ui/FAQItem';
import SectionHeading from '@/components/ui/SectionHeading';
import { SECTION_IDS } from '@/lib/constants';
import type { FaqItem } from '@/data/content';

/** Props for the {@link FAQSection} component. */
export interface FAQSectionProps {
  /** Section heading title. */
  title: string;
  /** Optional subtitle rendered below the heading. */
  subtitle?: string;
  /** Optional overline label rendered above the heading. */
  overline?: string;
  /** Array of FAQ item data objects to render as FAQItems. */
  items: FaqItem[];
}

/**
 * FAQ landing page section.
 *
 * Displays a section heading followed by a vertically stacked list of
 * accordion FAQ items centered within a `max-w-3xl` container.
 */
export function FAQSection({
  title,
  subtitle,
  overline,
  items,
}: FAQSectionProps): React.JSX.Element {
  return (
    <section
      id={SECTION_IDS.faq}
      data-testid="faq-section"
      className="py-section"
    >
      <Container>
        <SectionHeading
          title={title}
          subtitle={subtitle}
          overline={overline}
        />

        <div className="mx-auto max-w-3xl">
          {items.map((item) => (
            <FAQItem
              key={item.id}
              question={item.question}
              answer={item.answer}
            />
          ))}
        </div>
      </Container>
    </section>
  );
}

export default FAQSection;
