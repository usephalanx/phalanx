'use client';

// ---------------------------------------------------------------------------
// Navbar — sticky top navigation with glass effect, desktop links, mobile
// hamburger drawer, and scroll-spy active-link highlighting.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useState } from 'react';

import type { NavLink } from '@/data/content';
import { cn } from '@/lib/cn';
import { useActiveSection } from '@/lib/hooks/useActiveSection';

import Button from '@/components/ui/Button';
import { Container } from '@/components/ui/Container';
import Icon from '@/components/ui/Icon';

/** Props for the {@link Navbar} component. */
export interface NavbarProps {
  /** Brand name displayed next to the logo. */
  brandName: string;
  /** Navigation link items rendered in the centre. */
  navLinks: NavLink[];
  /** Label for the primary CTA button. */
  ctaLabel: string;
  /** Href for the primary CTA button. */
  ctaHref: string;
}

/**
 * Sticky top navbar with backdrop-blur glass effect on scroll,
 * centred section anchor links with active highlighting, a primary
 * CTA on the right, and a mobile hamburger menu with slide-in drawer.
 */
export default function Navbar({
  brandName,
  navLinks,
  ctaLabel,
  ctaHref,
}: NavbarProps) {
  const [scrolled, setScrolled] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const activeSection = useActiveSection();

  // Track scroll position for glass effect
  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 10);
    handleScroll(); // initial check
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  // Lock body scroll when mobile drawer is open
  useEffect(() => {
    document.body.style.overflow = mobileOpen ? 'hidden' : '';
    return () => {
      document.body.style.overflow = '';
    };
  }, [mobileOpen]);

  const closeMobile = useCallback(() => setMobileOpen(false), []);

  /** Check if a nav link matches the current active section. */
  const isActive = (href: string): boolean => {
    const id = href.replace('#', '');
    return activeSection === id || activeSection === id.replace(/-/g, '');
    // handles "how-it-works" → "howItWorks" mismatch via section IDs
  };

  return (
    <>
      <nav
        data-testid="navbar"
        className={cn(
          'fixed inset-x-0 top-0 z-50 transition-all duration-300',
          scrolled
            ? 'bg-bg/80 shadow-lg shadow-black/5 backdrop-blur-xl border-b border-border/50'
            : 'bg-transparent',
        )}
      >
        <Container className="flex h-16 items-center justify-between">
          {/* Brand */}
          <a href="#" className="flex items-center gap-2 font-display text-lg font-bold text-text">
            <Icon name="ShieldCheck" className="h-6 w-6 text-brand-blue" />
            {brandName}
          </a>

          {/* Desktop links */}
          <ul className="hidden items-center gap-1 md:flex" role="navigation">
            {navLinks.map((link) => (
              <li key={link.href}>
                <a
                  href={link.href}
                  className={cn(
                    'rounded-md px-3 py-2 text-sm font-medium transition-colors duration-200',
                    isActive(link.href)
                      ? 'text-brand-blue'
                      : 'text-text-secondary hover:text-text',
                  )}
                >
                  {link.label}
                </a>
              </li>
            ))}
          </ul>

          {/* Desktop CTA + mobile hamburger */}
          <div className="flex items-center gap-3">
            <div className="hidden md:block">
              <Button href={ctaHref} size="sm">
                {ctaLabel}
              </Button>
            </div>
            <button
              type="button"
              aria-label={mobileOpen ? 'Close menu' : 'Open menu'}
              className="inline-flex items-center justify-center rounded-md p-2 text-text-secondary hover:text-text md:hidden"
              onClick={() => setMobileOpen((prev) => !prev)}
            >
              <Icon name={mobileOpen ? 'X' : 'Menu'} className="h-6 w-6" />
            </button>
          </div>
        </Container>
      </nav>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          data-testid="mobile-overlay"
          className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm md:hidden"
          onClick={closeMobile}
          aria-hidden="true"
        />
      )}

      {/* Mobile drawer */}
      <aside
        data-testid="mobile-drawer"
        className={cn(
          'fixed right-0 top-0 z-50 flex h-full w-72 flex-col bg-bg-card p-6 shadow-2xl transition-transform duration-300 ease-in-out md:hidden',
          mobileOpen ? 'translate-x-0' : 'translate-x-full',
        )}
      >
        <div className="mb-8 flex items-center justify-between">
          <span className="font-display text-lg font-bold text-text">{brandName}</span>
          <button
            type="button"
            aria-label="Close menu"
            className="rounded-md p-2 text-text-secondary hover:text-text"
            onClick={closeMobile}
          >
            <Icon name="X" className="h-5 w-5" />
          </button>
        </div>

        <ul className="flex flex-col gap-1">
          {navLinks.map((link) => (
            <li key={link.href}>
              <a
                href={link.href}
                onClick={closeMobile}
                className={cn(
                  'block rounded-md px-3 py-3 text-base font-medium transition-colors duration-200',
                  isActive(link.href)
                    ? 'bg-brand-blue/10 text-brand-blue'
                    : 'text-text-secondary hover:bg-bg-elevated hover:text-text',
                )}
              >
                {link.label}
              </a>
            </li>
          ))}
        </ul>

        <div className="mt-auto pt-6">
          <Button href={ctaHref} size="md" className="w-full">
            {ctaLabel}
          </Button>
        </div>
      </aside>
    </>
  );
}
