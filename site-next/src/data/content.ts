// ---------------------------------------------------------------------------
// Single source of truth for all Phalanx marketing site content.
// All section content lives here so pages remain purely compositional.
// ---------------------------------------------------------------------------

/* ------------------------------------------------------------------ */
/*  Shared interfaces                                                  */
/* ------------------------------------------------------------------ */

/** A link rendered in the navbar / mobile nav. */
export interface NavLink {
  /** Visible label. */
  label: string;
  /** In-page anchor (e.g. "#features") or external URL. */
  href: string;
}

/** A social-media / external link displayed in the footer. */
export interface SocialLink {
  /** Platform name (used for aria-label). */
  platform: string;
  /** Full URL to the profile or page. */
  href: string;
  /** Name of the Lucide icon (or equivalent) to render. */
  icon: string;
}

/** A partner / tech logo displayed in the logo bar. */
export interface LogoItem {
  /** Company or technology name. */
  name: string;
  /** Optional path to an SVG logo asset. */
  svgPath?: string;
}

/** A column of links rendered in the site footer. */
export interface FooterLinkColumn {
  /** Column heading. */
  title: string;
  /** Links within the column. */
  links: { label: string; href: string }[];
}

/* ------------------------------------------------------------------ */
/*  Section interfaces                                                 */
/* ------------------------------------------------------------------ */

/** A single feature card displayed in the Features grid. */
export interface Feature {
  /** Name of the Lucide icon (or equivalent) to render. */
  icon: string;
  /** Short feature title. */
  title: string;
  /** One-to-two sentence description. */
  description: string;
}

/** A pricing tier shown on the Pricing section. */
export interface PricingTier {
  /** Unique tier identifier. */
  id: string;
  /** Tier display name. */
  name: string;
  /** Monthly price string — "$0", "$49", or "Custom". */
  price: string;
  /** Billing cadence label (e.g. "per seat / month", "free forever"). */
  billingPeriod: string;
  /** Optional small print below the price (alias for billingPeriod). */
  priceSuffix: string;
  /** Feature bullets included in this tier. */
  features: string[];
  /** CTA button label. */
  cta: string;
  /** CTA button label (canonical). */
  ctaLabel: string;
  /** CTA button destination href. */
  ctaHref: string;
  /** Whether this tier should be visually highlighted as recommended. */
  highlighted: boolean;
}

/** A single FAQ accordion item. */
export interface FaqItem {
  /** Unique FAQ identifier. */
  id: string;
  /** The question. */
  question: string;
  /** The answer (plain text or light markdown). */
  answer: string;
}

/** A step in the "How It Works" flow. */
export interface HowItWorksStep {
  /** 1-based step number. */
  step: number;
  /** Short step title. */
  title: string;
  /** Longer explanation. */
  description: string;
  /** Name of the Lucide icon (or equivalent) to render. */
  icon: string;
  /** Tailwind agent-* colour token for the step accent. */
  agentColor: string;
}

/** Hero section content. */
export interface HeroContent {
  /** Primary headline — may include a highlighted word/span. */
  headline: string;
  /** The word or phrase in the headline to accent. */
  highlightedWord: string;
  /** Secondary copy below the headline. */
  subheadline: string;
  /** Primary CTA button text. */
  ctaText: string;
  /** Secondary / ghost CTA text. */
  secondaryCtaText: string;
}

/** CTA banner section content. */
export interface CtaBannerContent {
  /** Primary banner headline. */
  headline: string;
  /** Supporting copy below the headline. */
  subheadline: string;
  /** Primary call-to-action button label. */
  primaryCta: string;
  /** Secondary / ghost call-to-action label. */
  secondaryCta: string;
}

/** Top-level site configuration object. */
export interface SiteConfig {
  /** Brand / product name. */
  name: string;
  /** Short tagline used in hero, meta, and footer. */
  tagline: string;
  /** SEO meta description. */
  description: string;
  /** Canonical site URL. */
  url: string;
  /** GitHub repository URL. */
  repoUrl: string;
  /** Social / external links. */
  socialLinks: SocialLink[];
}

/* ------------------------------------------------------------------ */
/*  Data                                                              */
/* ------------------------------------------------------------------ */

