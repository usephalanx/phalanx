// ---------------------------------------------------------------------------
// LogoBarSection — social proof strip of partner/tech logos below the hero.
// Server component rendering placeholder logo names with grayscale hover.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import type { LogoItem } from '@/data/content';

/** Props for the {@link LogoBarSection} component. */
export interface LogoBarSectionProps {
  /** Array of logo items to display. */
  logos: LogoItem[];
  /** Label text above the logo row. */
  label?: string;
}

/**
 * Social proof strip of partner/technology logos below the hero.
 *
 * Logos render in grayscale at reduced opacity, transitioning to full
 * colour on hover. When SVG paths are provided in the future, they
 * will render as images; for now, text placeholders are used.
 */
export function LogoBarSection({
  logos,
  label = 'Trusted by teams building with',
}: LogoBarSectionProps): React.JSX.Element {
  return (
    <section className="border-y border-border py-10">
      <Container>
        <p className="mb-6 text-center text-sm text-text-muted">{label}</p>
        <div className="flex flex-wrap items-center justify-center gap-8 sm:gap-12">
          {logos.map((logo) => (
            <span
              key={logo.name}
              className="select-none text-lg font-semibold text-text-muted grayscale transition hover:text-white hover:grayscale-0"
              title={logo.name}
            >
              {logo.name}
            </span>
          ))}
        </div>
      </Container>
    </section>
  );
}

export default LogoBarSection;
