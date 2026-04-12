# Phalanx Marketing Site — Architecture Plan

> Single-page Next.js 14 App Router site. Static export to `out/`.
> Dark theme, scroll-animated, fully accessible, mobile-first.

---

## 1. Folder Structure

```
site-next/
├── public/
│   ├── favicon.ico
│   ├── og-image.png              # 1200×630 OpenGraph image
│   └── demo.mp4                  # Optional hero background video
├── src/
│   ├── app/
│   │   ├── layout.tsx            # (EXISTS) Root layout — fonts, metadata, <body>
│   │   ├── page.tsx              # (EXISTS) Home page — composes all sections
│   │   └── globals.css           # (EXISTS) Tailwind directives, grid bg, animations
│   ├── components/
│   │   ├── sections/
│   │   │   ├── Navbar.tsx        # Client — sticky nav, mobile hamburger, scroll spy
│   │   │   ├── Hero.tsx          # Server — headline, CTAs, terminal mockup
│   │   │   ├── LogoBar.tsx       # Server — "Trusted by" logo ticker
│   │   │   ├── HowItWorks.tsx    # Client — 4-step flow with scroll animation
│   │   │   ├── Features.tsx      # Client — 6-card grid with staggered reveal
│   │   │   ├── Pricing.tsx       # Server — 3-tier card layout
│   │   │   ├── Faq.tsx           # Client — accordion with expand/collapse
│   │   │   ├── CtaBanner.tsx     # Server — full-width CTA strip
│   │   │   └── Footer.tsx        # Server — links, copyright, social icons
│   │   └── ui/
│   │       ├── Button.tsx        # Client — variant-driven button (primary, secondary, ghost)
│   │       ├── Badge.tsx         # Server — small label chip
│   │       ├── SectionHeading.tsx # Server — eyebrow + h2 + subtitle pattern
│   │       ├── Card.tsx          # Server — glass-morphism card wrapper
│   │       ├── Accordion.tsx     # Client — single FAQ item, animated open/close
│   │       ├── Container.tsx     # Server — max-w-content centered padding wrapper
│   │       ├── Icon.tsx          # Server — dynamic Lucide icon renderer by name string
│   │       ├── SkipNav.tsx       # Server — hidden skip-to-content link
│   │       └── AnimateIn.tsx     # Client — reusable scroll-triggered fade-up wrapper
│   ├── data/
│   │   └── content.ts            # (EXISTS) All mock data + TypeScript interfaces
│   └── lib/
│       ├── cn.ts                 # clsx + twMerge utility
│       ├── constants.ts          # NAV_LINKS, SOCIAL_LINKS, SECTION_IDS
│       └── hooks/
│           └── useActiveSection.ts # IntersectionObserver-based scroll spy hook
├── tailwind.config.ts            # (EXISTS) Theme tokens
├── next.config.mjs               # (EXISTS) Static export config
├── tsconfig.json                 # (EXISTS)
├── postcss.config.mjs            # (EXISTS)
└── package.json                  # (EXISTS)
```

---

## 2. Component Hierarchy

```
RootLayout (Server)                          ← app/layout.tsx
└── HomePage (Server)                        ← app/page.tsx
    ├── SkipNav (Server)                     ← ui/SkipNav.tsx
    ├── Navbar (Client)                      ← sections/Navbar.tsx
    ├── Hero (Server)                        ← sections/Hero.tsx
    │   ├── Container
    │   ├── Badge
    │   └── Button ×2
    ├── LogoBar (Server)                     ← sections/LogoBar.tsx
    │   └── Container
    ├── HowItWorks (Client)                  ← sections/HowItWorks.tsx
    │   ├── Container
    │   ├── SectionHeading
    │   ├── AnimateIn ×4
    │   │   └── Card ×4
    │   │       └── Icon ×4
    │   └── (connecting step line SVG)
    ├── Features (Client)                    ← sections/Features.tsx
    │   ├── Container
    │   ├── SectionHeading
    │   └── AnimateIn ×6
    │       └── Card ×6
    │           └── Icon ×6
    ├── Pricing (Server)                     ← sections/Pricing.tsx
    │   ├── Container
    │   ├── SectionHeading
    │   └── Card ×3
    │       └── Button ×3
    ├── Faq (Client)                         ← sections/Faq.tsx
    │   ├── Container
    │   ├── SectionHeading
    │   └── Accordion ×6
    ├── CtaBanner (Server)                   ← sections/CtaBanner.tsx
    │   ├── Container
    │   └── Button ×2
    └── Footer (Server)                      ← sections/Footer.tsx
        └── Container
```

---

## 3. Data Contracts

### 3.1 Existing Interfaces (src/data/content.ts — DO NOT MODIFY)

