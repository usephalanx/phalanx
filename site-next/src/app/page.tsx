/**
 * Root page — single-page marketing site for Phalanx.
 * Server Component. Composes all section components in order.
 */

import React from 'react';

import Navbar from '@/components/Navbar';
import { HeroSection } from '@/components/sections/HeroSection';
import { LogoBarSection } from '@/components/sections/LogoBarSection';
import { FeaturesSection } from '@/components/sections/FeaturesSection';
import { HowItWorksSection } from '@/components/sections/HowItWorksSection';
import { PricingSection } from '@/components/sections/PricingSection';
import { FAQSection } from '@/components/sections/FAQSection';
import CTASection from '@/components/sections/CTASection';
import FooterSection from '@/components/sections/FooterSection';

import {
  heroContent,
  logoItems,
  features,
  howItWorksSteps,
  pricingTiers,
  faqItems,
  ctaBannerContent,
  footerContent,
  navLinks,
  socialLinks,
  siteConfig,
} from '@/data/content';
import { WAITLIST_URL } from '@/lib/constants';

// ---------------------------------------------------------------------------
// Footer link columns (static, defined at module level to avoid re-creation)
// ---------------------------------------------------------------------------

const footerColumns = [
  {
    title: 'Product',
    links: [
      { label: 'Features', href: '#features' },
      { label: 'How It Works', href: '#how-it-works' },
      { label: 'Pricing', href: '#pricing' },
      { label: 'FAQ', href: '#faq' },
    ],
  },
  {
    title: 'Resources',
    links: [
      { label: 'Documentation', href: '/docs' },
      { label: 'GitHub', href: 'https://github.com/phalanx-dev' },
      { label: 'Changelog', href: '/changelog' },
    ],
  },
  {
    title: 'Company',
    links: [
      { label: 'About', href: '/about' },
      { label: 'Blog', href: '/blog' },
      { label: 'Privacy', href: '/privacy' },
      { label: 'Terms', href: '/terms' },
    ],
  },
];

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/** Landing page — assembles all sections with proper spacing. */
export default function HomePage(): React.JSX.Element {
  return (
    <>
      <Navbar
        brandName={siteConfig.name}
        navLinks={navLinks}
        ctaLabel="Get Started"
        ctaHref={WAITLIST_URL}
      />

      <main className="min-h-screen">
        {/* Hero + Logo bar (no extra top padding — hero has its own) */}
        <HeroSection heroContent={heroContent} />
        <LogoBarSection logos={logoItems} />

        {/* Features */}
        <FeaturesSection
          overline="Capabilities"
          title="Everything you need to ship autonomously"
          subtitle="Six specialized AI agents coordinate end-to-end — from planning through production release."
          features={features}
        />

        {/* How It Works */}
        <HowItWorksSection
          overline="Workflow"
          title="From Slack command to shipped code"
          subtitle="One command kicks off an autonomous pipeline. You approve — agents execute."
          steps={howItWorksSteps}
        />

        {/* Pricing */}
        <PricingSection
          overline="Pricing"
          title="Simple, transparent pricing"
          subtitle="Start free. Scale when you're ready."
          tiers={pricingTiers}
        />

        {/* FAQ */}
        <FAQSection
          overline="FAQ"
          title="Frequently asked questions"
          subtitle="Everything you need to know about Phalanx."
          items={faqItems}
        />

        {/* CTA Banner */}
        <CTASection
          headline={ctaBannerContent.headline}
          subheadline={ctaBannerContent.subheadline}
          ctaLabel={ctaBannerContent.primaryCta}
          ctaHref={WAITLIST_URL}
        />
      </main>

      <FooterSection
        brandName={siteConfig.name}
        tagline={footerContent.tagline}
        linkColumns={footerColumns}
        socialLinks={socialLinks}
        copyright={footerContent.copyright}
      />
    </>
  );
}
