// ---------------------------------------------------------------------------
// Footer — site-wide footer with branding, columnar navigation links,
// social media icons, and a copyright line.
// ---------------------------------------------------------------------------

import type { ReactNode } from 'react';

import type { NavLink, SocialLink } from '@/data/content';
import { cn } from '@/lib/cn';

import { Container } from '@/components/ui/Container';
import Icon from '@/components/ui/Icon';

/** A column of links displayed in the footer navigation area. */
export interface FooterLinkColumn {
  /** Column heading (e.g. "Product", "Company", "Legal"). */
  title: string;
  /** Links within the column. */
  links: NavLink[];
}

/** Props for the {@link Footer} component. */
export interface FooterProps {
  /** Brand name displayed in the footer header. */
  brandName: string;
  /** Short tagline displayed below the brand name. */
  tagline: string;
  /** Grouped navigation links organised by column. */
  columns: FooterLinkColumn[];
  /** Social media icon links. */
  socialLinks: SocialLink[];
  /** Copyright text displayed at the bottom. */
  copyright: string;
  /** Additional CSS classes merged with the root element. */
  className?: string;
}

/**
 * Site-wide footer with Phalanx branding, columnar navigation links,
 * social media icon links, and a copyright line.
 *
 * Dark background with a subtle `border-top` separator. Responsive:
 * columns stack vertically on mobile and lay out horizontally on larger
 * viewports.
 */
export default function Footer({
  brandName,
  tagline,
  columns,
  socialLinks,
  copyright,
  className,
}: FooterProps) {
  return (
    <footer
      data-testid="footer"
      className={cn(
        'border-t border-border/50 bg-bg-card pt-16 pb-8',
        className,
      )}
    >
      <Container>
        {/* Top section: brand + link columns */}
        <div className="grid grid-cols-1 gap-12 md:grid-cols-12">
          {/* Brand & tagline */}
          <div className="md:col-span-4">
            <div className="flex items-center gap-2 font-display text-lg font-bold text-text">
              <Icon name="ShieldCheck" className="h-6 w-6 text-brand-blue" />
              {brandName}
            </div>
            <p className="mt-3 max-w-xs text-sm leading-relaxed text-text-secondary">
              {tagline}
            </p>

            {/* Social links — below tagline on all viewports */}
            {socialLinks.length > 0 && (
              <div className="mt-6 flex items-center gap-4">
                {socialLinks.map((social) => (
                  <a
                    key={social.platform}
                    href={social.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={social.platform}
                    className="text-text-secondary transition-colors duration-200 hover:text-text"
                  >
                    <Icon name={social.icon} className="h-5 w-5" />
                  </a>
                ))}
              </div>
            )}
          </div>

          {/* Link columns */}
          <div className="grid grid-cols-2 gap-8 sm:grid-cols-3 md:col-span-8">
            {columns.map((column) => (
              <div key={column.title}>
                <h3 className="text-sm font-semibold uppercase tracking-wider text-text">
                  {column.title}
                </h3>
                <ul className="mt-4 space-y-3">
                  {column.links.map((link) => (
                    <li key={link.href}>
                      <a
                        href={link.href}
                        className="text-sm text-text-secondary transition-colors duration-200 hover:text-text"
                      >
                        {link.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>

        {/* Divider */}
        <div className="mt-12 border-t border-border/30" />

        {/* Copyright */}
        <p className="mt-6 text-center text-xs text-text-secondary">
          {copyright}
        </p>
      </Container>
    </footer>
  );
}