| Interface        | Fields                                                         | Used By            |
|------------------|----------------------------------------------------------------|---------------------|
| `Feature`        | `icon: string`, `title: string`, `description: string`         | Features section    |
| `PricingTier`    | `name`, `price`, `priceSuffix?`, `features: string[]`, `cta`, `highlighted` | Pricing section |
| `FaqItem`        | `question: string`, `answer: string`                           | Faq section         |
| `HowItWorksStep` | `step: number`, `title`, `description`, `icon: string`        | HowItWorks section  |
| `HeroContent`   | `headline`, `highlightedWord`, `subheadline`, `ctaText`, `secondaryCtaText` | Hero section |

### 3.2 Existing Data Exports (src/data/content.ts)

| Export             | Type               | Count |
|--------------------|--------------------|-------|
| `features`         | `Feature[]`        | 6     |
| `pricingTiers`     | `PricingTier[]`    | 3     |
| `faqItems`         | `FaqItem[]`        | 6     |
| `howItWorksSteps`  | `HowItWorksStep[]` | 4     |
| `heroContent`      | `HeroContent`      | 1     |

### 3.3 New Interfaces to Add to content.ts

```typescript
/** A navigation link in the Navbar. */
export interface NavLink {
  label: string;       // Display text, e.g. "Features"
  href: string;        // Anchor href, e.g. "#features"
}

/** A social media link for the Footer. */
export interface SocialLink {
  platform: string;    // "github" | "twitter" | "discord"
  href: string;        // Full URL
  icon: string;        // Lucide icon name
}
```

### 3.4 New Data Exports to Add to content.ts

```typescript
export const navLinks: NavLink[] = [
  { label: 'How It Works', href: '#how-it-works' },
  { label: 'Features', href: '#features' },
  { label: 'Pricing', href: '#pricing' },
  { label: 'FAQ', href: '#faq' },
];

export const socialLinks: SocialLink[] = [
  { platform: 'github', href: 'https://github.com/usephalanx/phalanx', icon: 'Github' },
  { platform: 'twitter', href: 'https://twitter.com/usephalanx', icon: 'Twitter' },
];

export const footerContent = {
  tagline: 'AI Agents in Formation',
  copyright: `© ${new Date().getFullYear()} Phalanx. Open source under MIT.`,
};

export const ctaBannerContent = {
  headline: 'Ready to ship faster?',
  subheadline: 'Deploy Phalanx in minutes. Open source, self-hostable, free forever for small teams.',
  primaryCta: 'Get Started Free',
  secondaryCta: 'Read the Docs',
};
```

---

## 4. Tailwind Theme Tokens

### 4.1 Existing Tokens (tailwind.config.ts — already defined)

#### Colors

| Token               | Hex                         | Usage                        |
|----------------------|-----------------------------|-------------------------------|
| `bg.DEFAULT`         | `#050608`                   | Page background               |
| `bg.elevated`        | `#0B0D14`                   | Navbar, elevated surfaces     |
| `bg.card`            | `#0F111A`                   | Card backgrounds              |
| `bg.card-hover`      | `#141725`                   | Card hover state              |
| `border.DEFAULT`     | `#1A1D2E`                   | Default borders               |
| `border.hover`       | `#272B45`                   | Hover/focus borders           |
| `text.DEFAULT`       | `#E4E8F1`                   | Primary body text             |
| `text.secondary`     | `#8891AB`                   | Secondary/muted text          |
| `text.muted`         | `#555E78`                   | Tertiary/disabled text        |
| `brand.blue`         | `#7AA2F7`                   | Primary accent, links, CTAs   |
| `brand.blue-dim`     | `rgba(122,162,247,0.12)`    | Blue glow/tint backgrounds    |
| `brand.amber`        | `#D4A853`                   | Secondary accent, highlights  |
| `brand.amber-dim`    | `rgba(212,168,83,0.10)`     | Amber glow/tint backgrounds   |
| `agent.cmd`          | `#7AA2F7`                   | Commander agent color         |
| `agent.plan`         | `#BB9AF7`                   | Planner agent color           |
| `agent.build`        | `#9ECE6A`                   | Builder agent color           |
| `agent.review`       | `#E0AF68`                   | Reviewer agent color          |
| `agent.qa`           | `#73DACA`                   | QA agent color                |
| `agent.sec`          | `#F7768E`                   | Security agent color          |
| `agent.rel`          | `#FF9E64`                   | Release agent color           |

#### Typography

| Token        | Value                                          |
|--------------|------------------------------------------------|
| `font-sans`  | `var(--font-inter)`, system-ui fallbacks       |
| `font-mono`  | `var(--font-jetbrains)`, Menlo, monospace      |

