// ---------------------------------------------------------------------------
// FAQSection — unit tests
// ---------------------------------------------------------------------------

import { render, screen } from '@testing-library/react';

import { FAQSection } from './FAQSection';
import type { FaqItem } from '@/data/content';

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

// Mock FAQItem to expose question and answer for assertion
jest.mock('@/components/ui/FAQItem', () => ({
  __esModule: true,
  default: ({ question, answer }: { question: string; answer: string }) => (
    <div data-testid="faq-item">
      <dt>{question}</dt>
      <dd>{answer}</dd>
    </div>
  ),
}));

const defaultItems: FaqItem[] = [
  {
    id: 'what-is-phalanx',
    question: 'What exactly is Phalanx?',
    answer:
      'Phalanx is an open-source AI team operating system.',
  },
  {
    id: 'human-approval',
    question: 'Do agents push code without human approval?',
    answer:
      'Never. Every critical stage has an approval gate.',
  },
  {
    id: 'self-host',
    question: 'Can I self-host Phalanx?',
    answer:
      'Yes. Phalanx is fully self-hostable.',
  },
  {
    id: 'llm-providers',
    question: 'Which LLM providers are supported?',
    answer:
      'Phalanx uses the Anthropic Claude API by default.',
  },
  {
    id: 'secrets-handling',
    question: 'How does Phalanx handle secrets and credentials?',
    answer:
      'Secrets are passed via environment variables.',
  },
  {
    id: 'repo-limits',
    question: 'Is there a limit on repository size or language support?',
    answer:
      'No hard limits.',
  },
];

describe('FAQSection', () => {
  // -- Rendering --------------------------------------------------------------

  it('renders without crashing', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByTestId('faq-section')).toBeInTheDocument();
  });

  it('renders the section heading with the provided title', () => {
    render(<FAQSection title="Frequently Asked Questions" items={defaultItems} />);
    expect(screen.getByText('Frequently Asked Questions')).toBeInTheDocument();
  });

  it('renders subtitle when provided', () => {
    render(
      <FAQSection
        title="FAQ"
        subtitle="Everything you need to know"
        items={defaultItems}
      />,
    );
    expect(screen.getByText('Everything you need to know')).toBeInTheDocument();
  });

  it('renders overline when provided', () => {
    render(
      <FAQSection title="FAQ" overline="Support" items={defaultItems} />,
    );
    expect(screen.getByTestId('section-heading')).toBeInTheDocument();
  });

  // -- FAQ Items --------------------------------------------------------------

  it('renders all 6 FAQ items', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    const items = screen.getAllByTestId('faq-item');
    expect(items).toHaveLength(6);
  });

  it('renders the first FAQ question', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('What exactly is Phalanx?')).toBeInTheDocument();
  });

  it('renders the second FAQ question', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('Do agents push code without human approval?')).toBeInTheDocument();
  });

  it('renders the self-host FAQ question', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('Can I self-host Phalanx?')).toBeInTheDocument();
  });

  it('renders the LLM providers FAQ question', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('Which LLM providers are supported?')).toBeInTheDocument();
  });

  it('renders the secrets FAQ question', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('How does Phalanx handle secrets and credentials?')).toBeInTheDocument();
  });

  it('renders the repo limits FAQ question', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('Is there a limit on repository size or language support?')).toBeInTheDocument();
  });

  it('renders FAQ answers', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByText('Phalanx is an open-source AI team operating system.')).toBeInTheDocument();
    expect(screen.getByText('Never. Every critical stage has an approval gate.')).toBeInTheDocument();
  });

  // -- Props ------------------------------------------------------------------

  it('uses title from props (not hardcoded)', () => {
    render(<FAQSection title="Custom FAQ Title" items={defaultItems} />);
    expect(screen.getByText('Custom FAQ Title')).toBeInTheDocument();
    expect(screen.queryByText('Frequently Asked Questions')).not.toBeInTheDocument();
  });

  it('uses item data from props (not hardcoded)', () => {
    const customItems: FaqItem[] = [
      {
        id: 'custom-1',
        question: 'Is this customizable?',
        answer: 'Absolutely.',
      },
    ];
    render(<FAQSection title="FAQ" items={customItems} />);
    expect(screen.getAllByTestId('faq-item')).toHaveLength(1);
    expect(screen.getByText('Is this customizable?')).toBeInTheDocument();
    expect(screen.getByText('Absolutely.')).toBeInTheDocument();
  });

  // -- Structure --------------------------------------------------------------

  it('has the correct section id for scroll-spy', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    expect(screen.getByTestId('faq-section')).toHaveAttribute('id', 'faq');
  });

  it('renders as a section element', () => {
    render(<FAQSection title="FAQ" items={defaultItems} />);
    const section = screen.getByTestId('faq-section');
    expect(section.tagName).toBe('SECTION');
  });
});
