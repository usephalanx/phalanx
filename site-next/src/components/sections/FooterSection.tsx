// ---------------------------------------------------------------------------
// FooterSection — Site footer with logo, tagline, link columns, and copyright.
// Dark slate-900 background with white/gray text. Responsive grid layout.
// ---------------------------------------------------------------------------

import React from 'react';

import Container from '@/components/ui/Container';
import type { FooterLinkColumn, SocialLink } from '@/data/content';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

/** Props for the {@link FooterSection} component. */
export interface FooterSectionProps {
  /** Brand / product name displayed next to the logo mark. */
  brandName: string;
  /** Short tagline rendered below the brand name. */
  tagline: string;
  /** Columns of navigation links (e.g. Product, Company, Legal). */
  linkColumns: FooterLinkColumn[];
  /** Social / external links rendered as icon-style links. */
  socialLinks?: SocialLink[];
  /** Copyright notice (should include year). */
  copyright: string;
  /** Optional additional CSS classes for the outer footer element. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Full-width site footer with a dark slate-900 background.
 *
 * Layout (responsive):
 * - **Mobile**: single column — brand block, then link columns stacked, then copyright.
 * - **md+**: grid with brand block on the left and link columns on the right.
 *
 * All content is provided via props — no hardcoded strings.
 */
export default function FooterSection({
  brandName,
  tagline,
  linkColumns,
  socialLinks,
  copyright,
  className,
}: FooterSectionProps): React.JSX.Element {
  return (
    <footer
      className={className}
      aria-label="Site footer"
    >
      <div className="bg-slate-900">
        <Container className="py-16">
          {/* ---- Top grid: brand + link columns ---- */}
          <div className="grid gap-12 md:grid-cols-2 lg:grid-cols-4">
            {/* Brand block */}
            <div className="lg:col-span-1">
              {/* Logo mark + name */}
              <div className="flex items-center gap-2">
                <div
                  className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-600"
                  aria-hidden="true"
                >
                  <span className="text-sm font-bold text-white">P</span>
                </div>
                <span className="text-lg font-semibold text-white">
                  {brandName}
                </span>
              </div>

              <p className="mt-4 max-w-xs text-sm leading-relaxed text-gray-400">
                {tagline}
              </p>

              {/* Social links */}
              {socialLinks && socialLinks.length > 0 && (
                <div className="mt-6 flex gap-4">
                  {socialLinks.map((link) => (
                    <a
                      key={link.platform}
                      href={link.href}
                      aria-label={link.platform}
                      className="text-gray-400 transition-colors hover:text-white"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {link.platform}
                    </a>
                  ))}
                </div>
              )}
            </div>

            {/* Link columns */}
            {linkColumns.map((column) => (
              <div key={column.title}>
                <h3 className="text-sm font-semibold uppercase tracking-wider text-white">
                  {column.title}
                </h3>
                <ul className="mt-4 space-y-3" role="list">
                  {column.links.map((link) => (
                    <li key={link.label}>
                      <a
                        href={link.href}
                        className="text-sm text-gray-400 transition-colors hover:text-white"
                      >
                        {link.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>

          {/* ---- Divider ---- */}
          <div className="mt-12 border-t border-slate-800" />

          {/* ---- Bottom: copyright ---- */}
          <div className="mt-8 text-center text-sm text-gray-500">
            {copyright}
          </div>
        </Container>
      </div>
    </footer>
  );
}
