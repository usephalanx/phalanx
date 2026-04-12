// ---------------------------------------------------------------------------
// StepCard — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import StepCard from './StepCard';

const defaultProps = {
  stepNumber: 1,
  title: 'Plan',
  description: 'The Planner agent breaks down your prompt into tasks.',
  icon: 'BrainCircuit',
};

describe('StepCard', () => {
  it('renders without crashing', () => {
    const { container } = render(<StepCard {...defaultProps} />);
    expect(container.firstChild).toBeInTheDocument();
  });

  // -- Step number ------------------------------------------------------------

  it('renders the step number', () => {
    render(<StepCard {...defaultProps} />);
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('displays step number inside a rounded-full indigo circle', () => {
    render(<StepCard {...defaultProps} />);
    const circle = screen.getByLabelText('Step 1');
    expect(circle.className).toContain('rounded-full');
    expect(circle.className).toContain('bg-primary-600');
  });

  it('renders a different step number', () => {
    render(<StepCard {...defaultProps} stepNumber={3} />);
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByLabelText('Step 3')).toBeInTheDocument();
  });

  // -- Title ------------------------------------------------------------------

  it('renders the title as an h3 element', () => {
    render(<StepCard {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading).toHaveTextContent('Plan');
  });

  it('applies bold white styling to the title', () => {
    render(<StepCard {...defaultProps} />);
    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading.className).toContain('font-bold');
    expect(heading.className).toContain('text-white');
  });

  // -- Description ------------------------------------------------------------

  it('renders the description text', () => {
    render(<StepCard {...defaultProps} />);
    expect(screen.getByText(defaultProps.description)).toBeInTheDocument();
  });

  it('applies muted text styling to the description', () => {
    render(<StepCard {...defaultProps} />);
    const desc = screen.getByText(defaultProps.description);
    expect(desc.className).toContain('text-text-muted');
    expect(desc.className).toContain('text-sm');
  });

  // -- Connector line ---------------------------------------------------------

  it('renders the connector line by default', () => {
    render(<StepCard {...defaultProps} />);
    expect(screen.getByTestId('step-connector')).toBeInTheDocument();
  });

  it('marks the connector line as aria-hidden', () => {
    render(<StepCard {...defaultProps} />);
    const connector = screen.getByTestId('step-connector');
    expect(connector).toHaveAttribute('aria-hidden', 'true');
  });

  it('hides the connector line when showConnector is false', () => {
    render(<StepCard {...defaultProps} showConnector={false} />);
    expect(screen.queryByTestId('step-connector')).not.toBeInTheDocument();
  });

  // -- Icon -------------------------------------------------------------------

  it('renders the icon via the Icon component', () => {
    const { container } = render(<StepCard {...defaultProps} />);
    // Lucide icons render as SVG elements
    const svg = container.querySelector('svg');
    expect(svg).toBeInTheDocument();
  });

  // -- className --------------------------------------------------------------

  it('merges additional className', () => {
    const { container } = render(<StepCard {...defaultProps} className="mt-8" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('mt-8');
    // Base classes should still be present
    expect(card.className).toContain('flex');
    expect(card.className).toContain('gap-5');
  });

  // -- Full prop combination --------------------------------------------------

  it('renders all props together', () => {
    render(
      <StepCard
        stepNumber={5}
        title="Deploy"
        description="Release agent ships to production."
        icon="Rocket"
        showConnector={false}
        className="custom-step"
      />,
    );

    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByLabelText('Step 5')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Deploy');
    expect(screen.getByText('Release agent ships to production.')).toBeInTheDocument();
    expect(screen.queryByTestId('step-connector')).not.toBeInTheDocument();
  });
});
