// ---------------------------------------------------------------------------
// SectionHeading — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import SectionHeading from './SectionHeading';

describe('SectionHeading', () => {
  it('renders without crashing', () => {
    render(<SectionHeading title="Test Title" />);
    expect(screen.getByText('Test Title')).toBeInTheDocument();
  });

  // -- Title ------------------------------------------------------------------

  it('renders the title as an h2 element', () => {
    render(<SectionHeading title="Features" />);
    const heading = screen.getByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent('Features');
  });

  it('applies large bold heading classes', () => {
    render(<SectionHeading title="Heading" />);
    const heading = screen.getByRole('heading', { level: 2 });
    expect(heading.className).toContain('text-3xl');
    expect(heading.className).toContain('md:text-4xl');
    expect(heading.className).toContain('font-bold');
    expect(heading.className).toContain('text-white');
  });

  // -- Subtitle ---------------------------------------------------------------

  it('renders subtitle when provided', () => {
    render(<SectionHeading title="Title" subtitle="A helpful subtitle" />);
    expect(screen.getByText('A helpful subtitle')).toBeInTheDocument();
  });

  it('does not render subtitle paragraph when omitted', () => {
    const { container } = render(<SectionHeading title="Title" />);
    const paragraphs = container.querySelectorAll('p');
    expect(paragraphs).toHaveLength(0);
  });

  it('applies muted text classes to subtitle', () => {
    render(<SectionHeading title="Title" subtitle="Sub" />);
    const sub = screen.getByText('Sub');
    expect(sub.className).toContain('text-text-secondary');
    expect(sub.className).toContain('text-lg');
  });

  // -- Overline ---------------------------------------------------------------

  it('renders overline when provided', () => {
    render(<SectionHeading title="Title" overline="SECTION" />);
    expect(screen.getByText('SECTION')).toBeInTheDocument();
  });

  it('does not render overline when omitted', () => {
    const { container } = render(<SectionHeading title="Title" />);
    const paragraphs = container.querySelectorAll('p');
    expect(paragraphs).toHaveLength(0);
  });

  it('applies overline styling classes', () => {
    render(<SectionHeading title="Title" overline="Label" />);
    const overline = screen.getByText('Label');
    expect(overline.className).toContain('uppercase');
    expect(overline.className).toContain('tracking-widest');
    expect(overline.className).toContain('text-primary-400');
    expect(overline.className).toContain('text-sm');
    expect(overline.className).toContain('font-semibold');
  });

  // -- Centered ---------------------------------------------------------------

  it('centers text by default', () => {
    const { container } = render(<SectionHeading title="Title" />);
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain('text-center');
  });

  it('removes text-center when centered is false', () => {
    const { container } = render(<SectionHeading title="Title" centered={false} />);
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).not.toContain('text-center');
  });

  it('applies text-center when centered is explicitly true', () => {
    const { container } = render(<SectionHeading title="Title" centered={true} />);
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain('text-center');
  });

  // -- className --------------------------------------------------------------

  it('merges additional className', () => {
    const { container } = render(<SectionHeading title="Title" className="mt-8" />);
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain('mt-8');
    // Base classes should still be present
    expect(wrapper.className).toContain('mb-12');
  });

  // -- Full prop combination --------------------------------------------------

  it('renders all props together', () => {
    const { container } = render(
      <SectionHeading
        title="Pricing"
        subtitle="Choose a plan"
        overline="Plans"
        centered={false}
        className="custom-class"
      />,
    );
    const wrapper = container.firstChild as HTMLElement;

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Pricing');
    expect(screen.getByText('Choose a plan')).toBeInTheDocument();
    expect(screen.getByText('Plans')).toBeInTheDocument();
    expect(wrapper.className).not.toContain('text-center');
    expect(wrapper.className).toContain('custom-class');
  });
});
