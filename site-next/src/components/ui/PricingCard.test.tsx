// ---------------------------------------------------------------------------
// PricingCard — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import PricingCard from './PricingCard';

const defaultProps = {
  tierName: 'Pro',
  price: '$49/mo',
  features: ['Unlimited builds', 'Priority support', 'Custom agents'],
  ctaText: 'Get Started',
};

describe('PricingCard', () => {
  it('renders without crashing', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    expect(container.firstChild).toBeInTheDocument();
  });

  // -- Tier name ----------------------------------------------------------------

  it('renders the tier name as an h3 element', () => {
    render(<PricingCard {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading).toHaveTextContent('Pro');
  });

  it('applies bold white styling to the tier name', () => {
    render(<PricingCard {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading.className).toContain('font-bold');
    expect(heading.className).toContain('text-white');
  });

  // -- Price --------------------------------------------------------------------

  it('renders the price string', () => {
    render(<PricingCard {...defaultProps} />);
    expect(screen.getByText('$49/mo')).toBeInTheDocument();
  });

  it('applies extrabold styling to the price', () => {
    render(<PricingCard {...defaultProps} />);
    const price = screen.getByText('$49/mo');
    expect(price.className).toContain('font-extrabold');
    expect(price.className).toContain('text-white');
  });

  // -- Features -----------------------------------------------------------------

  it('renders all features in a list', () => {
    render(<PricingCard {...defaultProps} />);
    const list = screen.getByRole('list');
    expect(list).toBeInTheDocument();
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(3);
  });

  it('renders each feature text', () => {
    render(<PricingCard {...defaultProps} />);
    expect(screen.getByText('Unlimited builds')).toBeInTheDocument();
    expect(screen.getByText('Priority support')).toBeInTheDocument();
    expect(screen.getByText('Custom agents')).toBeInTheDocument();
  });

  it('renders a checkmark icon for each feature', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    const svgs = container.querySelectorAll('svg');
    expect(svgs).toHaveLength(3);
  });

  it('marks checkmark icons as aria-hidden', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    const svgs = container.querySelectorAll('svg');
    svgs.forEach((svg) => {
      expect(svg).toHaveAttribute('aria-hidden', 'true');
    });
  });

  it('renders empty list when no features provided', () => {
    render(<PricingCard {...defaultProps} features={[]} />);
    const list = screen.getByRole('list');
    expect(list).toBeInTheDocument();
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
  });

  // -- CTA button ---------------------------------------------------------------

  it('renders the CTA button with provided text', () => {
    render(<PricingCard {...defaultProps} />);
    expect(screen.getByRole('button', { name: 'Get Started' })).toBeInTheDocument();
  });

  it('renders CTA as a link when ctaHref is provided', () => {
    render(<PricingCard {...defaultProps} ctaHref="/signup" />);
    const link = screen.getByText('Get Started');
    expect(link.tagName).toBe('A');
    expect(link).toHaveAttribute('href', '/signup');
  });

  // -- Normal (non-highlighted) state -------------------------------------------

  it('does not show ring classes when not highlighted', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).not.toContain('ring-2');
    expect(card.className).not.toContain('ring-indigo-500');
  });

  it('does not show Popular badge when not highlighted', () => {
    render(<PricingCard {...defaultProps} />);
    expect(screen.queryByText('Popular')).not.toBeInTheDocument();
  });

  it('renders CTA with secondary variant when not highlighted', () => {
    render(<PricingCard {...defaultProps} />);
    const btn = screen.getByRole('button', { name: 'Get Started' });
    expect(btn.className).toContain('border');
    expect(btn.className).toContain('border-primary-400');
  });

  // -- Highlighted state --------------------------------------------------------

  it('applies ring-2 ring-indigo-500 when highlighted', () => {
    const { container } = render(<PricingCard {...defaultProps} highlighted />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('ring-2');
    expect(card.className).toContain('ring-indigo-500');
  });

  it('shows Popular badge when highlighted', () => {
    render(<PricingCard {...defaultProps} highlighted />);
    const badge = screen.getByText('Popular');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('bg-indigo-500');
    expect(badge.className).toContain('text-white');
    expect(badge.className).toContain('rounded-full');
  });

  it('renders CTA with primary variant when highlighted', () => {
    render(<PricingCard {...defaultProps} highlighted />);
    const btn = screen.getByRole('button', { name: 'Get Started' });
    expect(btn.className).toContain('bg-primary-600');
    expect(btn.className).toContain('text-white');
  });

  // -- Card styling -------------------------------------------------------------

  it('applies rounded-xl and border classes to the card', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('rounded-xl');
    expect(card.className).toContain('border');
    expect(card.className).toContain('border-border');
  });

  it('applies p-6 padding to the card', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('p-6');
  });

  it('applies hover shadow transition classes', () => {
    const { container } = render(<PricingCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('hover:shadow-lg');
    expect(card.className).toContain('transition-shadow');
  });

  // -- className ----------------------------------------------------------------

  it('merges additional className', () => {
    const { container } = render(<PricingCard {...defaultProps} className="mt-8" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('mt-8');
    expect(card.className).toContain('rounded-xl');
  });

  // -- Full prop combination ----------------------------------------------------

  it('renders all props together in highlighted state', () => {
    render(
      <PricingCard
        tierName="Enterprise"
        price="Custom"
        features={['SSO', 'SLA', 'Dedicated support']}
        highlighted
        ctaText="Contact Sales"
        ctaHref="/contact"
        className="custom-card"
      />,
    );

    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Enterprise');
    expect(screen.getByText('Custom')).toBeInTheDocument();
    expect(screen.getByText('Popular')).toBeInTheDocument();
    expect(screen.getByText('SSO')).toBeInTheDocument();
    expect(screen.getByText('SLA')).toBeInTheDocument();
    expect(screen.getByText('Dedicated support')).toBeInTheDocument();
    const cta = screen.getByText('Contact Sales');
    expect(cta.tagName).toBe('A');
    expect(cta).toHaveAttribute('href', '/contact');
  });
});
