import { render } from '@testing-library/react';

import Icon from '@/components/ui/Icon';

/* lucide-react icons are SVG components; we mock the module to keep
   tests fast and avoid bundling real SVGs in the test environment. */
jest.mock('lucide-react', () => {
  const React = require('react');
  const makeMock = (name: string) =>
    React.forwardRef(({ className }: { className?: string }, ref: unknown) =>
      React.createElement('span', { 'data-testid': name, className, ref }),
    );
  return {
    BrainCircuit: makeMock('BrainCircuit'),
    Check: makeMock('Check'),
    ChevronDown: makeMock('ChevronDown'),
    FlaskConical: makeMock('FlaskConical'),
    GitPullRequestArrow: makeMock('GitPullRequestArrow'),
    Github: makeMock('Github'),
    Hammer: makeMock('Hammer'),
    ListChecks: makeMock('ListChecks'),
    Menu: makeMock('Menu'),
    MessageCircle: makeMock('MessageCircle'),
    Rocket: makeMock('Rocket'),
    ShieldCheck: makeMock('ShieldCheck'),
    Terminal: makeMock('Terminal'),
    Twitter: makeMock('Twitter'),
    Users: makeMock('Users'),
    Workflow: makeMock('Workflow'),
    X: makeMock('X'),
  };
});

describe('Icon', () => {
  it('renders a known icon by name', () => {
    const { getByTestId } = render(<Icon name="Github" />);
    expect(getByTestId('Github')).toBeInTheDocument();
  });

  it('returns null for an unknown icon name', () => {
    const { container } = render(<Icon name="NonExistentIcon" />);
    expect(container.firstChild).toBeNull();
  });

  it('passes className to the rendered icon', () => {
    const { getByTestId } = render(
      <Icon name="Rocket" className="w-6 h-6" />,
    );
    const svg = getByTestId('Rocket');
    expect(svg.className).toContain('w-6');
    expect(svg.className).toContain('h-6');
  });

  it('applies shrink-0 base class', () => {
    const { getByTestId } = render(<Icon name="Terminal" />);
    expect(getByTestId('Terminal').className).toContain('shrink-0');
  });
});
