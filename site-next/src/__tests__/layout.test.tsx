import { render, screen } from '@testing-library/react';
import RootLayout, { metadata } from '@/app/layout';

/* next/font/google is mocked by jest moduleNameMapper or auto-mock;
   we provide a minimal stub so the component renders without errors. */
jest.mock('next/font/google', () => ({
  Inter: () => ({ variable: '--font-inter' }),
  Space_Grotesk: () => ({ variable: '--font-space-grotesk' }),
  JetBrains_Mono: () => ({ variable: '--font-jetbrains' }),
}));

describe('RootLayout', () => {
  it('renders children inside an html element with lang="en"', () => {
    const { container } = render(
      <RootLayout>
        <p>test child</p>
      </RootLayout>,
    );
    expect(screen.getByText('test child')).toBeInTheDocument();
    const html = container.querySelector('html');
    expect(html).toHaveAttribute('lang', 'en');
  });

  it('applies all three font CSS variable classes to the html element', () => {
    const { container } = render(
      <RootLayout>
        <span />
      </RootLayout>,
    );
    const html = container.querySelector('html');
    expect(html?.className).toContain('--font-inter');
    expect(html?.className).toContain('--font-space-grotesk');
    expect(html?.className).toContain('--font-jetbrains');
  });

  it('applies base styling classes to body', () => {
    const { container } = render(
      <RootLayout>
        <span />
      </RootLayout>,
    );
    const body = container.querySelector('body');
    expect(body?.className).toContain('bg-bg');
    expect(body?.className).toContain('text-text');
    expect(body?.className).toContain('font-sans');
    expect(body?.className).toContain('antialiased');
  });
});

describe('metadata', () => {
  it('has the correct title', () => {
    expect(metadata.title).toBe(
      'Phalanx — From Slack Command to Shipped Software',
    );
  });

  it('has a non-empty description', () => {
    expect(metadata.description).toBeTruthy();
  });

  it('has openGraph metadata with matching title', () => {
    expect(metadata.openGraph).toBeDefined();
    expect((metadata.openGraph as Record<string, unknown>).title).toBe(
      'Phalanx — From Slack Command to Shipped Software',
    );
  });

  it('has twitter card metadata', () => {
    expect(metadata.twitter).toBeDefined();
    expect((metadata.twitter as Record<string, unknown>).card).toBe(
      'summary_large_image',
    );
  });

  it('sets metadataBase to usephalanx.com', () => {
    expect(metadata.metadataBase).toEqual(new URL('https://usephalanx.com'));
  });
});
