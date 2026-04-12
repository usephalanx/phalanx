/**
 * Tests for application-wide constants and re-exported content data.
 *
 * Constants live in @/lib/constants; content data lives in @/data/content
 * and is re-exported via @/lib/data with UPPERCASE aliases for compat.
 */

import { SECTION_IDS, GITHUB_URL, DOCS_URL } from '@/lib/constants';
import {
  navLinks as NAV_LINKS,
  siteConfig as SITE_CONFIG,
  features as FEATURES,
  pricingTiers as PRICING_TIERS,
  faqItems as FAQ_ITEMS,
  howItWorksSteps as HOW_IT_WORKS_STEPS,
  heroContent as HERO_CONTENT,
} from '@/data/content';

/* ------------------------------------------------------------------ */
/*  SECTION_IDS                                                        */
/* ------------------------------------------------------------------ */

describe('SECTION_IDS', () => {
  it('contains all expected section keys', () => {
    expect(SECTION_IDS).toHaveProperty('hero');
    expect(SECTION_IDS).toHaveProperty('howItWorks');
    expect(SECTION_IDS).toHaveProperty('features');
    expect(SECTION_IDS).toHaveProperty('pricing');
    expect(SECTION_IDS).toHaveProperty('faq');
  });

  it('maps to string IDs', () => {
    for (const id of Object.values(SECTION_IDS)) {
      expect(typeof id).toBe('string');
      expect(id.length).toBeGreaterThan(0);
    }
  });
});

/* ------------------------------------------------------------------ */
/*  External URLs                                                      */
/* ------------------------------------------------------------------ */

describe('GITHUB_URL', () => {
  it('is a valid GitHub URL', () => {
    expect(GITHUB_URL).toMatch(/^https:\/\/github\.com\//);
  });
});

describe('DOCS_URL', () => {
  it('is a non-empty string', () => {
    expect(typeof DOCS_URL).toBe('string');
    expect(DOCS_URL.length).toBeGreaterThan(0);
  });
});

/* ------------------------------------------------------------------ */
/*  NAV_LINKS                                                         */
/* ------------------------------------------------------------------ */

describe('NAV_LINKS', () => {
  it('is a non-empty array', () => {
    expect(Array.isArray(NAV_LINKS)).toBe(true);
    expect(NAV_LINKS.length).toBeGreaterThan(0);
  });

  it('each link has a label and href', () => {
    for (const link of NAV_LINKS) {
      expect(link.label).toBeTruthy();
      expect(link.href).toBeTruthy();
    }
  });

  it('every href is an anchor or absolute URL', () => {
    for (const link of NAV_LINKS) {
      expect(link.href).toMatch(/^(#|https?:\/\/)/);
    }
  });

  it('contains expected sections', () => {
    const labels = NAV_LINKS.map((l) => l.label);
    expect(labels).toContain('Features');
    expect(labels).toContain('Pricing');
    expect(labels).toContain('FAQ');
  });
});

/* ------------------------------------------------------------------ */
/*  SITE_CONFIG                                                       */
/* ------------------------------------------------------------------ */

describe('SITE_CONFIG', () => {
  it('has a brand name', () => {
    expect(SITE_CONFIG.name).toBe('Phalanx');
  });

  it('has a non-empty tagline', () => {
    expect(SITE_CONFIG.tagline).toBeTruthy();
  });

  it('has a valid canonical URL', () => {
    expect(SITE_CONFIG.url).toMatch(/^https:\/\//);
  });

  it('has a GitHub repo URL', () => {
    expect(SITE_CONFIG.repoUrl).toMatch(/^https:\/\/github\.com\//);
  });

  it('has at least one social link', () => {
    expect(SITE_CONFIG.socialLinks.length).toBeGreaterThan(0);
  });

  it('each social link has platform, href, and icon', () => {
    for (const link of SITE_CONFIG.socialLinks) {
      expect(link.platform).toBeTruthy();
      expect(link.href).toMatch(/^https?:\/\//);
      expect(link.icon).toBeTruthy();
    }
  });
});

/* ------------------------------------------------------------------ */
/*  Re-exported arrays — verify they match originals                  */
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

  it('includes expected feature titles', () => {
    const titles = FEATURES.map((f) => f.title);
    expect(titles).toContain('AI Planning');
    expect(titles).toContain('Security Scanning');
  });
});

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
});

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
