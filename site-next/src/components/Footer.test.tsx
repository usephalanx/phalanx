// ---------------------------------------------------------------------------
// Footer — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import Footer, { type FooterProps } from './Footer';

// Mock child UI components to isolate Footer logic
jest.mock('@/components/ui/Container', () => ({
  Container: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

jest.mock('@/components/ui/Icon', () => {
  return {
    __esModule: true,
    default: ({ name, className }: { name: string; className?: string }) => (
      <span data-testid={`icon-${name}`} className={className} />
    ),
  };
});

const defaultProps: FooterProps = {
  brandName: 'Phalanx',
  tagline: 'AI-powered security for modern development teams.',
  columns: [
    {
      title: 'Product',
      links: [
        { label: 'Features', href: '#features' },
        { label: 'Pricing', href: '#pricing' },
      ],
    },
    {
      title: 'Company',
      links: [
        { label: 'About', href: '/about' },
        { label: 'Blog', href: '/blog' },
      ],
    },
    {
      title: 'Legal',
      links: [
        { label: 'Privacy', href: '/privacy' },
        { label: 'Terms', href: '/terms' },
      ],
    },
  ],
  socialLinks: [
    { platform: 'GitHub', href: 'https://github.com/usephalanx/phalanx', icon: 'Github' },
    { platform: 'Twitter', href: 'https://twitter.com/usephalanx', icon: 'Twitter' },
  ],
  copyright: '© 2026 Phalanx. All rights reserved.',
};

describe('Footer', () => {
  // -- Rendering --------------------------------------------------------------

  it('renders without crashing', () => {
    render(<Footer {...defaultProps} />);
    expect(screen.getByTestId('footer')).toBeInTheDocument();
  });

  it('renders the brand name', () => {
    render(<Footer {...defaultProps} />);
    expect(screen.getByText('Phalanx')).toBeInTheDocument();
  });

  it('renders the tagline', () => {
    render(<Footer {...defaultProps} />);
    expect(screen.getByText(defaultProps.tagline)).toBeInTheDocument();
  });

  it('renders the copyright text', () => {
    render(<Footer {...defaultProps} />);
    expect(screen.getByText(defaultProps.copyright)).toBeInTheDocument();
  });

  // -- Link columns -----------------------------------------------------------

  it('renders all column titles', () => {
    render(<Footer {...defaultProps} />);
    for (const column of defaultProps.columns) {
      expect(screen.getByText(column.title)).toBeInTheDocument();
    }
  });

  it('renders all navigation links within columns', () => {
    render(<Footer {...defaultProps} />);
    for (const column of defaultProps.columns) {
      for (const link of column.links) {
        const el = screen.getByText(link.label);
        expect(el).toBeInTheDocument();
        expect(el.closest('a')).toHaveAttribute('href', link.href);
      }
    }
  });

  // -- Social links -----------------------------------------------------------

  it('renders social media links with correct hrefs', () => {
    render(<Footer {...defaultProps} />);
    for (const social of defaultProps.socialLinks) {
      const link = screen.getByLabelText(social.platform);
      expect(link).toHaveAttribute('href', social.href);
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    }
  });

  it('renders social media icons', () => {
    render(<Footer {...defaultProps} />);
    expect(screen.getByTestId('icon-Github')).toBeInTheDocument();
    expect(screen.getByTestId('icon-Twitter')).toBeInTheDocument();
  });

  it('does not render social section when socialLinks is empty', () => {
    render(<Footer {...defaultProps} socialLinks={[]} />);
    expect(screen.queryByLabelText('GitHub')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Twitter')).not.toBeInTheDocument();
  });

  // -- Props ------------------------------------------------------------------

  it('uses brandName from props (not hardcoded)', () => {
    render(<Footer {...defaultProps} brandName="TestBrand" />);
    expect(screen.getByText('TestBrand')).toBeInTheDocument();
  });

  it('uses tagline from props (not hardcoded)', () => {
    render(<Footer {...defaultProps} tagline="Custom tagline here." />);
    expect(screen.getByText('Custom tagline here.')).toBeInTheDocument();
  });

  it('uses copyright from props (not hardcoded)', () => {
    render(<Footer {...defaultProps} copyright="© 2025 Test Corp." />);
    expect(screen.getByText('© 2025 Test Corp.')).toBeInTheDocument();
  });

  it('renders custom columns from props', () => {
    const customColumns = [
      {
        title: 'Resources',
        links: [{ label: 'Docs', href: '/docs' }],
      },
    ];
    render(<Footer {...defaultProps} columns={customColumns} />);
    expect(screen.getByText('Resources')).toBeInTheDocument();
    expect(screen.getByText('Docs')).toBeInTheDocument();
    // Original columns should not appear
    expect(screen.queryByText('Product')).not.toBeInTheDocument();
  });

  it('applies additional className to root element', () => {
    render(<Footer {...defaultProps} className="mt-20" />);
    const footer = screen.getByTestId('footer');
    expect(footer.className).toContain('mt-20');
  });

  // -- Branding icon ----------------------------------------------------------

  it('renders the ShieldCheck brand icon', () => {
    render(<Footer {...defaultProps} />);
    expect(screen.getByTestId('icon-ShieldCheck')).toBeInTheDocument();
  });
});
