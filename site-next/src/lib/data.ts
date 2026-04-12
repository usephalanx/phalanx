// ---------------------------------------------------------------------------
// Thin re-export layer — all data lives in @/data/content.
// This module provides UPPERCASE aliases for backward compatibility.
// New code should import directly from @/data/content.
// ---------------------------------------------------------------------------

export {
  features as FEATURES,
  pricingTiers as PRICING_TIERS,
  faqItems as FAQ_ITEMS,
  howItWorksSteps as HOW_IT_WORKS_STEPS,
  heroContent as HERO_CONTENT,
  navLinks as NAV_LINKS,
  socialLinks as SOCIAL_LINKS,
  siteConfig as SITE_CONFIG,
  logoItems as LOGO_ITEMS,
  footerContent as FOOTER_CONTENT,
  ctaBannerContent as CTA_BANNER_CONTENT,
} from '@/data/content';

export type {
  Feature,
  PricingTier,
  FaqItem,
  HowItWorksStep,
  HeroContent,
  NavLink,
  SocialLink,
  SiteConfig,
  LogoItem,
  CtaBannerContent,
  FooterLinkColumn,
} from '@/data/content';
