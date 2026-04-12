import { render, screen } from '@testing-library/react';

import Container from '@/components/ui/Container';

describe('Container', () => {
  it('renders children', () => {
    render(
      <Container>
        <p>hello world</p>
      </Container>,
    );
    expect(screen.getByText('hello world')).toBeInTheDocument();
  });

  it('applies base classes', () => {
    const { container } = render(
      <Container>
        <span />
      </Container>,
    );
    const wrapper = container.firstElementChild;
    expect(wrapper?.className).toContain('max-w-content');
    expect(wrapper?.className).toContain('mx-auto');
    expect(wrapper?.className).toContain('px-6');
  });

  it('merges additional className', () => {
    const { container } = render(
      <Container className="mt-8">
        <span />
      </Container>,
    );
    const wrapper = container.firstElementChild;
    expect(wrapper?.className).toContain('mt-8');
    expect(wrapper?.className).toContain('max-w-content');
  });
});
