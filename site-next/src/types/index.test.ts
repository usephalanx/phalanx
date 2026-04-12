// ---------------------------------------------------------------------------
// types/index.ts — verify re-exports compile and resolve correctly.
// ---------------------------------------------------------------------------

import type {
  Feature,
  PricingTier,
  FaqItem,
  HowItWorksStep,
  HeroContent,
  NavLink,
  SocialLink,
  CtaBannerContent,
  FooterLinkColumn,
  LogoItem,
  SiteConfig,
} from './index';

describe('types/index re-exports', () => {
  it('Feature interface is usable', () => {
    const f: Feature = {
      icon: 'Brain',
      title: 'Test Feature',
      description: 'A test description.',
    };
    expect(f.title).toBe('Test Feature');
  });

  it('PricingTier interface is usable', () => {
    const tier: PricingTier = {
      id: 'test',
      name: 'Test',
      price: '$0',
      billingPeriod: 'free',
      priceSuffix: 'free',
      features: ['Feature A'],
      cta: 'Start',
      ctaLabel: 'Start',
      ctaHref: '/start',
      highlighted: false,
    };
    expect(tier.name).toBe('Test');
  });

  it('FaqItem interface is usable', () => {
    const faq: FaqItem = {
      id: 'q1',
      question: 'What is this?',
      answer: 'A test FAQ.',
    };
    expect(faq.id).toBe('q1');
  });

  it('HowItWorksStep interface is usable', () => {
    const step: HowItWorksStep = {
      step: 1,
      title: 'Step 1',
      description: 'First step.',
      icon: 'Terminal',
      agentColor: 'agent-cmd',
    };
    expect(step.step).toBe(1);
  });

  it('HeroContent interface is usable', () => {
    const hero: HeroContent = {
      headline: 'Hello World',
      highlightedWord: 'World',
      subheadline: 'Sub text.',
      ctaText: 'Go',
      secondaryCtaText: 'Learn More',
    };
    expect(hero.headline).toContain('World');
  });

  it('NavLink interface is usable', () => {
    const link: NavLink = { label: 'Home', href: '/' };
    expect(link.href).toBe('/');
  });

  it('SocialLink interface is usable', () => {
    const link: SocialLink = {
      platform: 'GitHub',
      href: 'https://github.com',
      icon: 'Github',
    };
    expect(link.platform).toBe('GitHub');
  });

  it('CtaBannerContent interface is usable', () => {
    const cta: CtaBannerContent = {
      headline: 'Ready?',
      subheadline: 'Start now.',
      primaryCta: 'Go',
      secondaryCta: 'Learn',
    };
    expect(cta.headline).toBe('Ready?');
  });

  it('FooterLinkColumn interface is usable', () => {
    const col: FooterLinkColumn = {
      title: 'Product',
      links: [{ label: 'Features', href: '#features' }],
    };
    expect(col.links).toHaveLength(1);
  });

  it('LogoItem interface is usable', () => {
    const logo: LogoItem = { name: 'Vercel' };
    expect(logo.name).toBe('Vercel');
  });

  it('SiteConfig interface is usable', () => {
    const config: SiteConfig = {
      name: 'Test',
      tagline: 'A tagline',
      description: 'Desc.',
      url: 'https://test.com',
      repoUrl: 'https://github.com/test',
      socialLinks: [],
    };
    expect(config.name).toBe('Test');
  });
});
