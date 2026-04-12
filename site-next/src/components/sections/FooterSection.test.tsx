// ---------------------------------------------------------------------------
// FooterSection — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import FooterSection from './FooterSection';
import type { FooterSectionProps } from './FooterSection';

const defaultColumns = [
  {
    title: 'Product',
    links: [
      { label: 'Features', href: '#features' },
      { label: 'Pricing', href: '#pricing' },
      { label: 'Documentation', href: '#docs' },
    ],
  },
  {
    title: 'Company',
    links: [
      { label: 'About', href: '#about' },
      { label: 'Blog', href: '#blog' },
    ],
  },
  {
    title: 'Legal',
    links: [
      { label: 'Privacy', href: '#privacy' },
      { label: 'Terms', href: '#terms' },
    ],
  },
];

const defaultSocialLinks = [
  { platform: 'GitHub', href: 'https://github.com/test', icon: 'Github' },
  { platform: 'Twitter', href: 'https://twitter.com/test', icon: 'Twitter' },
];

const defaultProps: FooterSectionProps = {
  brandName: 'Phalanx',
  tagline: 'AI Agents in Formation. From Slack to Shipped.',
  linkColumns: defaultColumns,
  socialLinks: defaultSocialLinks,
  copyright: '© 2026 Phalanx. All rights reserved.',
};

describe('FooterSection', () => {
  it('renders without crashing', () => {
    render(<FooterSection {...defaultProps} />);
    expect(
      screen.getByRole('contentinfo', { name: 'Site footer' }),
    ).toBeInTheDocument();
  });

  // -- Brand ------------------------------------------------------------------

  it('displays the brand name from props', () => {
    render(<FooterSection {...defaultProps} />);
    expect(screen.getByText('Phalanx')).toBeInTheDocument();
  });

  it('displays the tagline from props', () => {
    render(<FooterSection {...defaultProps} />);
    expect(
      screen.getByText('AI Agents in Formation. From Slack to Shipped.'),
    ).toBeInTheDocument();
  });

  it('renders a custom brand name when provided', () => {
    render(<FooterSection {...defaultProps} brandName="Acme" />);
    expect(screen.getByText('Acme')).toBeInTheDocument();
  });

  // -- Copyright --------------------------------------------------------------

  it('displays the copyright text from props', () => {
    render(<FooterSection {...defaultProps} />);
    expect(
      screen.getByText('© 2026 Phalanx. All rights reserved.'),
    ).toBeInTheDocument();
  });

  it('renders a custom copyright string', () => {
    render(
      <FooterSection {...defaultProps} copyright="© 2025 Custom Corp." />,
    );
    expect(screen.getByText('© 2025 Custom Corp.')).toBeInTheDocument();
  });

  // -- Link columns -----------------------------------------------------------

  it('renders all link column headings', () => {
    render(<FooterSection {...defaultProps} />);
    expect(screen.getByText('Product')).toBeInTheDocument();
    expect(screen.getByText('Company')).toBeInTheDocument();
    expect(screen.getByText('Legal')).toBeInTheDocument();
  });

  it('renders all links within each column', () => {
    render(<FooterSection {...defaultProps} />);
    // Product column
    expect(screen.getByText('Features')).toBeInTheDocument();
    expect(screen.getByText('Pricing')).toBeInTheDocument();
    expect(screen.getByText('Documentation')).toBeInTheDocument();
    // Company column
    expect(screen.getByText('About')).toBeInTheDocument();
    expect(screen.getByText('Blog')).toBeInTheDocument();
    // Legal column
    expect(screen.getByText('Privacy')).toBeInTheDocument();
    expect(screen.getByText('Terms')).toBeInTheDocument();
  });

  it('renders links with correct href attributes', () => {
    render(<FooterSection {...defaultProps} />);
    const featuresLink = screen.getByText('Features').closest('a');
    expect(featuresLink).toHaveAttribute('href', '#features');
    const privacyLink = screen.getByText('Privacy').closest('a');
    expect(privacyLink).toHaveAttribute('href', '#privacy');
  });

  // -- Social links -----------------------------------------------------------

  it('renders social links with accessible labels', () => {
    render(<FooterSection {...defaultProps} />);
    expect(screen.getByLabelText('GitHub')).toBeInTheDocument();
    expect(screen.getByLabelText('Twitter')).toBeInTheDocument();
  });

  it('social links open in a new tab', () => {
    render(<FooterSection {...defaultProps} />);
    const github = screen.getByLabelText('GitHub');
    expect(github).toHaveAttribute('target', '_blank');
    expect(github).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('renders without social links when none provided', () => {
    render(<FooterSection {...defaultProps} socialLinks={undefined} />);
    expect(screen.queryByLabelText('GitHub')).not.toBeInTheDocument();
  });

  // -- Custom className -------------------------------------------------------

  it('merges additional className on the footer element', () => {
    const { container } = render(
      <FooterSection {...defaultProps} className="mt-section" />,
    );
    const footer = container.querySelector('footer');
    expect(footer?.className).toContain('mt-section');
  });

  // -- Dark background --------------------------------------------------------

  it('applies dark slate-900 background', () => {
    const { container } = render(<FooterSection {...defaultProps} />);
    const bg = container.querySelector('.bg-slate-900');
    expect(bg).toBeInTheDocument();
  });
});
