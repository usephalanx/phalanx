# Maddie Real Estate Site — Post-Deploy Fixes

**Run:** `0c8be8bf` | **Branch:** `phalanx/run-0c8be8bf` | **Date:** 2026-04-07
**Demo:** `https://demo.usephalanx.com/build-a-stunning-one-page-real-estate-agent-website-for-madd/`

---

## Root Cause Summary

The builder (Claude) split output across two directories:
- `src/` — `App.tsx`, `main.tsx` (root entry points)
- `frontend/src/components/` — all actual components

`App.tsx` imported from `./components/Header` etc., but `src/components/` **didn't exist**. Vite resolved these as `undefined` modules and built successfully (no hard error), producing a bundle that crashed at runtime with `TypeError: Cannot read properties of undefined (reading 'map')`.

Additionally, all components were designed to accept props from a parent data layer, but `App.tsx` called every component with **zero props** and no component had default values — so all rendered as blank/crashed.

---

## Fixes Applied (in order)

### Fix 1 — Copy components into correct location
**Problem:** `src/components/` missing; all imports resolved to `undefined`
**Fix:** Copied `frontend/src/components/` → `src/components/` (26 files)
**Commit:** `92ac1a4`

### Fix 2 — Header: add default `navLinks` prop
**Problem:** `navLinks.map()` on `undefined` (required prop, no default)
**Fix:** Added defaults to destructure:
```tsx
navLinks = [
  { label: 'About', href: '#about' },
  { label: 'Recent Sales', href: '#recent-sales' },
  { label: 'Contact', href: '#contact' },
],
logoText = 'Maddie',
ctaText = 'Contact Maddie',
ctaHref = '#contact',
```
**Commit:** `a83c7bc`

### Fix 3 — About: add default `trustBadges` prop
**Problem:** `trustBadges.map()` on `undefined` (required prop, no default)
**Fix:** Added default array to destructure:
```tsx
trustBadges = [
  { icon: '🏆', label: 'Licensed Agent' },
  { icon: '🏠', label: '200+ Homes Sold' },
  { icon: '⭐', label: '5★ Rated' },
],
```
**Commit:** `a83c7bc`

### Fix 4 — Hero: add defaults for all required props
**Problem:** All 7 props required with no defaults → `headline`, `subheading`, `backgroundImageUrl` all `undefined` → blank hero section
**Fix:** Added defaults:
```tsx
headline = 'Your Dream Home Starts Here',
subheading = "Expert real estate guidance with a personal touch...",
backgroundImageUrl = 'https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=1600&q=80',
primaryCtaLabel = 'View My Listings',
primaryCtaHref = '#recent-sales',
secondaryCtaLabel = 'Get In Touch',
secondaryCtaHref = '#contact',
```
**Commit:** `1917339`

### Fix 5 — About: add defaults for `bioText` and `avatarImageUrl`
**Problem:** Bio text and headshot were `undefined` → about section showed only avatar placeholder + badges
**Fix:** Added defaults:
```tsx
bioText = 'With over 10 years in luxury residential real estate...',
avatarImageUrl = 'https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=400&q=80',
```
**Commit:** `1917339`

### Fix 6 — Button: `label` prop → `children`
**Problem:** `ContactForm` called `<Button label={submitButtonText} />` but `Button` component renders `{children}`, not a `label` prop → submit button was gold rectangle with no text
**Fix:**
```tsx
// Before
<Button label={submitButtonText} variant="primary" type="submit" />

// After
<Button variant="primary" type="submit">{submitButtonText}</Button>
```
**Commit:** `c08cf0b`

---

## SRE Pipeline Fixes (same session)

These are infrastructure-level bugs found and fixed during the same deploy session:

| Bug | Fix |
|-----|-----|
| `MissingGreenlet` crash in Commander when accessing `wo.title` after `session.commit()` | Capture `wo_title = str(wo.title)` immediately after load, before any commits expire the ORM object |
| Health check always fails for `python:3.12-slim` containers (no `curl`) | Added `python3 -c "urllib.request.urlopen(...)"` as fallback in `_health_check()` |

---

## Brainstorm Notes (for tomorrow)

### Issues to address in v2 build
1. **Component/data architecture** — builder should either use a single-file component pattern or a dedicated `src/data/` constants file that App.tsx populates and passes as props. The two-directory split is a recurring failure mode.
2. **Required props without defaults** — any component that uses `.map()` must have a default array. Builder prompt should enforce this.
3. **Button `label` vs `children`** — builder inconsistently uses prop names. Should standardize on React convention (`children` for content).
4. **Tailwind custom colors** — `gold-500`, `gold-600` undefined (tailwind config has `gold.DEFAULT` not numbered variants). Button uses `bg-gold` not `bg-gold-500`.
5. **Hero image loading** — no fallback if Unsplash URL fails (CORS or rate limit).
6. **About headshot** — using a generic Unsplash stock photo. Should use a styled SVG avatar placeholder by default that's clearly a placeholder, not a random person.

### Feature improvements for Maddie's site
- Real contact form submission (EmailJS or Formspree)
- Lightbox for property photos
- Add "Testimonials" section
- Google Maps embed for service area
- Add Maddie's real phone/email/Instagram
- Mobile hamburger menu animation polish
- SEO meta tags (og:image, structured data for LocalBusiness)
