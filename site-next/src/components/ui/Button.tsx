'use client';

// ---------------------------------------------------------------------------
// Button — variant-driven button/link component for CTAs.
// Renders as <a> when `href` is provided, <button> otherwise.
// ---------------------------------------------------------------------------

import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/cn';

/** Visual style variant. */
export type ButtonVariant = 'primary' | 'secondary' | 'ghost';

/** Size preset. */
export type ButtonSize = 'sm' | 'md' | 'lg';

/** Props for the {@link Button} component. */
export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  /** Child content (label text, icon, etc.). */
  children: ReactNode;
  /** Visual variant — defaults to `'primary'`. */
  variant?: ButtonVariant;
  /** Size preset — defaults to `'md'`. */
  size?: ButtonSize;
  /** If provided, renders an anchor (`<a>`) instead of `<button>`. */
  href?: string;
  /** Click handler (only applicable when rendering as `<button>`). */
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  /** Additional CSS classes merged with variant styles. */
  className?: string;
}

/** Base classes shared by every variant. */
const baseClasses =
  'inline-flex items-center justify-center rounded-lg font-semibold transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 focus-visible:ring-offset-2 focus-visible:ring-offset-bg';

/** Variant-specific class maps. */
const variantClasses: Record<ButtonVariant, string> = {
  primary:
    'bg-primary-600 text-white hover:bg-primary-700 hover:scale-[1.02] active:scale-[0.98] shadow-lg shadow-primary-600/20',
  secondary:
    'bg-transparent border border-primary-400 text-primary-300 hover:bg-primary-600 hover:text-white hover:border-primary-600',
  ghost:
    'bg-transparent text-text-secondary hover:text-text hover:bg-bg-elevated underline-offset-4',
};

/** Size-specific class maps. */
const sizeClasses: Record<ButtonSize, string> = {
  sm: 'text-sm px-4 py-2 gap-1.5',
  md: 'text-base px-6 py-3 gap-2',
  lg: 'text-lg px-8 py-4 gap-2.5',
};

/**
 * A polymorphic button component supporting three visual variants
 * (`primary`, `secondary`, `ghost`) and three size presets.
 *
 * When an `href` prop is supplied, the component renders as an `<a>` tag
 * for proper link semantics. Otherwise it renders as a `<button>`.
 */
export default function Button({
  children,
  variant = 'primary',
  size = 'md',
  href,
  className,
  ...rest
}: ButtonProps) {
  const classes = cn(
    baseClasses,
    variantClasses[variant],
    sizeClasses[size],
    className,
  );

  if (href) {
    return (
      <a href={href} className={classes}>
        {children}
      </a>
    );
  }

  return (
    <button className={classes} {...rest}>
      {children}
    </button>
  );
}