Font weights loaded: 400, 500, 600, 700, 800, 900 (Inter); 400, 500, 600 (JetBrains Mono).

#### Spacing & Layout

| Token            | Value      | Usage                           |
|------------------|------------|---------------------------------|
| `max-w-content`  | `1100px`   | Maximum content width           |
| `spacing.section`| `120px`    | Vertical padding between sections |

#### Breakpoints

| Name | Width    | Target                          |
|------|----------|---------------------------------|
| `sm` | `600px`  | Large phones, small tablets     |
| `md` | `900px`  | Tablets, small laptops          |
| `lg` | `1100px` | Desktops (matches max-w-content)|

### 4.2 Tokens to ADD to tailwind.config.ts

```typescript
// Inside theme.extend:
animation: {
  'fade-up': 'fadeUp 0.5s ease-out forwards',
  'slide-in-left': 'slideInLeft 0.6s ease-out forwards',
  'ticker': 'ticker 30s linear infinite',
},
keyframes: {
  fadeUp: {
    '0%': { opacity: '0', transform: 'translateY(24px)' },
    '100%': { opacity: '1', transform: 'translateY(0)' },
  },
  slideInLeft: {
    '0%': { opacity: '0', transform: 'translateX(-24px)' },
    '100%': { opacity: '1', transform: 'translateX(0)' },
  },
  ticker: {
    '0%': { transform: 'translateX(0)' },
    '100%': { transform: 'translateX(-50%)' },
  },
},
backdropBlur: {
  nav: '16px',
},
```

---

## 5. Section Specifications

### 5.1 Navbar (`sections/Navbar.tsx`) — Client Component

**Purpose**: Sticky top navigation with scroll-responsive background, nav links with scroll spy, and mobile hamburger menu.

**Props**: None (reads from `navLinks` import).

**Layout**:
- Fixed `top-0 left-0 right-0 z-50`
- Background: transparent when at top, `bg-bg-elevated/80 backdrop-blur-nav border-b border-border` after 50px scroll
- Inner: `Container` → flex row, justify-between, items-center, h-16
- Left: Phalanx wordmark (`font-mono font-semibold text-lg text-brand-blue`)
- Center (md+): horizontal nav links, `text-sm font-medium text-text-secondary hover:text-text transition`
- Active link: `text-brand-blue` (determined by `useActiveSection` hook)
- Right: `Button variant="primary" size="sm"` → "Get Started"
- Mobile (<md): hamburger icon button → slides open a full-screen overlay with stacked nav links

**Scroll behavior**: `useActiveSection` hook observes each section's `id` via IntersectionObserver (threshold: 0.3) and returns the currently visible section id.

**Accessibility**:
- `<nav aria-label="Main navigation">`
- Mobile menu: `<dialog>` or `role="dialog" aria-modal="true"`, focus trapped while open
- Hamburger button: `aria-expanded`, `aria-controls="mobile-menu"`, `aria-label="Open menu"` / `"Close menu"`
- All nav links are `<a>` elements with visible focus rings

---

### 5.2 Hero (`sections/Hero.tsx`) — Server Component

**Purpose**: Above-the-fold headline, subheadline, two CTAs, and a decorative terminal mockup.

**Props**: None (reads from `heroContent` import).

**Layout**:
- `<section id="hero">` with `py-section` (120px top/bottom)
- `Container` → centered text, `max-w-3xl mx-auto text-center`
- Badge: `<Badge>Open Source</Badge>` above headline
- Headline: `<h1 className="text-4xl sm:text-5xl md:text-6xl font-extrabold tracking-tight leading-[1.1]">`
  - `highlightedWord` wrapped in `<span className="text-brand-blue">`
- Subheadline: `<p className="text-text-secondary text-lg md:text-xl max-w-2xl mx-auto mt-6">`
- CTAs: flex row (stacks on mobile), gap-4, mt-10
  - Primary: `<Button variant="primary" size="lg">{heroContent.ctaText}</Button>`
  - Secondary: `<Button variant="ghost" size="lg">{heroContent.secondaryCtaText}</Button>`
- Terminal mockup (below CTAs, mt-16):
  - `bg-bg-card border border-border rounded-xl overflow-hidden`
  - Title bar: 3 dots (red/yellow/green circles), centered "Terminal" text in `font-mono text-xs text-text-muted`
  - Body: `font-mono text-sm text-text-secondary p-6` showing a simulated `/phalanx build` command with colorized agent steps using `agent.*` colors

---

### 5.3 LogoBar (`sections/LogoBar.tsx`) — Server Component

**Purpose**: Social proof strip showing "Built for teams using" + horizontally scrolling logo placeholders.

