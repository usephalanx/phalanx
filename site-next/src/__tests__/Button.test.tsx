import { render, screen } from '@testing-library/react';

import Button from '@/components/ui/Button';

describe('Button', () => {
  it('renders as a <button> by default', () => {
    render(<Button>Click me</Button>);
    const el = screen.getByRole('button', { name: 'Click me' });
    expect(el.tagName).toBe('BUTTON');
  });

  it('renders as an <a> when href is provided', () => {
    render(<Button href="/docs">Docs</Button>);
    const el = screen.getByRole('link', { name: 'Docs' });
    expect(el.tagName).toBe('A');
    expect(el).toHaveAttribute('href', '/docs');
  });

  it('applies primary variant classes by default', () => {
    render(<Button>Primary</Button>);
    const el = screen.getByRole('button', { name: 'Primary' });
    expect(el.className).toContain('bg-primary-600');
    expect(el.className).toContain('text-white');
  });

  it('applies secondary variant classes', () => {
    render(<Button variant="secondary">Secondary</Button>);
    const el = screen.getByRole('button', { name: 'Secondary' });
    expect(el.className).toContain('bg-transparent');
    expect(el.className).toContain('border');
    expect(el.className).toContain('border-primary-400');
  });

  it('applies ghost variant classes', () => {
    render(<Button variant="ghost">Ghost</Button>);
    const el = screen.getByRole('button', { name: 'Ghost' });
    expect(el.className).toContain('text-text-secondary');
  });

  it('applies size classes', () => {
    const { rerender } = render(<Button size="sm">Small</Button>);
    expect(screen.getByRole('button').className).toContain('text-sm');

    rerender(<Button size="lg">Large</Button>);
    expect(screen.getByRole('button').className).toContain('text-lg');
  });

  it('merges additional className', () => {
    render(<Button className="mt-4">Custom</Button>);
    expect(screen.getByRole('button').className).toContain('mt-4');
  });

  it('applies focus-visible ring classes', () => {
    render(<Button>Focus</Button>);
    const el = screen.getByRole('button');
    expect(el.className).toContain('focus-visible:ring-2');
    expect(el.className).toContain('focus-visible:ring-primary-400');
  });
});
