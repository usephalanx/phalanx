// ---------------------------------------------------------------------------
// Application-wide constants (section IDs, external URLs).
// All content data lives in @/data/content — import from there instead.
// ---------------------------------------------------------------------------

/* ------------------------------------------------------------------ */
/*  Section IDs                                                       */
/* ------------------------------------------------------------------ */

/**
 * Mapping of logical section names to their DOM element IDs.
 * Used by scroll-spy hooks and nav link highlighting.
 */
export const SECTION_IDS = {
  hero: 'hero',
  features: 'features',
  howItWorks: 'how-it-works',
  pricing: 'pricing',
  faq: 'faq',
  cta: 'cta',
} as const;

/* ------------------------------------------------------------------ */
/*  External URLs                                                     */
/* ------------------------------------------------------------------ */

/** Primary GitHub repository URL. */
export const GITHUB_URL = 'https://github.com/usephalanx/phalanx';

/** Documentation / README URL. */
export const DOCS_URL = 'https://github.com/usephalanx/phalanx#readme';

/** Waitlist / signup URL. */
export const WAITLIST_URL = '/signup';
