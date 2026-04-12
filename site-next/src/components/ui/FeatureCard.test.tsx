// ---------------------------------------------------------------------------
// FeatureCard — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import FeatureCard from './FeatureCard';

const defaultProps = {
  icon: '🧠',
  title: 'AI Planning',
  description: 'Breaks down prompts into executable task graphs.',
};

describe('FeatureCard', () => {
  it('renders without crashing', () => {
    const { container } = render(<FeatureCard {...defaultProps} />);
    expect(container.firstChild).toBeInTheDocument();
  });

  // -- Icon -------------------------------------------------------------------

  it('renders the icon content', () => {
    render(<FeatureCard {...defaultProps} />);
    expect(screen.getByText('🧠')).toBeInTheDocument();
  });

  it('renders the icon inside a colored circle', () => {
    render(<FeatureCard {...defaultProps} />);
    const iconEl = screen.getByText('🧠');
    expect(iconEl.className).toContain('rounded-full');
    expect(iconEl.className).toContain('bg-primary-600/15');
  });

  it('marks the icon container as aria-hidden', () => {
    render(<FeatureCard {...defaultProps} />);
    const iconEl = screen.getByText('🧠');
    expect(iconEl).toHaveAttribute('aria-hidden', 'true');
  });

  it('renders an SVG placeholder string as icon', () => {
    render(<FeatureCard {...defaultProps} icon="⚙️" />);
    expect(screen.getByText('⚙️')).toBeInTheDocument();
  });

  // -- Title ------------------------------------------------------------------

  it('renders the title as an h3 element', () => {
    render(<FeatureCard {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading).toHaveTextContent('AI Planning');
  });

  it('applies bold white styling to the title', () => {
    render(<FeatureCard {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading.className).toContain('font-bold');
    expect(heading.className).toContain('text-white');
  });

  // -- Description ------------------------------------------------------------

  it('renders the description text', () => {
    render(<FeatureCard {...defaultProps} />);
    expect(screen.getByText(defaultProps.description)).toBeInTheDocument();
  });

  it('applies muted text styling to the description', () => {
    render(<FeatureCard {...defaultProps} />);
    const desc = screen.getByText(defaultProps.description);
    expect(desc.className).toContain('text-text-muted');
    expect(desc.className).toContain('text-sm');
  });

  // -- Card styling -----------------------------------------------------------

  it('applies rounded-xl and border classes to the card', () => {
    const { container } = render(<FeatureCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('rounded-xl');
    expect(card.className).toContain('border');
    expect(card.className).toContain('border-border');
  });

  it('applies p-6 padding to the card', () => {
    const { container } = render(<FeatureCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('p-6');
  });

  it('applies hover shadow transition classes', () => {
    const { container } = render(<FeatureCard {...defaultProps} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('hover:shadow-lg');
    expect(card.className).toContain('transition-shadow');
  });

  // -- className --------------------------------------------------------------

  it('merges additional className', () => {
    const { container } = render(<FeatureCard {...defaultProps} className="mt-8" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('mt-8');
    // Base classes should still be present
    expect(card.className).toContain('rounded-xl');
  });

  // -- Full prop combination --------------------------------------------------

  it('renders all props together', () => {
    render(
      <FeatureCard
        icon="🔒"
        title="Security"
        description="Scans for vulnerabilities automatically."
        className="custom-card"
      />,
    );

    expect(screen.getByText('🔒')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Security');
    expect(screen.getByText('Scans for vulnerabilities automatically.')).toBeInTheDocument();
  });
});
