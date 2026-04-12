// ---------------------------------------------------------------------------
// CTASection — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import CTASection from './CTASection';

const defaultProps = {
  headline: 'Ready to Transform Your Dev Workflow?',
  subheadline:
    'Deploy your AI-powered engineering team in minutes. Free to start.',
  ctaLabel: 'Get Started Free',
  ctaHref: '/signup',
};

describe('CTASection', () => {
  it('renders without crashing', () => {
    render(<CTASection {...defaultProps} />);
    expect(
      screen.getByRole('heading', { name: defaultProps.headline }),
    ).toBeInTheDocument();
  });

  // -- Heading ----------------------------------------------------------------

  it('displays the headline text from props', () => {
    render(<CTASection {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent(defaultProps.headline);
  });

  it('renders a custom headline when provided', () => {
    render(<CTASection {...defaultProps} headline="Ship Code Faster" />);
    expect(screen.getByRole('heading')).toHaveTextContent('Ship Code Faster');
  });

  // -- Subtitle ---------------------------------------------------------------

  it('displays the subheadline text from props', () => {
    render(<CTASection {...defaultProps} />);
    expect(screen.getByText(defaultProps.subheadline)).toBeInTheDocument();
  });

  // -- CTA button -------------------------------------------------------------

  it('renders the CTA button with correct label', () => {
    render(<CTASection {...defaultProps} />);
    const link = screen.getByRole('link', { name: defaultProps.ctaLabel });
    expect(link).toBeInTheDocument();
  });

  it('renders the CTA button with correct href', () => {
    render(<CTASection {...defaultProps} />);
    const link = screen.getByRole('link', { name: defaultProps.ctaLabel });
    expect(link).toHaveAttribute('href', '/signup');
  });

  it('renders as an anchor element (link) for the CTA', () => {
    render(<CTASection {...defaultProps} />);
    const link = screen.getByRole('link', { name: defaultProps.ctaLabel });
    expect(link.tagName).toBe('A');
  });

  // -- Section semantics ------------------------------------------------------

  it('has the correct section id for scroll-spy', () => {
    const { container } = render(<CTASection {...defaultProps} />);
    const section = container.querySelector('section');
    expect(section).toHaveAttribute('id', 'cta');
  });

  it('has an accessible aria-label', () => {
    render(<CTASection {...defaultProps} />);
    expect(
      screen.getByRole('region', { name: 'Call to action' }),
    ).toBeInTheDocument();
  });

  // -- Gradient container classes ---------------------------------------------

  it('applies gradient background classes', () => {
    const { container } = render(<CTASection {...defaultProps} />);
    const gradientDiv = container.querySelector('.rounded-2xl');
    expect(gradientDiv?.className).toContain('bg-gradient-to-r');
    expect(gradientDiv?.className).toContain('from-indigo-600');
    expect(gradientDiv?.className).toContain('to-violet-600');
  });

  // -- Custom className -------------------------------------------------------

  it('merges additional className on the section', () => {
    const { container } = render(
      <CTASection {...defaultProps} className="mt-section" />,
    );
    const section = container.querySelector('section');
    expect(section?.className).toContain('mt-section');
  });
});
