import { renderHook, act } from '@testing-library/react';

import { useActiveSection } from '@/lib/hooks/useActiveSection';
import { SECTION_IDS } from '@/lib/constants';

/* ------------------------------------------------------------------ */
/*  IntersectionObserver mock                                          */
/* ------------------------------------------------------------------ */

type IntersectionCallback = (entries: Partial<IntersectionObserverEntry>[]) => void;

let observerCallback: IntersectionCallback;
let observedElements: Element[] = [];

const mockDisconnect = jest.fn();
const mockObserve = jest.fn((el: Element) => {
  observedElements.push(el);
});

beforeEach(() => {
  observedElements = [];
  mockDisconnect.mockClear();
  mockObserve.mockClear();

  (global as unknown as Record<string, unknown>).IntersectionObserver = jest.fn(
    (callback: IntersectionCallback) => {
      observerCallback = callback;
      return {
        observe: mockObserve,
        unobserve: jest.fn(),
        disconnect: mockDisconnect,
      };
    },
  );
});

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

describe('useActiveSection', () => {
  it('returns "hero" as the default active section', () => {
    const { result } = renderHook(() => useActiveSection());
    expect(result.current).toBe('hero');
  });

  it('creates an IntersectionObserver on mount', () => {
    // Add DOM elements for sections
    for (const id of Object.values(SECTION_IDS)) {
      const el = document.createElement('div');
      el.id = id;
      document.body.appendChild(el);
    }

    renderHook(() => useActiveSection());
    expect(global.IntersectionObserver).toHaveBeenCalledTimes(1);

    // Cleanup
    for (const id of Object.values(SECTION_IDS)) {
      const el = document.getElementById(id);
      if (el) document.body.removeChild(el);
    }
  });

  it('updates active section when an entry intersects', () => {
    const el = document.createElement('div');
    el.id = 'features';
    document.body.appendChild(el);

    const { result } = renderHook(() => useActiveSection());

    act(() => {
      observerCallback([{ isIntersecting: true, target: el }]);
    });

    expect(result.current).toBe('features');

    document.body.removeChild(el);
  });

  it('disconnects the observer on unmount', () => {
    const el = document.createElement('div');
    el.id = 'hero';
    document.body.appendChild(el);

    const { unmount } = renderHook(() => useActiveSection());
    unmount();

    expect(mockDisconnect).toHaveBeenCalledTimes(1);

    document.body.removeChild(el);
  });
});