**Layout**:
- `<section>` with `py-12 border-y border-border`
- Eyebrow: `text-text-muted text-xs uppercase tracking-widest text-center mb-8`
- Logo row: CSS `overflow-hidden` container with duplicated children for seamless `animate-ticker` loop
- Logos: 6 placeholder grey rectangles (`bg-border rounded h-8 w-24`) — to be replaced with real SVGs later

---

### 5.4 HowItWorks (`sections/HowItWorks.tsx`) — Client Component

**Purpose**: 4-step numbered flow showing the Slack → Plan → Build → Ship pipeline.

**Props**: None (reads from `howItWorksSteps`).

**Layout**:
- `<section id="how-it-works">` with `py-section`
- `SectionHeading eyebrow="How It Works" title="Four Steps to Shipped Code" subtitle="From Slack command to production in minutes."`
- Steps: responsive grid
  - Mobile (<md): single column, vertical connector line between steps
  - Desktop (md+): 4-column grid, horizontal connector line
- Each step card:
  - `AnimateIn` wrapper with staggered delay (index × 100ms)
  - `Card` with `p-6 text-center`
  - Step number: `w-10 h-10 rounded-full bg-brand-blue-dim text-brand-blue font-bold text-sm flex items-center justify-center mx-auto`
  - Icon: `<Icon name={step.icon} className="w-8 h-8 text-brand-blue mx-auto mt-4" />`
  - Title: `text-lg font-semibold mt-3`
  - Description: `text-text-secondary text-sm mt-2`

---

### 5.5 Features (`sections/Features.tsx`) — Client Component

**Purpose**: 6-card grid showcasing core platform features.

**Props**: None (reads from `features`).

**Layout**:
- `<section id="features">` with `py-section`
- `SectionHeading eyebrow="Features" title="Everything You Need to Ship" subtitle="Specialized agents handle every stage of your development pipeline."`
- Grid: `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6 mt-12`
- Each card:
  - `AnimateIn` wrapper with staggered delay (index × 80ms)
  - `Card` with `p-6 hover:border-border-hover transition-colors`
  - Icon: `<Icon name={feature.icon} className="w-10 h-10 text-brand-blue" />`
  - Title: `<h3 className="text-lg font-semibold mt-4">{feature.title}</h3>`
  - Description: `<p className="text-text-secondary text-sm mt-2 leading-relaxed">{feature.description}</p>`

---

### 5.6 Pricing (`sections/Pricing.tsx`) — Server Component

**Purpose**: 3-tier pricing comparison.

**Props**: None (reads from `pricingTiers`).

**Layout**:
- `<section id="pricing">` with `py-section`
- `SectionHeading eyebrow="Pricing" title="Simple, Transparent Pricing" subtitle="Start free. Scale when you're ready."`
- Grid: `grid grid-cols-1 md:grid-cols-3 gap-6 mt-12 items-start`
- Each tier card:
  - `Card` with `p-8`
  - Highlighted tier: `border-brand-blue ring-1 ring-brand-blue/20 relative`
    - "Most Popular" badge: `<Badge className="absolute -top-3 left-1/2 -translate-x-1/2">Most Popular</Badge>`
  - Tier name: `text-lg font-semibold`
  - Price: `text-4xl font-extrabold mt-2` + `priceSuffix` in `text-text-muted text-sm ml-2`
  - Feature list: `ul` with `li` items, each with a `Check` icon in `text-brand-blue`
  - CTA: `Button variant={highlighted ? 'primary' : 'secondary'} className="w-full mt-8"`

**Accessibility**: Highlighted tier has `aria-label="Recommended plan"` on its card.

---

### 5.7 FAQ (`sections/Faq.tsx`) — Client Component

**Purpose**: Expandable accordion of common questions.

**Props**: None (reads from `faqItems`).

**Layout**:
- `<section id="faq">` with `py-section`
- `SectionHeading eyebrow="FAQ" title="Frequently Asked Questions"`
- Accordion list: `max-w-2xl mx-auto mt-12 space-y-3`
- Each `Accordion` item: see UI component spec below

---

### 5.8 CtaBanner (`sections/CtaBanner.tsx`) — Server Component

**Purpose**: Full-width call-to-action before the footer.

**Layout**:
- `<section>` with `py-section`
- `Container` → centered text
- Inner wrapper: `bg-bg-card border border-border rounded-2xl p-12 md:p-16 text-center relative overflow-hidden`
- Decorative glow: `absolute` radial gradient blob (`bg-brand-blue/5 blur-3xl`) behind text
- Headline: `text-3xl md:text-4xl font-bold`
- Subheadline: `text-text-secondary text-lg mt-4 max-w-xl mx-auto`
- CTAs: flex row centered, gap-4, mt-8

