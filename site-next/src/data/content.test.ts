// ---------------------------------------------------------------------------
// content.ts — unit tests for mock data constants and type conformance.
// ---------------------------------------------------------------------------

import {
  features,
  pricingTiers,
  faqItems,
  howItWorksSteps,
  heroContent,
  navLinks,
  socialLinks,
  footerContent,
  ctaBannerContent,
  logoItems,
  siteConfig,
} from './content';

import type {
  Feature,
  PricingTier,
  FaqItem,
  HowItWorksStep,
  HeroContent,
  NavLink,
  SocialLink,
  CtaBannerContent,
  LogoItem,
  SiteConfig,
} from './content';

/* ------------------------------------------------------------------ */
/*  features                                                           */
/* ------------------------------------------------------------------ */

describe('features', () => {
  it('exports exactly 6 features', () => {
    expect(features).toHaveLength(6);
  });

  it('every feature has required fields', () => {
    features.forEach((f: Feature) => {
      expect(typeof f.icon).toBe('string');
      expect(f.icon.length).toBeGreaterThan(0);
      expect(typeof f.title).toBe('string');
      expect(f.title.length).toBeGreaterThan(0);
      expect(typeof f.description).toBe('string');
      expect(f.description.length).toBeGreaterThan(0);
    });
  });

  it('includes the expected feature titles', () => {
    const titles = features.map((f) => f.title);
    expect(titles).toContain('AI Planning');
    expect(titles).toContain('Auto Code Review');
    expect(titles).toContain('Security Scanning');
    expect(titles).toContain('Automated QA');
    expect(titles).toContain('Config-Driven Pipelines');
    expect(titles).toContain('Human-in-the-Loop');
  });
});

/* ------------------------------------------------------------------ */
/*  pricingTiers                                                       */
/* ------------------------------------------------------------------ */

describe('pricingTiers', () => {
  it('exports exactly 3 tiers', () => {
    expect(pricingTiers).toHaveLength(3);
  });

  it('tiers are Starter, Pro, Enterprise', () => {
    const names = pricingTiers.map((t) => t.name);
    expect(names).toEqual(['Starter', 'Pro', 'Enterprise']);
  });

  it('every tier has required fields', () => {
    pricingTiers.forEach((t: PricingTier) => {
      expect(typeof t.id).toBe('string');
      expect(typeof t.name).toBe('string');
      expect(typeof t.price).toBe('string');
      expect(typeof t.billingPeriod).toBe('string');
      expect(Array.isArray(t.features)).toBe(true);
      expect(t.features.length).toBeGreaterThan(0);
      expect(typeof t.highlighted).toBe('boolean');
      expect(typeof t.ctaLabel).toBe('string');
      expect(typeof t.ctaHref).toBe('string');
    });
  });

  it('exactly one tier is highlighted', () => {
    const highlighted = pricingTiers.filter((t) => t.highlighted);
    expect(highlighted).toHaveLength(1);
    expect(highlighted[0].name).toBe('Pro');
  });
});

/* ------------------------------------------------------------------ */
/*  howItWorksSteps                                                    */
/* ------------------------------------------------------------------ */

describe('howItWorksSteps', () => {
  it('exports exactly 4 steps', () => {
    expect(howItWorksSteps).toHaveLength(4);
  });

  it('steps are numbered 1 through 4', () => {
    const numbers = howItWorksSteps.map((s) => s.step);
    expect(numbers).toEqual([1, 2, 3, 4]);
  });

  it('every step has required fields', () => {
    howItWorksSteps.forEach((s: HowItWorksStep) => {
      expect(typeof s.step).toBe('number');
      expect(typeof s.title).toBe('string');
      expect(typeof s.description).toBe('string');
      expect(typeof s.icon).toBe('string');
      expect(typeof s.agentColor).toBe('string');
      expect(s.agentColor).toMatch(/^agent-/);
    });
  });

  it('follows the expected flow', () => {
    const titles = howItWorksSteps.map((s) => s.title);
    expect(titles).toEqual(['Slack Command', 'AI Plans', 'Builds & Reviews', 'Ships']);
  });
});

/* ------------------------------------------------------------------ */
/*  faqItems                                                           */
/* ------------------------------------------------------------------ */

