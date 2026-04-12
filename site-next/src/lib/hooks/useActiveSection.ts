'use client';

// ---------------------------------------------------------------------------
// Scroll-spy hook — tracks which page section is currently in view.
// Uses IntersectionObserver for performant, passive scroll detection.
// ---------------------------------------------------------------------------

import { useEffect, useState } from 'react';

import { SECTION_IDS } from '@/lib/constants';

/**
 * Returns the ID of the page section currently intersecting the viewport.
 *
 * The observer uses a `rootMargin` of `-40% 0px -55% 0px` so that the
 * "active" section corresponds roughly to the top-center of the screen —
 * matching the navbar's visual position.
 *
 * Falls back to `'hero'` when no section is observed.
 */
export function useActiveSection(): string {
  const [activeSection, setActiveSection] = useState<string>('hero');

  useEffect(() => {
    const ids = Object.values(SECTION_IDS);
    const elements = ids
      .map((id) => document.getElementById(id))
      .filter(Boolean) as HTMLElement[];

    if (elements.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setActiveSection(entry.target.id);
          }
        }
      },
      { rootMargin: '-40% 0px -55% 0px' },
    );

    for (const el of elements) {
      observer.observe(el);
    }

    return () => {
      observer.disconnect();
    };
  }, []);

  return activeSection;
}
