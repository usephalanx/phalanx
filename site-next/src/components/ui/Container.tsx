// ---------------------------------------------------------------------------
// Container — max-width centered padding wrapper used by every section.
// ---------------------------------------------------------------------------

import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

/** Props for the {@link Container} component. */
export interface ContainerProps {
  /** Child elements to render inside the container. */
  children: ReactNode;
  /** Additional CSS classes merged with the base styles. */
  className?: string;
}

/**
 * A centered, max-width constrained wrapper that provides consistent
 * horizontal padding across all page sections.
 */
function Container({ children, className }: ContainerProps) {
  return (
    <div className={cn('max-w-content mx-auto px-6', className)}>
      {children}
    </div>
  );
}

export { Container };
export default Container;