---

### 5.9 Footer (`sections/Footer.tsx`) — Server Component

**Purpose**: Site footer with links, social icons, and copyright.

**Layout**:
- `<footer>` with `py-12 border-t border-border`
- `Container` → flex column md:flex-row justify-between items-center gap-6
- Left: Phalanx wordmark + tagline
- Center: nav links (same as Navbar)
- Right: social icon links (`<a>` with `<Icon>`)
- Bottom: copyright text in `text-text-muted text-xs`

---

## 6. UI Component Specifications

### 6.1 Button (`ui/Button.tsx`) — Client Component

```typescript
interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant: 'primary' | 'secondary' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  href?: string;           // If provided, renders as <a>
  children: React.ReactNode;
}
```

| Variant     | Base Classes                                                                   |
|-------------|--------------------------------------------------------------------------------|
| `primary`   | `bg-brand-blue text-bg font-semibold hover:bg-brand-blue/90 shadow-lg shadow-brand-blue/20` |
| `secondary` | `bg-bg-card border border-border text-text hover:border-border-hover`          |
| `ghost`     | `text-text-secondary hover:text-text underline-offset-4 hover:underline`       |

| Size | Padding             | Text Size    |
|------|---------------------|--------------|
| `sm` | `px-4 py-2`         | `text-sm`    |
| `md` | `px-6 py-2.5`       | `text-sm`    |
| `lg` | `px-8 py-3`         | `text-base`  |

All buttons: `rounded-lg transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-blue focus-visible:ring-offset-2 focus-visible:ring-offset-bg inline-flex items-center justify-center`

---

### 6.2 Badge (`ui/Badge.tsx`) — Server Component

```typescript
interface BadgeProps {
  children: React.ReactNode;
  className?: string;
}
```

Base: `inline-flex items-center px-3 py-1 text-xs font-medium rounded-full bg-brand-blue-dim text-brand-blue border border-brand-blue/20`

---

### 6.3 SectionHeading (`ui/SectionHeading.tsx`) — Server Component

```typescript
interface SectionHeadingProps {
  eyebrow: string;       // Uppercase small label
  title: string;         // Section h2
  subtitle?: string;     // Optional paragraph below title
}
```

Layout:
- `text-center max-w-2xl mx-auto`
- Eyebrow: `text-brand-blue text-sm font-semibold uppercase tracking-wider`
- Title: `<h2 className="text-3xl md:text-4xl font-bold mt-3">`
- Subtitle: `<p className="text-text-secondary text-lg mt-4">`

---

### 6.4 Card (`ui/Card.tsx`) — Server Component

```typescript
interface CardProps {
  children: React.ReactNode;
  className?: string;
}
```

Base: `bg-bg-card border border-border rounded-xl transition-colors duration-200`

---

### 6.5 Accordion (`ui/Accordion.tsx`) — Client Component

```typescript
interface AccordionProps {
  question: string;
  answer: string;
  defaultOpen?: boolean;
}
```

**Behavior**:
- Click header to toggle open/close
- `framer-motion` `AnimatePresence` for smooth height animation
- Chevron icon rotates 180° on open

**Layout**:
- Outer: `border border-border rounded-lg overflow-hidden`
- Header button: `w-full flex items-center justify-between p-5 text-left font-medium hover:bg-bg-card-hover transition-colors`
- Body: `px-5 pb-5 text-text-secondary text-sm leading-relaxed`

**Accessibility**:
- Header: `<button aria-expanded="{isOpen}" aria-controls="accordion-{index}-body">`
- Body: `<div id="accordion-{index}-body" role="region" aria-labelledby="accordion-{index}-header">`

---

### 6.6 Container (`ui/Container.tsx`) — Server Component

```typescript
interface ContainerProps {
  children: React.ReactNode;
  className?: string;
}
```

Base: `max-w-content mx-auto px-6`

---

### 6.7 Icon (`ui/Icon.tsx`) — Server Component

```typescript
interface IconProps {
  name: string;          // Lucide icon name, e.g. "BrainCircuit"
  className?: string;
}
```

Implementation: imports all needed icons from `lucide-react` in a lookup map. Renders the matching component or returns `null`.

```typescript
import { BrainCircuit, GitPullRequestArrow, ShieldCheck, FlaskConical, Workflow, Users, Terminal, ListChecks, Hammer, Rocket, ChevronDown, Check, Github, Twitter, Menu, X } from 'lucide-react';

const iconMap: Record<string, React.ComponentType<any>> = {
  BrainCircuit, GitPullRequestArrow, ShieldCheck, FlaskConical, Workflow, Users,
  Terminal, ListChecks, Hammer, Rocket, ChevronDown, Check, Github, Twitter, Menu, X,
};

export function Icon({ name, className }: IconProps) {
  const Comp = iconMap[name];
  if (!Comp) return null;
  return <Comp className={className} />;
}
```

