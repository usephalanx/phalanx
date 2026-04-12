'use client';

// ---------------------------------------------------------------------------
// TerminalDemo — animated terminal showing the /phalanx build → agent flow.
// Client component using useEffect + useState for typewriter animation.
// ---------------------------------------------------------------------------

import React, { useEffect, useState, useCallback } from 'react';

import { cn } from '@/lib/cn';

/** Props for the {@link TerminalDemo} component. */
export interface TerminalDemoProps {
  /** Additional CSS classes for positioning. */
  className?: string;
}

/** A single agent status line in the terminal output. */
interface AgentLine {
  /** Agent display name. */
  label: string;
  /** Tailwind background colour class for the status dot. */
  colorClass: string;
}

/** The command typed into the terminal. */
const COMMAND = '/phalanx build Add user auth';

/** Agent status lines shown sequentially after the command. */
const AGENT_LINES: AgentLine[] = [
  { label: 'Planner', colorClass: 'bg-agent-plan' },
  { label: 'Builder', colorClass: 'bg-agent-build' },
  { label: 'Reviewer', colorClass: 'bg-agent-review' },
  { label: 'QA', colorClass: 'bg-agent-qa' },
  { label: 'Security', colorClass: 'bg-agent-sec' },
  { label: 'Released', colorClass: 'bg-agent-rel' },
];

/** Delay (ms) between each typed character. */
const TYPE_DELAY = 40;

/** Delay (ms) between each agent line appearing. */
const LINE_DELAY = 500;

/** Total cycle duration (ms) before the animation resets. */
const RESET_DELAY = 8000;

/**
 * Animated terminal window showing the /phalanx build pipeline flow.
 *
 * Renders a fake macOS-style terminal with a typewriter effect for the
 * command input, followed by sequential agent status lines. The animation
 * loops on an 8-second cycle.
 */
export function TerminalDemo({ className }: TerminalDemoProps): React.JSX.Element {
  const [typedChars, setTypedChars] = useState(0);
  const [visibleLines, setVisibleLines] = useState(0);
  const [cycle, setCycle] = useState(0);

  /** Reset all animation state for a new cycle. */
  const reset = useCallback(() => {
    setTypedChars(0);
    setVisibleLines(0);
    setCycle((c) => c + 1);
  }, []);

  // Typewriter effect — type one character at a time.
  useEffect(() => {
    if (typedChars >= COMMAND.length) return;

    const timer = setTimeout(() => {
      setTypedChars((n) => n + 1);
    }, TYPE_DELAY);

    return () => clearTimeout(timer);
  }, [typedChars, cycle]);

  // Agent lines — reveal one line at a time after typing completes.
  useEffect(() => {
    if (typedChars < COMMAND.length) return;
    if (visibleLines >= AGENT_LINES.length) return;

    const timer = setTimeout(() => {
      setVisibleLines((n) => n + 1);
    }, LINE_DELAY);

    return () => clearTimeout(timer);
  }, [typedChars, visibleLines, cycle]);

  // Reset cycle after all lines are shown.
  useEffect(() => {
    if (visibleLines < AGENT_LINES.length) return;

    const timer = setTimeout(reset, RESET_DELAY);

    return () => clearTimeout(timer);
  }, [visibleLines, reset, cycle]);

  return (
    <div
      className={cn(
        'overflow-hidden rounded-xl border border-border bg-bg-elevated shadow-2xl',
        className,
      )}
    >
      {/* Title bar */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <span className="h-3 w-3 rounded-full bg-[#FF5F56]" />
        <span className="h-3 w-3 rounded-full bg-[#FFBD2E]" />
        <span className="h-3 w-3 rounded-full bg-[#27C93F]" />
        <span className="ml-3 text-xs text-text-muted">phalanx — zsh</span>
      </div>

      {/* Terminal body */}
      <div className="p-4 font-mono text-sm leading-relaxed">
        {/* Command line */}
        <div className="flex items-center gap-2">
          <span className="select-none text-text-muted">$</span>
          <span className="text-brand-blue">
            {COMMAND.slice(0, typedChars)}
            {typedChars < COMMAND.length && (
              <span className="ml-px inline-block h-4 w-1.5 animate-pulse bg-brand-blue" />
            )}
          </span>
        </div>

        {/* Agent status lines */}
        {typedChars >= COMMAND.length && (
          <div className="mt-3 space-y-1.5">
            {AGENT_LINES.slice(0, visibleLines).map((line) => (
              <div key={line.label} className="flex items-center gap-2 animate-fade-in">
                <span className={cn('h-2 w-2 rounded-full', line.colorClass)} />
                <span className="text-text-secondary">{line.label}</span>
                <span className="text-agent-build">✓</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default TerminalDemo;
