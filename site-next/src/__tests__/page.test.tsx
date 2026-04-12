/**
 * HomePage — integration-level smoke tests.
 *
 * Child section components are mocked so the page test focuses on
 * composition, ordering, and the presence of key landmarks.
 */

import { render, screen } from '@testing-library/react';
import HomePage from '@/app/page';

// ---------------------------------------------------------------------------
// Mock all child components to isolate page composition logic.
// This avoids coupling to internal section markup and client-side hooks.
// ---------------------------------------------------------------------------

jest.mock('@/components/Navbar', () => {
  return {
    __esModule: true,
    default: ({ brandName }: { brandName: string }) => (
      <nav data-testid="navbar">{brandName}</nav>
    ),
  };
});

jest.mock('@/components/sections/HeroSection', () => ({
  HeroSection: () => <section data-testid="hero-section">Hero</section>,
}));

jest.mock('@/components/sections/LogoBarSection', () => ({
  LogoBarSection: () => <section data-testid="logo-bar-section">LogoBar</section>,
}));

jest.mock('@/components/sections/FeaturesSection', () => ({
  FeaturesSection: () => <section data-testid="features-section">Features</section>,
}));

jest.mock('@/components/sections/HowItWorksSection', () => ({
  HowItWorksSection: () => <section data-testid="how-it-works-section">HowItWorks</section>,
}));

jest.mock('@/components/sections/PricingSection', () => ({
  PricingSection: () => <section data-testid="pricing-section">Pricing</section>,
}));

jest.mock('@/components/sections/FAQSection', () => ({
  FAQSection: () => <section data-testid="faq-section">FAQ</section>,
}));

jest.mock('@/components/sections/CTASection', () => ({
  __esModule: true,
  default: () => <section data-testid="cta-section">CTA</section>,
}));

jest.mock('@/components/sections/FooterSection', () => ({
  __esModule: true,
  default: () => <footer data-testid="footer-section">Footer</footer>,
}));

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('HomePage', () => {
  it('renders the Navbar with the brand name', () => {
    render(<HomePage />);
    expect(screen.getByTestId('navbar')).toHaveTextContent('Phalanx');
  });

  it('renders a main landmark element', () => {
    render(<HomePage />);
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('renders all expected sections', () => {
    render(<HomePage />);
    expect(screen.getByTestId('hero-section')).toBeInTheDocument();
    expect(screen.getByTestId('features-section')).toBeInTheDocument();
    expect(screen.getByTestId('how-it-works-section')).toBeInTheDocument();
    expect(screen.getByTestId('pricing-section')).toBeInTheDocument();
    expect(screen.getByTestId('faq-section')).toBeInTheDocument();
    expect(screen.getByTestId('cta-section')).toBeInTheDocument();
    expect(screen.getByTestId('footer-section')).toBeInTheDocument();
  });

  it('renders sections in the correct visual order', () => {
    render(<HomePage />);
    const main = screen.getByRole('main');
    const sectionIds = Array.from(main.querySelectorAll('[data-testid]')).map(
      (el) => el.getAttribute('data-testid'),
    );
    expect(sectionIds).toEqual([
      'hero-section',
      'logo-bar-section',
      'features-section',
      'how-it-works-section',
      'pricing-section',
      'faq-section',
      'cta-section',
    ]);
  });
});