/** Six core features of Phalanx. */
export const features: Feature[] = [
  {
    icon: 'BrainCircuit',
    title: 'AI Planning',
    description:
      'An LLM-powered planner breaks your request into scoped tasks with dependencies, file targets, and acceptance criteria — before a single line is written.',
  },
  {
    icon: 'GitPullRequestArrow',
    title: 'Auto Code Review',
    description:
      'A dedicated reviewer agent audits every changeset for correctness, style, and security — then posts inline comments just like a senior engineer.',
  },
  {
    icon: 'ShieldCheck',
    title: 'Security Scanning',
    description:
      'The security agent runs static analysis, dependency audits, and secret detection on every build so vulnerabilities never reach production.',
  },
  {
    icon: 'FlaskConical',
    title: 'Automated QA',
    description:
      'Tests are generated and executed automatically. Coverage gates ensure nothing ships until your quality bar is met.',
  },
  {
    icon: 'Workflow',
    title: 'Config-Driven Pipelines',
    description:
      'Define your workflow in a simple YAML config. Add agents, reorder stages, or insert approval gates — no code changes required.',
  },
  {
    icon: 'Users',
    title: 'Human-in-the-Loop',
    description:
      'Approval gates in Slack let your team review plans, diffs, and test results before anything merges. You stay in command at every step.',
  },
];

/** Three pricing tiers: Starter, Pro, Enterprise. */
export const pricingTiers: PricingTier[] = [
  {
    id: 'starter',
    name: 'Starter',
    price: '$0',
    billingPeriod: 'free forever',
    priceSuffix: 'free forever',
    features: [
      'Up to 3 team members',
      'Community support',
      'All core agents included',
      'Self-hosted deployment',
      '100 runs / month',
    ],
    cta: 'Get Started',
    ctaLabel: 'Get Started',
    ctaHref: '/signup',
    highlighted: false,
  },
  {
    id: 'pro',
    name: 'Pro',
    price: '$49',
    billingPeriod: 'per seat / month',
    priceSuffix: 'per seat / month',
    features: [
      'Unlimited team members',
      'Priority support & SLA',
      'All core agents included',
      'Advanced analytics dashboard',
      'Unlimited runs',
      'Custom agent plugins',
    ],
    cta: 'Start Free Trial',
    ctaLabel: 'Start Free Trial',
    ctaHref: '/signup?plan=pro',
    highlighted: true,
  },
  {
    id: 'enterprise',
    name: 'Enterprise',
    price: 'Custom',
    billingPeriod: '',
    priceSuffix: '',
    features: [
      'Everything in Pro',
      'Dedicated success engineer',
      'SSO / SAML integration',
      'On-prem or private cloud',
      'Custom SLA & compliance',
      'Volume discounts',
    ],
    cta: 'Contact Sales',
    ctaLabel: 'Contact Sales',
    ctaHref: '/contact',
    highlighted: false,
  },
];

/** Six FAQ items about Phalanx. */
export const faqItems: FaqItem[] = [
  {
    id: 'what-is-phalanx',
    question: 'What exactly is Phalanx?',
    answer:
      'Phalanx is an open-source AI team operating system. You issue a Slack command, and a formation of specialised agents — planner, builder, reviewer, QA, security, and release — coordinate to ship your request from idea to production.',
  },
  {
    id: 'human-approval',
    question: 'Do agents push code without human approval?',
    answer:
      'Never. Every critical stage has an approval gate that posts to Slack. Your team reviews the plan, the diff, and the test results before anything is merged or deployed.',
  },
  {
    id: 'self-host',
    question: 'Can I self-host Phalanx?',
    answer:
      'Yes. Phalanx is fully self-hostable. The stack is Docker Compose with PostgreSQL, Redis, and a Celery worker fleet. Deploy on your own infrastructure or a single cloud VM — no vendor lock-in.',
  },
  {
    id: 'llm-providers',
    question: 'Which LLM providers are supported?',
    answer:
      'Phalanx uses the Anthropic Claude API by default but is designed to be model-agnostic. You can swap in any OpenAI-compatible provider by changing your environment config.',
  },
  {
    id: 'secrets-handling',
    question: 'How does Phalanx handle secrets and credentials?',
    answer:
      'Secrets are passed via environment variables and never stored in the database or logs. The security agent actively scans every changeset for leaked credentials before code can be merged.',
  },
  {
    id: 'repo-limits',
    question: 'Is there a limit on repository size or language support?',
    answer:
      'No hard limits. Phalanx works with any language or framework that can be built in a containerised environment. Repository size is bounded only by your infrastructure resources.',
  },
];

