import {
  FEATURES,
  PRICING_TIERS,
  FAQ_ITEMS,
  HOW_IT_WORKS_STEPS,
  HERO_CONTENT,
} from '@/lib/data';
import type {
  Feature,
  PricingTier,
  FaqItem,
  HowItWorksStep,
  HeroContent,
  NavLink,
  SocialLink,
  SiteConfig,
} from '@/lib/data';

/* ------------------------------------------------------------------ */
/*  FEATURES                                                           */
/* ------------------------------------------------------------------ */

describe('FEATURES', () => {
  it('contains exactly 6 features', () => {
    expect(FEATURES).toHaveLength(6);
  });

  it('each feature has icon, title, and description', () => {
    for (const f of FEATURES) {
      expect(f.icon).toBeTruthy();
      expect(f.title).toBeTruthy();
      expect(f.description).toBeTruthy();
    }
  });

  it('includes AI Planning and Security Scanning', () => {
    const titles = FEATURES.map((f) => f.title);
    expect(titles).toContain('AI Planning');
    expect(titles).toContain('Security Scanning');
  });

  it('all icon names are non-empty strings', () => {
    for (const f of FEATURES) {
      expect(typeof f.icon).toBe('string');
      expect(f.icon.length).toBeGreaterThan(0);
    }
  });
});

/* ------------------------------------------------------------------ */
/*  PRICING_TIERS                                                      */
/* ------------------------------------------------------------------ */

describe('PRICING_TIERS', () => {
  it('contains exactly 3 tiers', () => {
    expect(PRICING_TIERS).toHaveLength(3);
  });

  it('tiers are Starter, Pro, Enterprise in order', () => {
    const names = PRICING_TIERS.map((t) => t.name);
    expect(names).toEqual(['Starter', 'Pro', 'Enterprise']);
  });

  it('Starter is $0', () => {
    const starter = PRICING_TIERS.find((t) => t.name === 'Starter');
    expect(starter?.price).toBe('$0');
  });

  it('Pro is $49', () => {
    const pro = PRICING_TIERS.find((t) => t.name === 'Pro');
    expect(pro?.price).toBe('$49');
  });

  it('Enterprise is Custom', () => {
    const ent = PRICING_TIERS.find((t) => t.name === 'Enterprise');
    expect(ent?.price).toBe('Custom');
  });

  it('has exactly one highlighted tier', () => {
    expect(PRICING_TIERS.filter((t) => t.highlighted)).toHaveLength(1);
  });

  it('each tier has a non-empty features list and cta', () => {
    for (const tier of PRICING_TIERS) {
      expect(tier.features.length).toBeGreaterThan(0);
      expect(tier.cta).toBeTruthy();
    }
  });
});

/* ------------------------------------------------------------------ */
/*  FAQ_ITEMS                                                          */
/* ------------------------------------------------------------------ */

describe('FAQ_ITEMS', () => {
  it('contains exactly 6 items', () => {
    expect(FAQ_ITEMS).toHaveLength(6);
  });

  it('each item has a question and answer', () => {
    for (const item of FAQ_ITEMS) {
      expect(item.question).toBeTruthy();
      expect(item.answer).toBeTruthy();
    }
  });
});

/* ------------------------------------------------------------------ */
/*  HOW_IT_WORKS_STEPS                                                 */
/* ------------------------------------------------------------------ */

describe('HOW_IT_WORKS_STEPS', () => {
  it('contains exactly 4 steps', () => {
    expect(HOW_IT_WORKS_STEPS).toHaveLength(4);
  });

  it('steps are numbered 1 through 4', () => {
    expect(HOW_IT_WORKS_STEPS.map((s) => s.step)).toEqual([1, 2, 3, 4]);
  });

  it('each step has title, description, and icon', () => {
    for (const s of HOW_IT_WORKS_STEPS) {
      expect(s.title).toBeTruthy();
      expect(s.description).toBeTruthy();
      expect(s.icon).toBeTruthy();
    }
  });
});

/* ------------------------------------------------------------------ */
/*  HERO_CONTENT                                                       */
/* ------------------------------------------------------------------ */

describe('HERO_CONTENT', () => {
  it('has all required fields', () => {
    expect(HERO_CONTENT.headline).toBeTruthy();
    expect(HERO_CONTENT.highlightedWord).toBeTruthy();
    expect(HERO_CONTENT.subheadline).toBeTruthy();
    expect(HERO_CONTENT.ctaText).toBeTruthy();
    expect(HERO_CONTENT.secondaryCtaText).toBeTruthy();
  });

  it('headline contains the highlighted word', () => {
    expect(HERO_CONTENT.headline).toContain(HERO_CONTENT.highlightedWord);
  });
});

/* ------------------------------------------------------------------ */
/*  Type exports compile check                                         */
/* ------------------------------------------------------------------ */

describe('type exports', () => {
  it('exported types are usable', () => {
    // These assignments verify the types compile correctly.
    const _feature: Feature = FEATURES[0];
    const _tier: PricingTier = PRICING_TIERS[0];
    const _faq: FaqItem = FAQ_ITEMS[0];
    const _step: HowItWorksStep = HOW_IT_WORKS_STEPS[0];
    const _hero: HeroContent = HERO_CONTENT;
    const _nav: NavLink = { label: 'Test', href: '#test' };
    const _social: SocialLink = { platform: 'X', href: 'https://x.com', icon: 'Twitter' };
    const _config: SiteConfig = {
      name: 'Test',
      tagline: 'Test',
      description: 'Test',
      url: 'https://test.com',
      repoUrl: 'https://github.com/test',
      socialLinks: [],
    };

    expect(_feature).toBeDefined();
    expect(_tier).toBeDefined();
    expect(_faq).toBeDefined();
    expect(_step).toBeDefined();
    expect(_hero).toBeDefined();
    expect(_nav).toBeDefined();
    expect(_social).toBeDefined();
    expect(_config).toBeDefined();
  });
});