---

### 6.8 SkipNav (`ui/SkipNav.tsx`) — Server Component

```typescript
// No props
```

Renders: `<a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-[100] focus:px-4 focus:py-2 focus:bg-brand-blue focus:text-bg focus:rounded-lg focus:font-semibold">Skip to content</a>`

The `<main>` in `page.tsx` must have `id="main-content"`.

---

### 6.9 AnimateIn (`ui/AnimateIn.tsx`) — Client Component

```typescript
interface AnimateInProps {
  children: React.ReactNode;
  delay?: number;          // seconds, default 0
  className?: string;
}
```

Implementation:
```typescript
'use client';
import { motion } from 'framer-motion';

export function AnimateIn({ children, delay = 0, className }: AnimateInProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-80px' }}
      transition={{ duration: 0.5, delay, ease: [0.25, 0.1, 0.25, 1] }}
      className={className}
    >
      {children}
    </motion.div>
  );
}
```

**Reduced motion**: Add to globals.css:
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

---

## 7. Library Utilities

### 7.1 `lib/cn.ts`

```typescript
import { clsx, type ClassValue } from 'clsx';

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}
```

Note: `tailwind-merge` is NOT in package.json. Use `clsx` only. If class conflicts become an issue, add `tailwind-merge` later.

### 7.2 `lib/constants.ts`

```typescript
export const SECTION_IDS = {
  hero: 'hero',
  howItWorks: 'how-it-works',
  features: 'features',
  pricing: 'pricing',
  faq: 'faq',
} as const;

export const GITHUB_URL = 'https://github.com/usephalanx/phalanx';
export const DOCS_URL = 'https://github.com/usephalanx/phalanx#readme';
```

### 7.3 `lib/hooks/useActiveSection.ts`

```typescript
'use client';
import { useState, useEffect } from 'react';
import { SECTION_IDS } from '../constants';

export function useActiveSection(): string {
  const [active, setActive] = useState<string>('hero');

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) setActive(entry.target.id);
        }
      },
      { rootMargin: '-40% 0px -55% 0px' }
    );

    Object.values(SECTION_IDS).forEach((id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });

    return () => observer.disconnect();
  }, []);

  return active;
}
```

---

## 8. Responsive Behavior

### Layout per breakpoint

| Section      | Base (< 600px)                   | sm (≥ 600px)                     | md (≥ 900px)                     | lg (≥ 1100px)            |
|--------------|----------------------------------|----------------------------------|----------------------------------|--------------------------|
| Navbar       | Logo + hamburger, no inline links | Same as base                    | Inline links visible, no burger  | Same as md               |
| Hero         | Stack CTAs vertically, text-3xl  | text-4xl, CTAs inline            | text-5xl                         | text-6xl                 |
| LogoBar      | Ticker, smaller logos            | Ticker                           | Ticker                           | Ticker                   |
| HowItWorks   | 1 column, vertical connector     | 2×2 grid                        | 4 columns, horizontal connector  | Same as md               |
| Features     | 1 column                         | 2 columns                        | 2 columns                        | 3 columns                |
| Pricing      | 1 column, stacked cards          | 1 column                         | 3 columns side-by-side           | Same as md               |
| FAQ          | Full-width                       | Full-width                       | max-w-2xl centered               | Same as md               |
| CtaBanner    | p-8, text-2xl                    | p-10, text-3xl                   | p-16, text-4xl                   | Same as md               |
| Footer       | Stacked center-aligned           | Stacked center-aligned           | Horizontal row                   | Same as md               |

---

## 9. Accessibility Requirements

### 9.1 Landmarks

| Element      | Role / Tag                        | Label                            |
|--------------|-----------------------------------|----------------------------------|
| Skip nav     | `<a>` (visually hidden)          | "Skip to content"                |
| Navbar       | `<nav aria-label="Main">`        | "Main navigation"                |
| Main         | `<main id="main-content">`       | —                                |
| Each section | `<section id="..." aria-labelledby>` | Linked to SectionHeading h2   |
| Footer       | `<footer>`                        | —                                |

### 9.2 Interactive Components

