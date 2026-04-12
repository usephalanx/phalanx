// ---------------------------------------------------------------------------
// Navbar — unit tests
// ---------------------------------------------------------------------------

import { fireEvent, render, screen } from '@testing-library/react';

import Navbar from './Navbar';

// Mock useActiveSection so tests don't rely on IntersectionObserver
jest.mock('@/lib/hooks/useActiveSection', () => ({
  useActiveSection: () => 'hero',
}));

// Mock child UI components to isolate Navbar logic
jest.mock('@/components/ui/Container', () => ({
  Container: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

jest.mock('@/components/ui/Button', () => {
  return {
    __esModule: true,
    default: ({ children, href, className }: { children: React.ReactNode; href?: string; className?: string }) =>
      href ? <a href={href} className={className}>{children}</a> : <button className={className}>{children}</button>,
  };
});

jest.mock('@/components/ui/Icon', () => {
  return {
    __esModule: true,
    default: ({ name, className }: { name: string; className?: string }) => (
      <span data-testid={`icon-${name}`} className={className} />
    ),
  };
});

const defaultProps = {
  brandName: 'Phalanx',
  navLinks: [
    { label: 'Features', href: '#features' },
    { label: 'How It Works', href: '#how-it-works' },
    { label: 'Pricing', href: '#pricing' },
    { label: 'FAQ', href: '#faq' },
  ],
  ctaLabel: 'Get Started',
  ctaHref: '#pricing',
};

describe('Navbar', () => {
  // -- Rendering --------------------------------------------------------------

  it('renders without crashing', () => {
    render(<Navbar {...defaultProps} />);
    expect(screen.getByTestId('navbar')).toBeInTheDocument();
  });

  it('renders the brand name', () => {
    render(<Navbar {...defaultProps} />);
    // Brand name appears in both desktop nav and mobile drawer
    const brands = screen.getAllByText('Phalanx');
    expect(brands.length).toBeGreaterThanOrEqual(1);
  });

  it('renders all navigation links', () => {
    render(<Navbar {...defaultProps} />);
    for (const link of defaultProps.navLinks) {
      // Each link appears twice (desktop + mobile drawer)
      const matches = screen.getAllByText(link.label);
      expect(matches.length).toBeGreaterThanOrEqual(1);
    }
  });

  it('renders the CTA button with correct href', () => {
    render(<Navbar {...defaultProps} />);
    const ctas = screen.getAllByText('Get Started');
    expect(ctas.length).toBeGreaterThanOrEqual(1);
    // At least one should be an anchor with href
    const anchor = ctas.find((el) => el.tagName === 'A');
    expect(anchor).toBeDefined();
    expect(anchor).toHaveAttribute('href', '#pricing');
  });

  // -- Props ------------------------------------------------------------------

  it('uses brandName from props (not hardcoded)', () => {
    render(<Navbar {...defaultProps} brandName="TestBrand" />);
    expect(screen.getAllByText('TestBrand').length).toBeGreaterThanOrEqual(1);
  });

  it('uses ctaLabel from props', () => {
    render(<Navbar {...defaultProps} ctaLabel="Sign Up" />);
    expect(screen.getAllByText('Sign Up').length).toBeGreaterThanOrEqual(1);
  });

  it('renders custom navLinks from props', () => {
    const customLinks = [{ label: 'About', href: '#about' }];
    render(<Navbar {...defaultProps} navLinks={customLinks} />);
    expect(screen.getAllByText('About').length).toBeGreaterThanOrEqual(1);
    // Original links should not appear
    expect(screen.queryByText('Features')).not.toBeInTheDocument();
  });

  // -- Mobile hamburger -------------------------------------------------------

  it('has a hamburger button for mobile', () => {
    render(<Navbar {...defaultProps} />);
    const hamburger = screen.getByLabelText('Open menu');
    expect(hamburger).toBeInTheDocument();
  });

  it('opens mobile drawer when hamburger is clicked', () => {
    render(<Navbar {...defaultProps} />);
    const hamburger = screen.getByLabelText('Open menu');
    fireEvent.click(hamburger);

    // Overlay should be visible
    expect(screen.getByTestId('mobile-overlay')).toBeInTheDocument();
    // Drawer should be translated into view
    const drawer = screen.getByTestId('mobile-drawer');
    expect(drawer.className).toContain('translate-x-0');
  });

  it('closes mobile drawer when close button is clicked', () => {
    render(<Navbar {...defaultProps} />);

    // Open
    fireEvent.click(screen.getByLabelText('Open menu'));
    expect(screen.getByTestId('mobile-drawer').className).toContain('translate-x-0');

    // Close via close button inside drawer
    const closeButtons = screen.getAllByLabelText('Close menu');
    fireEvent.click(closeButtons[0]);

    // Drawer should slide away
    const drawer = screen.getByTestId('mobile-drawer');
    expect(drawer.className).toContain('translate-x-full');
  });

  it('closes mobile drawer when overlay is clicked', () => {
    render(<Navbar {...defaultProps} />);

    fireEvent.click(screen.getByLabelText('Open menu'));
    fireEvent.click(screen.getByTestId('mobile-overlay'));

    const drawer = screen.getByTestId('mobile-drawer');
    expect(drawer.className).toContain('translate-x-full');
  });

  it('closes mobile drawer when a nav link is clicked', () => {
    render(<Navbar {...defaultProps} />);

    fireEvent.click(screen.getByLabelText('Open menu'));
    // Click first link in the mobile drawer
    const mobileLinks = screen.getAllByText('Features');
    fireEvent.click(mobileLinks[mobileLinks.length - 1]);

    const drawer = screen.getByTestId('mobile-drawer');
    expect(drawer.className).toContain('translate-x-full');
  });

  // -- Scroll glass effect ----------------------------------------------------

  it('starts with transparent background (no scroll)', () => {
    render(<Navbar {...defaultProps} />);
    const nav = screen.getByTestId('navbar');
    expect(nav.className).toContain('bg-transparent');
    expect(nav.className).not.toContain('backdrop-blur-xl');
  });

  it('applies glass effect classes on scroll', () => {
    render(<Navbar {...defaultProps} />);

    // Simulate scroll
    Object.defineProperty(window, 'scrollY', { value: 50, writable: true });
    fireEvent.scroll(window);

    const nav = screen.getByTestId('navbar');
    expect(nav.className).toContain('backdrop-blur-xl');
    expect(nav.className).not.toContain('bg-transparent');
  });
});
