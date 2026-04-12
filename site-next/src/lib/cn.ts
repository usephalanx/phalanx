// ---------------------------------------------------------------------------
// Tailwind class-name merge utility.
// Wraps clsx for conditional class composition.
// ---------------------------------------------------------------------------

import { type ClassValue, clsx } from 'clsx';

/**
 * Merge and deduplicate Tailwind CSS class names.
 *
 * Accepts any combination of strings, arrays, and objects supported by `clsx`.
 * Returns a single space-separated class string.
 */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
