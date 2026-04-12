// ---------------------------------------------------------------------------
// constants.ts — unit tests for section IDs and external URLs.
// ---------------------------------------------------------------------------

import { SECTION_IDS, GITHUB_URL, DOCS_URL, WAITLIST_URL } from './constants';

describe('SECTION_IDS', () => {
  it('contains all expected section keys', () => {
    expect(SECTION_IDS).toHaveProperty('hero', 'hero');
    expect(SECTION_IDS).toHaveProperty('features', 'features');
    expect(SECTION_IDS).toHaveProperty('howItWorks', 'how-it-works');
    expect(SECTION_IDS).toHaveProperty('pricing', 'pricing');
    expect(SECTION_IDS).toHaveProperty('faq', 'faq');
    expect(SECTION_IDS).toHaveProperty('cta', 'cta');
  });

  it('values are all non-empty strings', () => {
    Object.values(SECTION_IDS).forEach((id) => {
      expect(typeof id).toBe('string');
      expect(id.length).toBeGreaterThan(0);
    });
  });
});

describe('external URLs', () => {
  it('GITHUB_URL is a valid URL string', () => {
    expect(GITHUB_URL).toMatch(/^https:\/\//);
  });

  it('DOCS_URL is defined', () => {
    expect(typeof DOCS_URL).toBe('string');
    expect(DOCS_URL.length).toBeGreaterThan(0);
  });

  it('WAITLIST_URL is defined', () => {
    expect(typeof WAITLIST_URL).toBe('string');
    expect(WAITLIST_URL.length).toBeGreaterThan(0);
  });
});
