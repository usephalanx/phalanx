/**
 * Tests verifying the Jest testing infrastructure is correctly configured.
 *
 * Covers:
 * - jest-dom matchers loaded via jest.setup.ts
 * - CSS import mock resolves to an empty object
 * - Image/file import mock resolves to a stub string
 * - @/ path alias resolves correctly
 */

import styleMock from '@/__mocks__/styleMock';
import fileMock from '@/__mocks__/fileMock';

/* ------------------------------------------------------------------ */
/*  jest-dom matchers                                                  */
/* ------------------------------------------------------------------ */

describe('jest-dom matchers', () => {
  it('toBeInTheDocument is available', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    expect(div).toBeInTheDocument();
    document.body.removeChild(div);
  });

  it('toHaveClass is available', () => {
    const span = document.createElement('span');
    span.className = 'active';
    document.body.appendChild(span);
    expect(span).toHaveClass('active');
    document.body.removeChild(span);
  });

  it('toBeVisible is available', () => {
    const p = document.createElement('p');
    document.body.appendChild(p);
    expect(p).toBeVisible();
    document.body.removeChild(p);
  });
});

/* ------------------------------------------------------------------ */
/*  CSS mock                                                           */
/* ------------------------------------------------------------------ */

describe('CSS import mock', () => {
  it('resolves to an empty object', () => {
    expect(styleMock).toEqual({});
  });

  it('is a plain object (not null/undefined)', () => {
    expect(typeof styleMock).toBe('object');
    expect(styleMock).not.toBeNull();
  });
});

/* ------------------------------------------------------------------ */
/*  File / image mock                                                  */
/* ------------------------------------------------------------------ */

describe('file import mock', () => {
  it('resolves to a string stub', () => {
    expect(typeof fileMock).toBe('string');
  });

  it('returns the expected placeholder value', () => {
    expect(fileMock).toBe('test-file-stub');
  });
});

/* ------------------------------------------------------------------ */
/*  @/ path alias                                                      */
/* ------------------------------------------------------------------ */

describe('@/ path alias', () => {
  it('resolves @/data/content via the alias', () => {
    /* If the alias is broken, this import throws at load time.
       We additionally verify the shape of an exported array. */
    const { features } = require('@/data/content');
    expect(Array.isArray(features)).toBe(true);
    expect(features.length).toBeGreaterThan(0);
  });

  it('resolves @/lib/constants via the alias', () => {
    const { SECTION_IDS } = require('@/lib/constants');
    expect(SECTION_IDS).toBeDefined();
    expect(SECTION_IDS.hero).toBe('hero');
  });

  it('resolves @/data/content siteConfig via the alias', () => {
    const { siteConfig } = require('@/data/content');
    expect(siteConfig).toBeDefined();
    expect(siteConfig.name).toBe('Phalanx');
  });
});