/** Four-step "How It Works" flow. */
export const howItWorksSteps: HowItWorksStep[] = [
  {
    step: 1,
    title: 'Slack Command',
    description:
      'Type /phalanx build "add dark-mode toggle" in any Slack channel. The gateway creates a work order and kicks off the pipeline.',
    icon: 'Terminal',
    agentColor: 'agent-cmd',
  },
  {
    step: 2,
    title: 'AI Plans',
    description:
      'The planner agent analyses your codebase, breaks the request into tasks, and posts a structured plan to Slack for your approval.',
    icon: 'ListChecks',
    agentColor: 'agent-plan',
  },
  {
    step: 3,
    title: 'Builds & Reviews',
    description:
      'Builder agents write the code in parallel. Reviewer and security agents audit every change. QA agents generate and run tests.',
    icon: 'Hammer',
    agentColor: 'agent-build',
  },
  {
    step: 4,
    title: 'Ships',
    description:
      'After final approval the release agent opens a PR, merges, and deploys. You get a Slack summary with links, coverage, and timing.',
    icon: 'Rocket',
    agentColor: 'agent-rel',
  },
];

/** Hero section copy. */
export const heroContent: HeroContent = {
  headline: 'From Slack Command to Shipped Software',
  highlightedWord: 'Shipped',
  subheadline:
    'Open-source AI agents coordinate from planning to production — with human approval at every gate. Config-driven. Self-hostable.',
  ctaText: 'Start Building — Free',
  secondaryCtaText: 'See How It Works',
};

/* ------------------------------------------------------------------ */
/*  Navigation & footer data                                           */
/* ------------------------------------------------------------------ */

/** Primary nav links rendered in the site header. */
export const navLinks: NavLink[] = [
  { label: 'Features', href: '#features' },
  { label: 'How It Works', href: '#how-it-works' },
  { label: 'Pricing', href: '#pricing' },
  { label: 'FAQ', href: '#faq' },
];

/** Social / external links displayed in the footer and navbar. */
export const socialLinks: SocialLink[] = [
  {
    platform: 'GitHub',
    href: 'https://github.com/usephalanx/phalanx',
    icon: 'Github',
  },
  {
    platform: 'Twitter',
    href: 'https://twitter.com/usephalanx',
    icon: 'Twitter',
  },
  {
    platform: 'Discord',
    href: 'https://discord.gg/phalanx',
    icon: 'MessageCircle',
  },
];

/** Footer content strings. */
export const footerContent = {
  /** Brand tagline rendered in the footer. */
  tagline: 'AI Agents in Formation. From Slack to Shipped.',
  /** Copyright notice with year placeholder. */
  copyright: `© ${new Date().getFullYear()} Phalanx. All rights reserved.`,
} as const;

/** CTA banner section content. */
export const ctaBannerContent: CtaBannerContent = {
  headline: 'Ready to ship faster?',
  subheadline:
    'Deploy your AI-powered engineering team in minutes. Open source, self-hostable, and free to start.',
  primaryCta: 'Get Started — Free',
  secondaryCta: 'View Documentation',
};

/** Placeholder partner / technology logos for social proof. */
export const logoItems: LogoItem[] = [
  { name: 'Vercel' },
  { name: 'Supabase' },
  { name: 'Stripe' },
  { name: 'Linear' },
  { name: 'Figma' },
  { name: 'Notion' },
];

/** Global brand and meta configuration. */
export const siteConfig: SiteConfig = {
  name: 'Phalanx',
  tagline: 'From Slack Command to Shipped Software',
  description:
    'Open-source AI team operating system. Specialized agents coordinate from planning to production with human approval at every gate.',
  url: 'https://usephalanx.com',
  repoUrl: 'https://github.com/usephalanx/phalanx',
  socialLinks,
};
