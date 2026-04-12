// ---------------------------------------------------------------------------
// Button — unit tests
// ---------------------------------------------------------------------------

import { fireEvent, render, screen } from '@testing-library/react';

import Button from './Button';

describe('Button', () => {
  it('renders without crashing', () => {
    render(<Button>Click me</Button>);
    expect(screen.getByRole('button', { name: 'Click me' })).toBeInTheDocument();
  });

  it('renders children text', () => {
    render(<Button>Hello World</Button>);
    expect(screen.getByText('Hello World')).toBeInTheDocument();
  });

  // -- Variants ---------------------------------------------------------------

  it('applies primary variant classes by default', () => {
    render(<Button>Primary</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('bg-primary-600');
    expect(btn.className).toContain('text-white');
    expect(btn.className).toContain('hover:scale-[1.02]');
  });

  it('applies secondary variant classes', () => {
    render(<Button variant="secondary">Secondary</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('bg-transparent');
    expect(btn.className).toContain('border');
    expect(btn.className).toContain('border-primary-400');
    expect(btn.className).toContain('hover:bg-primary-600');
  });

  it('applies ghost variant classes', () => {
    render(<Button variant="ghost">Ghost</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('bg-transparent');
    expect(btn.className).toContain('text-text-secondary');
    expect(btn.className).toContain('hover:bg-bg-elevated');
  });

  // -- Sizes ------------------------------------------------------------------

  it('applies md size classes by default', () => {
    render(<Button>Medium</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('text-base');
    expect(btn.className).toContain('px-6');
    expect(btn.className).toContain('py-3');
  });

  it('applies sm size classes', () => {
    render(<Button size="sm">Small</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('text-sm');
    expect(btn.className).toContain('px-4');
    expect(btn.className).toContain('py-2');
  });

  it('applies lg size classes', () => {
    render(<Button size="lg">Large</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('text-lg');
    expect(btn.className).toContain('px-8');
    expect(btn.className).toContain('py-4');
  });

  // -- Polymorphic rendering --------------------------------------------------

  it('renders as <a> when href is provided', () => {
    render(<Button href="https://example.com">Link</Button>);
    const el = screen.getByText('Link');
    expect(el.tagName).toBe('A');
    expect(el).toHaveAttribute('href', 'https://example.com');
  });

  it('renders as <button> when href is not provided', () => {
    render(<Button>Btn</Button>);
    expect(screen.getByText('Btn').tagName).toBe('BUTTON');
  });

  it('applies variant classes to anchor element', () => {
    render(
      <Button href="/signup" variant="secondary">
        Sign Up
      </Button>,
    );
    const el = screen.getByText('Sign Up');
    expect(el.tagName).toBe('A');
    expect(el.className).toContain('border-primary-400');
  });

  it('applies size classes to anchor element', () => {
    render(
      <Button href="/go" size="lg">
        Go
      </Button>,
    );
    const el = screen.getByText('Go');
    expect(el.className).toContain('px-8');
  });

  // -- Event handling ---------------------------------------------------------

  it('calls onClick when clicked', () => {
    const handleClick = jest.fn();
    render(<Button onClick={handleClick}>Click</Button>);
    fireEvent.click(screen.getByRole('button'));
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  // -- Custom className -------------------------------------------------------

  it('merges additional className', () => {
    render(<Button className="mt-4">Styled</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('mt-4');
    // Base classes should still be present
    expect(btn.className).toContain('inline-flex');
  });

  // -- Disabled state ---------------------------------------------------------

  it('supports disabled attribute', () => {
    render(<Button disabled>Disabled</Button>);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  // -- Focus ring classes -----------------------------------------------------

  it('includes focus-visible ring classes', () => {
    render(<Button>Focus</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('focus-visible:ring-2');
    expect(btn.className).toContain('focus-visible:ring-primary-400');
  });

  // -- Transition / animation classes -----------------------------------------

  it('includes transition classes for animations', () => {
    render(<Button>Animated</Button>);
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('transition-all');
    expect(btn.className).toContain('duration-200');
  });
});