describe('faqItems', () => {
  it('exports 5 or 6 FAQ items', () => {
    expect(faqItems.length).toBeGreaterThanOrEqual(5);
    expect(faqItems.length).toBeLessThanOrEqual(6);
  });

  it('every FAQ has a unique id', () => {
    const ids = faqItems.map((f) => f.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('every FAQ has a question and answer', () => {
    faqItems.forEach((f: FaqItem) => {
      expect(typeof f.id).toBe('string');
      expect(f.question.length).toBeGreaterThan(0);
      expect(f.answer.length).toBeGreaterThan(0);
    });
  });
});

/* ------------------------------------------------------------------ */
/*  heroContent                                                        */
/* ------------------------------------------------------------------ */

describe('heroContent', () => {
  it('has all required fields', () => {
    const h: HeroContent = heroContent;
    expect(typeof h.headline).toBe('string');
    expect(typeof h.highlightedWord).toBe('string');
    expect(typeof h.subheadline).toBe('string');
    expect(typeof h.ctaText).toBe('string');
    expect(typeof h.secondaryCtaText).toBe('string');
  });

  it('headline contains the highlighted word', () => {
    expect(heroContent.headline).toContain(heroContent.highlightedWord);
  });
});

/* ------------------------------------------------------------------ */
/*  navLinks                                                           */
/* ------------------------------------------------------------------ */

describe('navLinks', () => {
  it('exports an array of NavLink objects', () => {
    expect(Array.isArray(navLinks)).toBe(true);
    navLinks.forEach((link: NavLink) => {
      expect(typeof link.label).toBe('string');
      expect(typeof link.href).toBe('string');
    });
  });

  it('includes at least 4 navigation links', () => {
    expect(navLinks.length).toBeGreaterThanOrEqual(4);
  });
});

/* ------------------------------------------------------------------ */
/*  socialLinks                                                        */
/* ------------------------------------------------------------------ */

describe('socialLinks', () => {
  it('exports an array of SocialLink objects', () => {
    expect(Array.isArray(socialLinks)).toBe(true);
    socialLinks.forEach((link: SocialLink) => {
      expect(typeof link.platform).toBe('string');
      expect(typeof link.href).toBe('string');
      expect(typeof link.icon).toBe('string');
    });
  });
});

/* ------------------------------------------------------------------ */
/*  logoItems                                                          */
/* ------------------------------------------------------------------ */

describe('logoItems', () => {
  it('exports at least 5 logo items', () => {
    expect(logoItems.length).toBeGreaterThanOrEqual(5);
  });

  it('every logo has a name', () => {
    logoItems.forEach((logo: LogoItem) => {
      expect(typeof logo.name).toBe('string');
      expect(logo.name.length).toBeGreaterThan(0);
    });
  });
});

/* ------------------------------------------------------------------ */
/*  ctaBannerContent                                                   */
/* ------------------------------------------------------------------ */

describe('ctaBannerContent', () => {
  it('has all required fields', () => {
    const cta: CtaBannerContent = ctaBannerContent;
    expect(typeof cta.headline).toBe('string');
    expect(typeof cta.subheadline).toBe('string');
    expect(typeof cta.primaryCta).toBe('string');
    expect(typeof cta.secondaryCta).toBe('string');
  });
});

/* ------------------------------------------------------------------ */
/*  siteConfig                                                         */
/* ------------------------------------------------------------------ */

describe('siteConfig', () => {
  it('has all required fields', () => {
    const cfg: SiteConfig = siteConfig;
    expect(typeof cfg.name).toBe('string');
    expect(typeof cfg.tagline).toBe('string');
    expect(typeof cfg.description).toBe('string');
    expect(typeof cfg.url).toBe('string');
    expect(typeof cfg.repoUrl).toBe('string');
    expect(Array.isArray(cfg.socialLinks)).toBe(true);
  });
});

/* ------------------------------------------------------------------ */
/*  footerContent                                                      */
/* ------------------------------------------------------------------ */

describe('footerContent', () => {
  it('has tagline and copyright', () => {
    expect(typeof footerContent.tagline).toBe('string');
    expect(typeof footerContent.copyright).toBe('string');
    expect(footerContent.copyright).toContain('Phalanx');
  });
});
