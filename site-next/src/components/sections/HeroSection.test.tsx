// ---------------------------------------------------------------------------
// HeroSection — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import { HeroSection } from './HeroSection';
import type { HeroContent } from '@/data/content';

// Mock Container to a simple div
jest.mock('@/components/ui/Container', () => ({
  __esModule: true,
  default: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

// Mock TerminalDemo — client component not needed in unit tests
jest.mock('@/components/sections/TerminalDemo', () => ({
  TerminalDemo: ({ className }: { className?: string }) => (
    <div data-testid="terminal-demo" className={className} />
  ),
}));

const defaultContent: HeroContent = {
  headline: 'Ship production code with autonomous AI agents',
  subheadline:
    'Phalanx orchestrates a team of specialized AI agents that plan, build, review, test, and secure your code.',
  primaryCta: { label: 'Start Building — Free', href: '/signup' },
  secondaryCta: { label: 'See How It Works', href: '#how-it-works' },
};

describe('HeroSection', () => {
  // -- Rendering ------------------------------------------------------------

  it('renders without crashing', () => {
    render(<HeroSection heroContent={defaultContent} />);
    expect(screen.getByTestId('hero-section')).toBeInTheDocument();
  });

  it('renders the headline from props', () => {
    render(<HeroSection heroContent={defaultContent} />);
    expect(
      screen.getByText('Ship production code with autonomous AI agents'),
    ).toBeInTheDocument();
  });

  it('renders the subheadline from props', () => {
    render(<HeroSection heroContent={defaultContent} />);
    expect(
      screen.getByText(defaultContent.subheadline),
    ).toBeInTheDocument();
  });

  it('renders the primary CTA button with correct label and href', () => {
    render(<HeroSection heroContent={defaultContent} />);
    const primaryCta = screen.getByText('Start Building — Free');
    expect(primaryCta).toBeInTheDocument();
    expect(primaryCta.closest('a')).toHaveAttribute('href', '/signup');
  });

  it('renders the secondary CTA button with correct label and href', () => {
    render(<HeroSection heroContent={defaultContent} />);
    const secondaryCta = screen.getByText('See How It Works');
    expect(secondaryCta).toBeInTheDocument();
    expect(secondaryCta.closest('a')).toHaveAttribute('href', '#how-it-works');
  });

  // -- Props ----------------------------------------------------------------

  it('uses headline from props (not hardcoded)', () => {
    const custom: HeroContent = {
      ...defaultContent,
      headline: 'Custom Headline Text',
    };
    render(<HeroSection heroContent={custom} />);
    expect(screen.getByText('Custom Headline Text')).toBeInTheDocument();
    expect(
      screen.queryByText('Ship production code with autonomous AI agents'),
    ).not.toBeInTheDocument();
  });

  it('uses CTA labels from props (not hardcoded)', () => {
    const custom: HeroContent = {
      ...defaultContent,
      primaryCta: { label: 'Get Started', href: '/start' },
      secondaryCta: { label: 'Learn More', href: '#learn' },
    };
    render(<HeroSection heroContent={custom} />);
    expect(screen.getByText('Get Started')).toBeInTheDocument();
    expect(screen.getByText('Learn More')).toBeInTheDocument();
    expect(screen.queryByText('Start Building — Free')).not.toBeInTheDocument();
  });

  it('uses CTA hrefs from props (not hardcoded)', () => {
    const custom: HeroContent = {
      ...defaultContent,
      primaryCta: { label: 'Go', href: '/custom-signup' },
      secondaryCta: { label: 'Info', href: '#custom-section' },
    };
    render(<HeroSection heroContent={custom} />);
    expect(screen.getByText('Go').closest('a')).toHaveAttribute('href', '/custom-signup');
    expect(screen.getByText('Info').closest('a')).toHaveAttribute('href', '#custom-section');
  });

  // -- Structure ------------------------------------------------------------

  it('has the correct section id for scroll-spy', () => {
    render(<HeroSection heroContent={defaultContent} />);
    expect(screen.getByTestId('hero-section')).toHaveAttribute('id', 'hero');
  });

  it('renders the TerminalDemo component', () => {
    render(<HeroSection heroContent={defaultContent} />);
    expect(screen.getByTestId('terminal-demo')).toBeInTheDocument();
  });

  it('applies min-h-screen for full viewport height', () => {
    render(<HeroSection heroContent={defaultContent} />);
    const section = screen.getByTestId('hero-section');
    expect(section.className).toContain('min-h-screen');
  });
});
