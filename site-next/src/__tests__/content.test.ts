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
} from '@/data/content';

describe('content data', () => {
  describe('features', () => {
    it('contains exactly 6 features', () => {
      expect(features).toHaveLength(6);
    });

    it('each feature has required fields', () => {
      for (const feature of features) {
        expect(feature.icon).toBeTruthy();
        expect(feature.title).toBeTruthy();
        expect(feature.description).toBeTruthy();
      }
    });
  });

  describe('pricingTiers', () => {
    it('contains exactly 3 tiers', () => {
      expect(pricingTiers).toHaveLength(3);
    });

    it('has exactly one highlighted tier', () => {
      const highlighted = pricingTiers.filter((t) => t.highlighted);
      expect(highlighted).toHaveLength(1);
    });

    it('each tier has a name, price, features, and cta', () => {
      for (const tier of pricingTiers) {
        expect(tier.name).toBeTruthy();
        expect(tier.price).toBeTruthy();
        expect(tier.features.length).toBeGreaterThan(0);
        expect(tier.cta).toBeTruthy();
      }
    });
  });

  describe('faqItems', () => {
    it('contains at least 5 items', () => {
      expect(faqItems.length).toBeGreaterThanOrEqual(5);
    });

    it('each item has a question and answer', () => {
      for (const item of faqItems) {
        expect(item.question).toBeTruthy();
        expect(item.answer).toBeTruthy();
      }
    });
  });

  describe('howItWorksSteps', () => {
    it('contains exactly 4 steps', () => {
      expect(howItWorksSteps).toHaveLength(4);
    });

    it('steps are numbered 1 through 4', () => {
      const stepNumbers = howItWorksSteps.map((s) => s.step);
      expect(stepNumbers).toEqual([1, 2, 3, 4]);
    });
  });

  describe('heroContent', () => {
    it('has all required fields', () => {
      expect(heroContent.headline).toBeTruthy();
      expect(heroContent.highlightedWord).toBeTruthy();
      expect(heroContent.subheadline).toBeTruthy();
      expect(heroContent.ctaText).toBeTruthy();
      expect(heroContent.secondaryCtaText).toBeTruthy();
    });

    it('headline contains the highlighted word', () => {
      expect(heroContent.headline).toContain(heroContent.highlightedWord);
    });
  });

  describe('navLinks', () => {
    it('contains exactly 4 links', () => {
      expect(navLinks).toHaveLength(4);
    });

    it('each link has label and href', () => {
      for (const link of navLinks) {
        expect(link.label).toBeTruthy();
        expect(link.href).toMatch(/^#/);
      }
    });
  });

  describe('socialLinks', () => {
    it('contains at least 2 links', () => {
      expect(socialLinks.length).toBeGreaterThanOrEqual(2);
    });

    it('each link has platform, href, and icon', () => {
      for (const link of socialLinks) {
        expect(link.platform).toBeTruthy();
        expect(link.href).toMatch(/^https?:\/\//);
        expect(link.icon).toBeTruthy();
      }
    });
  });

  describe('footerContent', () => {
    it('has a tagline', () => {
      expect(footerContent.tagline).toBeTruthy();
    });

    it('has a copyright notice', () => {
      expect(footerContent.copyright).toContain('Phalanx');
    });
  });

  describe('ctaBannerContent', () => {
    it('has all required fields', () => {
      expect(ctaBannerContent.headline).toBeTruthy();
      expect(ctaBannerContent.subheadline).toBeTruthy();
      expect(ctaBannerContent.primaryCta).toBeTruthy();
      expect(ctaBannerContent.secondaryCta).toBeTruthy();
    });
  });
});
