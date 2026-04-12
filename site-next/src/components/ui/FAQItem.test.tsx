// ---------------------------------------------------------------------------
// FAQItem — unit tests
// ---------------------------------------------------------------------------

import { fireEvent, render, screen } from '@testing-library/react';

import FAQItem from './FAQItem';

const defaultProps = {
  question: 'What is Phalanx?',
  answer: 'An AI-powered team operating system that ships code autonomously.',
};

describe('FAQItem', () => {
  it('renders without crashing', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    expect(container.firstChild).toBeInTheDocument();
  });

  // -- Question (summary) -----------------------------------------------------

  it('renders the question text', () => {
    render(<FAQItem {...defaultProps} />);
    expect(screen.getByText(defaultProps.question)).toBeInTheDocument();
  });

  it('renders the question inside a summary element', () => {
    render(<FAQItem {...defaultProps} />);
    const summary = screen.getByText(defaultProps.question);
    expect(summary.tagName).toBe('SUMMARY');
  });

  it('applies font-semibold and text-white to the summary', () => {
    render(<FAQItem {...defaultProps} />);
    const summary = screen.getByText(defaultProps.question);
    expect(summary.className).toContain('font-semibold');
    expect(summary.className).toContain('text-white');
  });

  // -- Answer -----------------------------------------------------------------

  it('renders the answer text', () => {
    render(<FAQItem {...defaultProps} defaultOpen />);
    expect(screen.getByText(defaultProps.answer)).toBeInTheDocument();
  });

  it('applies text-text-secondary styling to the answer', () => {
    render(<FAQItem {...defaultProps} defaultOpen />);
    const answer = screen.getByText(defaultProps.answer);
    expect(answer.className).toContain('text-text-secondary');
    expect(answer.className).toContain('text-sm');
  });

  // -- Chevron icon -----------------------------------------------------------

  it('renders a chevron SVG icon', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const svg = container.querySelector('svg');
    expect(svg).toBeInTheDocument();
  });

  it('marks the chevron as aria-hidden', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const svg = container.querySelector('svg');
    expect(svg).toHaveAttribute('aria-hidden', 'true');
  });

  it('applies rotation transition classes to the chevron', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const svg = container.querySelector('svg');
    expect(svg?.className.baseVal || svg?.getAttribute('class')).toContain(
      'transition-transform',
    );
  });

  // -- Closed state (default) -------------------------------------------------

  it('is closed by default', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const details = container.querySelector('details');
    expect(details).not.toHaveAttribute('open');
  });

  // -- Open state (defaultOpen) -----------------------------------------------

  it('is open when defaultOpen is true', () => {
    const { container } = render(<FAQItem {...defaultProps} defaultOpen />);
    const details = container.querySelector('details');
    expect(details).toHaveAttribute('open');
  });

  // -- Toggle interaction -----------------------------------------------------

  it('toggles open when the summary is clicked', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const details = container.querySelector('details') as HTMLDetailsElement;
    const summary = screen.getByText(defaultProps.question);

    expect(details.open).toBe(false);

    fireEvent.click(summary);
    expect(details.open).toBe(true);
  });

  // -- Border / styling -------------------------------------------------------

  it('applies border-b and py-4 classes to the root', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain('border-b');
    expect(root.className).toContain('border-border');
    expect(root.className).toContain('py-4');
  });

  it('renders as a <details> element', () => {
    const { container } = render(<FAQItem {...defaultProps} />);
    const root = container.firstChild as HTMLElement;
    expect(root.tagName).toBe('DETAILS');
  });

  // -- className --------------------------------------------------------------

  it('merges additional className', () => {
    const { container } = render(<FAQItem {...defaultProps} className="mt-8" />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain('mt-8');
    expect(root.className).toContain('border-b');
  });

  // -- Grid-rows animation wrapper --------------------------------------------

  it('wraps the answer in a grid transition container', () => {
    const { container } = render(<FAQItem {...defaultProps} defaultOpen />);
    const answer = screen.getByText(defaultProps.answer);
    // The answer <p> is inside an overflow-hidden div, inside a grid div
    const overflowDiv = answer.parentElement as HTMLElement;
    expect(overflowDiv.className).toContain('overflow-hidden');
    const gridDiv = overflowDiv.parentElement as HTMLElement;
    expect(gridDiv.className).toContain('grid');
  });

  // -- Full prop combination --------------------------------------------------

  it('renders all props together', () => {
    const { container } = render(
      <FAQItem
        question="How does billing work?"
        answer="We bill monthly based on usage."
        defaultOpen
        className="custom-faq"
      />,
    );

    expect(screen.getByText('How does billing work?')).toBeInTheDocument();
    expect(screen.getByText('We bill monthly based on usage.')).toBeInTheDocument();

    const root = container.firstChild as HTMLElement;
    expect(root).toHaveAttribute('open');
    expect(root.className).toContain('custom-faq');
  });
});
