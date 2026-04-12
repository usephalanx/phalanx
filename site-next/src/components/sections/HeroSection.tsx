// ---------------------------------------------------------------------------
// HeroSection — above-the-fold hero with headline, dual CTAs, and terminal.
// Server component wrapping a client TerminalDemo child.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import { TerminalDemo } from '@/components/sections/TerminalDemo';
import { SECTION_IDS } from '@/lib/constants';
import type { HeroContent } from '@/data/content';

/** Props for the {@link HeroSection} component. */
export interface HeroSectionProps {
  /** Hero content data sourced from @/data/content. */
  heroContent: HeroContent;
}

/**
 * Above-the-fold hero section with headline, subheadline, dual CTAs,
 * and an animated terminal demonstration.
 *
 * Layout: two-column on lg+ (text left, terminal right), stacked on mobile.
 * Features an animated indigo→violet→purple gradient background.
 */
export function HeroSection({ heroContent }: HeroSectionProps): React.JSX.Element {
  return (
    <section
      id={SECTION_IDS.hero}
      data-testid="hero-section"
      className="relative min-h-screen overflow-hidden pb-16 pt-24 sm:pb-24 sm:pt-32"
    >
      {/* Animated gradient background */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 animate-[gradient-shift_8s_ease-in-out_infinite] bg-gradient-to-br from-primary-900/40 via-accent-900/30 to-primary-950/50"
      />
      {/* Radial glow accent */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute left-1/2 top-1/4 -z-10 h-[600px] w-[600px] -translate-x-1/2 rounded-full bg-accent-600/10 blur-3xl"
      />

      <Container>
        <div className="grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
          {/* Text column */}
          <div className="flex flex-col items-start gap-6">
            <h1 className="font-display text-4xl font-extrabold tracking-tight text-white sm:text-5xl lg:text-6xl">
              {heroContent.headline}
            </h1>
            <p className="max-w-xl text-lg leading-relaxed text-text-secondary">
              {heroContent.subheadline}
            </p>
            <div className="flex flex-wrap gap-4">
              <a
                href={heroContent.primaryCta.href}
                className="inline-flex items-center rounded-lg bg-brand-blue px-6 py-3 text-sm font-semibold text-bg transition hover:bg-brand-blue/90 shadow-lg shadow-brand-blue/20"
              >
                {heroContent.primaryCta.label}
              </a>
              <a
                href={heroContent.secondaryCta.href}
                className="inline-flex items-center rounded-lg border border-border px-6 py-3 text-sm font-semibold text-text transition hover:border-border-hover"
              >
                {heroContent.secondaryCta.label}
              </a>
            </div>
          </div>

          {/* Terminal column */}
          <div className="flex justify-center lg:justify-end">
            <TerminalDemo className="w-full max-w-lg" />
          </div>
        </div>
      </Container>
    </section>
  );
}

export default HeroSection;
