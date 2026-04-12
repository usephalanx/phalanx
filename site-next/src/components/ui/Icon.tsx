// ---------------------------------------------------------------------------
// Icon — dynamic Lucide icon renderer by string name.
// Maps a string identifier to a Lucide React component so data files can
// reference icons without importing React components directly.
// ---------------------------------------------------------------------------

import type { ComponentType } from 'react';
import {
  BrainCircuit,
  Check,
  ChevronDown,
  FlaskConical,
  GitPullRequestArrow,
  Github,
  Hammer,
  ListChecks,
  Menu,
  MessageCircle,
  Rocket,
  ShieldCheck,
  Terminal,
  Twitter,
  Users,
  Workflow,
  X,
} from 'lucide-react';

import { cn } from '@/lib/cn';

/** Props for the {@link Icon} component. */
export interface IconProps {
  /** String key matching a name in the icon map. */
  name: string;
  /** Additional CSS classes applied to the SVG element. */
  className?: string;
}

/** Map of string identifiers → Lucide icon components. */
const iconMap: Record<string, ComponentType<{ className?: string }>> = {
  BrainCircuit,
  Check,
  ChevronDown,
  FlaskConical,
  GitPullRequestArrow,
  Github,
  Hammer,
  ListChecks,
  Menu,
  MessageCircle,
  Rocket,
  ShieldCheck,
  Terminal,
  Twitter,
  Users,
  Workflow,
  X,
};

/**
 * Renders a Lucide icon by its string name.
 *
 * Returns `null` if the name is not found in the icon map, ensuring
 * unknown icon names degrade gracefully rather than throwing.
 */
export default function Icon({ name, className }: IconProps) {
  const LucideIcon = iconMap[name];

  if (!LucideIcon) {
    return null;
  }

  return <LucideIcon className={cn('shrink-0', className)} />;
}