| Component    | Requirements                                                                 |
|--------------|------------------------------------------------------------------------------|
| Navbar links | Focus-visible ring, smooth scroll via `href="#id"`, active state conveyed    |
| Mobile menu  | Focus trap, `Escape` closes, `aria-modal`, `aria-expanded` on trigger        |
| Buttons      | `focus-visible:ring-2 ring-brand-blue ring-offset-2 ring-offset-bg`          |
| Accordion    | `aria-expanded`, `aria-controls`, `role="region"`, `Enter`/`Space` toggles   |
| Social links | `aria-label="Phalanx on {platform}"` (screen reader text)                    |

### 9.3 Color Contrast (WCAG AA)

| Pair                                  | Ratio  | Requirement | Status  |
|---------------------------------------|--------|-------------|---------|
| `text.DEFAULT` (#E4E8F1) on `bg` (#050608) | 15.3:1 | 4.5:1       | ✅ Pass |
| `text.secondary` (#8891AB) on `bg` (#050608) | 6.5:1 | 4.5:1      | ✅ Pass |
| `text.muted` (#555E78) on `bg` (#050608) | 3.3:1 | 3:1 (large)  | ✅ Pass (large text only) |
| `brand.blue` (#7AA2F7) on `bg` (#050608) | 8.1:1 | 4.5:1       | ✅ Pass |
| `bg` (#050608) on `brand.blue` (#7AA2F7) | 8.1:1 | 4.5:1       | ✅ Pass (button text) |

**Rule**: Never use `text.muted` for body text below 18px/bold or 24px/regular.

### 9.4 Motion

- All `framer-motion` animations respect `prefers-reduced-motion: reduce` via the global CSS rule in Section 6.9.
- `animate-ticker` (LogoBar) is paused on `prefers-reduced-motion: reduce`.
- No auto-playing video; any demo.mp4 usage requires user-initiated play.

---

## 10. Animation Plan

| Section      | Animation Type           | Trigger         | Duration | Easing                   | Delay          |
|--------------|--------------------------|-----------------|----------|--------------------------|----------------|
| Hero badge   | fade-up                  | mount           | 500ms    | `[0.25, 0.1, 0.25, 1]`  | 0ms            |
| Hero h1      | fade-up                  | mount           | 500ms    | same                     | 100ms          |
| Hero sub     | fade-up                  | mount           | 500ms    | same                     | 200ms          |
| Hero CTAs    | fade-up                  | mount           | 500ms    | same                     | 300ms          |
| Hero terminal| fade-up                  | mount           | 600ms    | same                     | 500ms          |
| LogoBar      | continuous ticker        | always          | 30s      | linear                   | —              |
| HowItWorks   | fade-up per card         | whileInView     | 500ms    | same                     | index × 100ms  |
| Features     | fade-up per card         | whileInView     | 500ms    | same                     | index × 80ms   |
| Pricing      | fade-up per card         | whileInView     | 500ms    | same                     | index × 100ms  |
| FAQ items    | fade-up per item         | whileInView     | 400ms    | same                     | index × 60ms   |
| Accordion    | height auto-animate      | toggle          | 300ms    | `easeInOut`              | —              |
| CtaBanner    | fade-up                  | whileInView     | 600ms    | same                     | 0ms            |

---

## 11. Performance Budget

| Metric                    | Target       |
|---------------------------|--------------|
| First Contentful Paint    | < 1.0s       |
| Largest Contentful Paint  | < 2.0s       |
| Total JS bundle (gzipped) | < 80KB      |
| CLS                       | < 0.05      |
| Lighthouse Performance    | ≥ 95        |

**Static export** (`output: 'export'`): No Node.js server at runtime. All HTML pre-rendered at build time. Hosted as static files on any CDN.

**Images**: `unoptimized: true` in next.config.mjs (required for static export). Use SVGs for icons/logos. Any raster images should be pre-optimized (WebP, compressed).

**Font loading**: `next/font/google` with `display: 'swap'` prevents FOIT. Only Inter and JetBrains Mono loaded.

---

## 12. Build & Dev Commands

```bash
# Install dependencies
cd site-next && npm install

# Development server (port 3000)
npm run dev

# Production build (static export to out/)
npm run build

# Verify build output
ls out/index.html
```

---

## 13. Composition in page.tsx

Final `app/page.tsx` structure:

```typescript
import { SkipNav } from '@/components/ui/SkipNav';
import { Navbar } from '@/components/sections/Navbar';
import { Hero } from '@/components/sections/Hero';
import { LogoBar } from '@/components/sections/LogoBar';
import { HowItWorks } from '@/components/sections/HowItWorks';
import { Features } from '@/components/sections/Features';
import { Pricing } from '@/components/sections/Pricing';
import { Faq } from '@/components/sections/Faq';
import { CtaBanner } from '@/components/sections/CtaBanner';
import { Footer } from '@/components/sections/Footer';

export default function HomePage() {
  return (
    <>
      <SkipNav />
      <Navbar />
      <main id="main-content" className="min-h-screen">
        <Hero />
        <LogoBar />
        <HowItWorks />
        <Features />
        <Pricing />
        <Faq />
        <CtaBanner />
      </main>
      <Footer />
    </>
  );
}
```

---

## 14. Files to Create (ordered)

| #  | Path                                       | Server/Client | Dependencies             |
|----|---------------------------------------------|---------------|--------------------------|
| 1  | `src/lib/cn.ts`                             | —             | clsx                     |
| 2  | `src/lib/constants.ts`                      | —             | —                        |
| 3  | `src/lib/hooks/useActiveSection.ts`         | Client        | constants.ts             |
| 4  | `src/data/content.ts`                       | — (modify)    | Add NavLink, SocialLink, new exports |
| 5  | `src/components/ui/Container.tsx`           | Server        | cn.ts                    |
| 6  | `src/components/ui/Icon.tsx`                | Server        | lucide-react             |
| 7  | `src/components/ui/Button.tsx`              | Client        | cn.ts                    |
| 8  | `src/components/ui/Badge.tsx`               | Server        | cn.ts                    |
| 9  | `src/components/ui/SectionHeading.tsx`      | Server        | —                        |
| 10 | `src/components/ui/Card.tsx`                | Server        | cn.ts                    |
| 11 | `src/components/ui/AnimateIn.tsx`           | Client        | framer-motion            |
| 12 | `src/components/ui/Accordion.tsx`           | Client        | framer-motion, Icon      |
| 13 | `src/components/ui/SkipNav.tsx`             | Server        | —                        |
| 14 | `src/components/sections/Navbar.tsx`        | Client        | Icon, Button, Container, useActiveSection |
| 15 | `src/components/sections/Hero.tsx`          | Server        | Badge, Button, Container |
| 16 | `src/components/sections/LogoBar.tsx`       | Server        | Container                |
| 17 | `src/components/sections/HowItWorks.tsx`    | Client        | SectionHeading, Card, Icon, AnimateIn, Container |
| 18 | `src/components/sections/Features.tsx`      | Client        | SectionHeading, Card, Icon, AnimateIn, Container |
| 19 | `src/components/sections/Pricing.tsx`       | Server        | SectionHeading, Card, Button, Icon, Badge, Container |
| 20 | `src/components/sections/Faq.tsx`           | Client        | SectionHeading, Accordion, Container |
| 21 | `src/components/sections/CtaBanner.tsx`     | Server        | Button, Container        |
| 22 | `src/components/sections/Footer.tsx`        | Server        | Icon, Container          |
| 23 | `src/app/globals.css`                       | — (modify)    | Add reduced-motion rule  |
| 24 | `tailwind.config.ts`                        | — (modify)    | Add animations/keyframes |
| 25 | `src/app/page.tsx`                          | Server (modify)| Compose all sections    |

---

## 15. Test Strategy

### Unit Tests (add `vitest` + `@testing-library/react` to devDependencies)

| Test File                              | Test Function                                           | Verifies                                           |
|----------------------------------------|---------------------------------------------------------|----------------------------------------------------|
| `__tests__/ui/Button.test.tsx`         | `test_renders_primary_variant`                          | Primary class applied                              |
| `__tests__/ui/Button.test.tsx`         | `test_renders_as_link_when_href_provided`               | `<a>` rendered instead of `<button>`               |
| `__tests__/ui/Accordion.test.tsx`      | `test_toggles_open_close_on_click`                      | Content visible/hidden on click                    |
| `__tests__/ui/Accordion.test.tsx`      | `test_aria_expanded_matches_state`                      | `aria-expanded` toggles                            |
| `__tests__/ui/Icon.test.tsx`           | `test_renders_known_icon`                               | BrainCircuit renders an SVG                        |
| `__tests__/ui/Icon.test.tsx`           | `test_returns_null_for_unknown_icon`                    | Unknown name → null                                |
| `__tests__/sections/Pricing.test.tsx`  | `test_renders_three_tiers`                              | 3 tier cards in DOM                                |
| `__tests__/sections/Pricing.test.tsx`  | `test_highlighted_tier_has_aria_label`                  | `aria-label="Recommended plan"` present            |
| `__tests__/sections/Features.test.tsx` | `test_renders_six_feature_cards`                        | 6 cards in DOM                                     |
| `__tests__/sections/Faq.test.tsx`      | `test_renders_all_faq_items`                            | 6 accordion items                                  |

### Build Verification

```bash
cd site-next && npm run build   # Must exit 0 and produce out/index.html
```

### Accessibility Audit

Run `npx axe-cli out/index.html` or Lighthouse accessibility audit — must score ≥ 90.
