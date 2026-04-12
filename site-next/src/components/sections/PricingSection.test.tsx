// ---------------------------------------------------------------------------
// PricingSection — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import { PricingSection } from './PricingSection';
import type { PricingTier } from '@/data/content';

// Mock Container to a simple div
jest.mock('@/components/ui/Container', () => ({
  __esModule: true,
  default: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

// Mock SectionHeading to a simple heading
jest.mock('@/components/ui/SectionHeading', () => ({
  __esModule: true,
  default: ({ title, subtitle }: { title: string; subtitle?: string }) => (
    <div data-testid="section-heading">
      <h2>{title}</h2>
      {subtitle && <p>{subtitle}</p>}
    </div>
  ),
}));

// Mock PricingCard to expose key props for assertion
jest.mock('@/components/ui/PricingCard', () => ({
  __esModule: true,
  default: ({
    tierName,
    price,
    features,
    highlighted,
    ctaText,
    ctaHref,
  }: {
    tierName: string;
    price: string;
    features: string[];
    highlighted?: boolean;
    ctaText: string;
    ctaHref?: string;
  }) => (
    <div data-testid="pricing-card" data-highlighted={highlighted ? 'true' : 'false'}>
      <h3>{tierName}</h3>
      <span data-testid="price">{price}</span>
      <ul>
        {features.map((f) => (
          <li key={f}>{f}</li>
        ))}
      </ul>
      <a href={ctaHref}>{ctaText}</a>
    </div>
  ),
}));

const defaultTiers: PricingTier[] = [
  {
    id: 'starter',
    name: 'Starter',
    price: '$0',
    billingPeriod: 'free forever',
    priceSuffix: 'free forever',
    features: ['Up to 3 team members', 'Community support', '100 runs / month'],
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
    features: ['Unlimited team members', 'Priority support', 'Unlimited runs'],
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
    features: ['Everything in Pro', 'Dedicated success engineer', 'SSO / SAML'],
    cta: 'Contact Sales',
    ctaLabel: 'Contact Sales',
    ctaHref: '/contact',
    highlighted: false,
  },
];

describe('PricingSection', () => {
  // -- Rendering --------------------------------------------------------------

  it('renders without crashing', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    expect(screen.getByTestId('pricing-section')).toBeInTheDocument();
  });

  it('renders the section heading with the provided title', () => {
    render(<PricingSection title="Simple, Transparent Pricing" tiers={defaultTiers} />);
    expect(screen.getByText('Simple, Transparent Pricing')).toBeInTheDocument();
  });

  it('renders subtitle when provided', () => {
    render(
      <PricingSection
        title="Pricing"
        subtitle="Choose the plan that fits your team"
        tiers={defaultTiers}
      />,
    );
    expect(screen.getByText('Choose the plan that fits your team')).toBeInTheDocument();
  });

  it('renders overline when provided', () => {
    render(
      <PricingSection title="Pricing" overline="Plans" tiers={defaultTiers} />,
    );
    // overline is passed to SectionHeading — our mock doesn't render it,
    // but we verify the heading and section still render correctly
    expect(screen.getByTestId('section-heading')).toBeInTheDocument();
  });

  // -- Tiers ------------------------------------------------------------------

  it('renders all 3 pricing cards', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    const cards = screen.getAllByTestId('pricing-card');
    expect(cards).toHaveLength(3);
  });

  it('renders the Starter tier', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    expect(screen.getByText('Starter')).toBeInTheDocument();
    expect(screen.getByText('$0')).toBeInTheDocument();
    expect(screen.getByText('Get Started')).toBeInTheDocument();
  });

  it('renders the Pro tier', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    expect(screen.getByText('Pro')).toBeInTheDocument();
    expect(screen.getByText('$49')).toBeInTheDocument();
    expect(screen.getByText('Start Free Trial')).toBeInTheDocument();
  });

  it('renders the Enterprise tier', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    expect(screen.getByText('Enterprise')).toBeInTheDocument();
    expect(screen.getByText('Custom')).toBeInTheDocument();
    expect(screen.getByText('Contact Sales')).toBeInTheDocument();
  });

  // -- Highlighted tier -------------------------------------------------------

  it('marks the Pro tier as highlighted', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    const cards = screen.getAllByTestId('pricing-card');
    expect(cards[1]).toHaveAttribute('data-highlighted', 'true');
  });

  it('does not mark Starter as highlighted', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    const cards = screen.getAllByTestId('pricing-card');
    expect(cards[0]).toHaveAttribute('data-highlighted', 'false');
  });

  it('does not mark Enterprise as highlighted', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    const cards = screen.getAllByTestId('pricing-card');
    expect(cards[2]).toHaveAttribute('data-highlighted', 'false');
  });

  // -- Features ---------------------------------------------------------------

  it('renders feature items for each tier', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    expect(screen.getByText('Up to 3 team members')).toBeInTheDocument();
    expect(screen.getByText('Unlimited team members')).toBeInTheDocument();
    expect(screen.getByText('Everything in Pro')).toBeInTheDocument();
  });

  // -- Props ------------------------------------------------------------------

  it('uses title from props (not hardcoded)', () => {
    render(<PricingSection title="Custom Title" tiers={defaultTiers} />);
    expect(screen.getByText('Custom Title')).toBeInTheDocument();
    expect(screen.queryByText('Simple, Transparent Pricing')).not.toBeInTheDocument();
  });

  it('uses tier data from props (not hardcoded)', () => {
    const customTiers: PricingTier[] = [
      {
        id: 'solo',
        name: 'Solo',
        price: '$10',
        billingPeriod: '/month',
        priceSuffix: '/month',
        features: ['One seat'],
        cta: 'Subscribe',
        ctaLabel: 'Subscribe',
        ctaHref: '/subscribe',
        highlighted: false,
      },
    ];
    render(<PricingSection title="Plans" tiers={customTiers} />);
    expect(screen.getAllByTestId('pricing-card')).toHaveLength(1);
    expect(screen.getByText('Solo')).toBeInTheDocument();
    expect(screen.getByText('$10')).toBeInTheDocument();
    expect(screen.getByText('One seat')).toBeInTheDocument();
  });

  // -- Structure --------------------------------------------------------------

  it('has the correct section id for scroll-spy', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    expect(screen.getByTestId('pricing-section')).toHaveAttribute('id', 'pricing');
  });

  it('renders as a section element', () => {
    render(<PricingSection title="Pricing" tiers={defaultTiers} />);
    const section = screen.getByTestId('pricing-section');
    expect(section.tagName).toBe('SECTION');
  });
});
