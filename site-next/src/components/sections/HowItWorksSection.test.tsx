// ---------------------------------------------------------------------------
// HowItWorksSection — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import { HowItWorksSection } from './HowItWorksSection';
import type { HowItWorksStep } from '@/data/content';

// Mock Container to a simple div
jest.mock('@/components/ui/Container', () => ({
  __esModule: true,
  default: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

// Mock SectionHeading to a simple heading
jest.mock('@/components/ui/SectionHeading', () => ({
  __esModule: true,
  default: ({ title, subtitle }: { title: string; subtitle?: string }) => (
    <div data-testid="section-heading">
      <h2>{title}</h2>
      {subtitle && <p>{subtitle}</p>}
    </div>
  ),
}));

// Mock StepCard to a simple card
jest.mock('@/components/ui/StepCard', () => ({
  __esModule: true,
  default: ({
    stepNumber,
    title,
    description,
  }: {
    stepNumber: number;
    title: string;
    description: string;
    icon: string;
    showConnector?: boolean;
  }) => (
    <div data-testid="step-card">
      <span data-testid="step-number">{stepNumber}</span>
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  ),
}));

const defaultSteps: HowItWorksStep[] = [
  {
    step: 1,
    title: 'Slack Command',
    description: 'Type /phalanx build in any Slack channel.',
    icon: 'Terminal',
    agentColor: 'agent-cmd',
  },
  {
    step: 2,
    title: 'AI Plans',
    description: 'The planner agent analyses your codebase and breaks the request into tasks.',
    icon: 'ListChecks',
    agentColor: 'agent-plan',
  },
  {
    step: 3,
    title: 'Builds & Reviews',
    description: 'Builder agents write the code. Reviewer and security agents audit every change.',
    icon: 'Hammer',
    agentColor: 'agent-build',
  },
  {
    step: 4,
    title: 'Ships',
    description: 'After approval the release agent opens a PR, merges, and deploys.',
    icon: 'Rocket',
    agentColor: 'agent-rel',
  },
];

describe('HowItWorksSection', () => {
  // -- Rendering --------------------------------------------------------------

  it('renders without crashing', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByTestId('how-it-works-section')).toBeInTheDocument();
  });

  it('renders the section heading with the provided title', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByText('How It Works')).toBeInTheDocument();
  });

  it('renders subtitle when provided', () => {
    render(
      <HowItWorksSection
        title="How It Works"
        subtitle="Four simple steps"
        steps={defaultSteps}
      />,
    );
    expect(screen.getByText('Four simple steps')).toBeInTheDocument();
  });

  // -- Steps ------------------------------------------------------------------

  it('renders all 4 step cards', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    const cards = screen.getAllByTestId('step-card');
    expect(cards).toHaveLength(4);
  });

  it('renders step 1 — Slack Command', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByText('Slack Command')).toBeInTheDocument();
    expect(
      screen.getByText('Type /phalanx build in any Slack channel.'),
    ).toBeInTheDocument();
  });

  it('renders step 2 — AI Plans', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByText('AI Plans')).toBeInTheDocument();
  });

  it('renders step 3 — Builds & Reviews', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByText('Builds & Reviews')).toBeInTheDocument();
  });

  it('renders step 4 — Ships', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByText('Ships')).toBeInTheDocument();
  });

  it('displays correct step numbers', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    const stepNumbers = screen.getAllByTestId('step-number');
    expect(stepNumbers.map((el) => el.textContent)).toEqual(['1', '2', '3', '4']);
  });

  // -- Props ------------------------------------------------------------------

  it('uses title from props (not hardcoded)', () => {
    render(<HowItWorksSection title="Custom Title" steps={defaultSteps} />);
    expect(screen.getByText('Custom Title')).toBeInTheDocument();
    expect(screen.queryByText('How It Works')).not.toBeInTheDocument();
  });

  it('uses step data from props (not hardcoded)', () => {
    const customSteps: HowItWorksStep[] = [
      {
        step: 1,
        title: 'Custom Step',
        description: 'Custom description',
        icon: 'Star',
        agentColor: 'agent-cmd',
      },
    ];
    render(<HowItWorksSection title="Title" steps={customSteps} />);
    expect(screen.getAllByTestId('step-card')).toHaveLength(1);
    expect(screen.getByText('Custom Step')).toBeInTheDocument();
    expect(screen.getByText('Custom description')).toBeInTheDocument();
  });

  // -- Structure --------------------------------------------------------------

  it('has the correct section id for scroll-spy', () => {
    render(<HowItWorksSection title="How It Works" steps={defaultSteps} />);
    expect(screen.getByTestId('how-it-works-section')).toHaveAttribute(
      'id',
      'how-it-works',
    );
  });
});
