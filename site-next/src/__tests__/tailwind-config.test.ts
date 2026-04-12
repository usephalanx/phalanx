import config from '../../tailwind.config';

describe('tailwind.config', () => {
  const extend = config.theme?.extend;

  describe('colors', () => {
    it('defines primary color palette with indigo-600 as default', () => {
      const colors = extend?.colors as Record<string, Record<string, string>>;
      expect(colors.primary.DEFAULT).toBe('#4F46E5');
      expect(colors.primary[600]).toBe('#4F46E5');
    });

    it('defines accent color palette with violet-500 as default', () => {
      const colors = extend?.colors as Record<string, Record<string, string>>;
      expect(colors.accent.DEFAULT).toBe('#8B5CF6');
      expect(colors.accent[500]).toBe('#8B5CF6');
    });

    it('defines brand blue and amber', () => {
      const colors = extend?.colors as Record<string, Record<string, string>>;
      expect(colors.brand.blue).toBe('#7AA2F7');
      expect(colors.brand.amber).toBe('#D4A853');
    });

    it('defines dark background colors', () => {
      const colors = extend?.colors as Record<string, Record<string, string>>;
      expect(colors.bg.DEFAULT).toBe('#050608');
      expect(colors.bg.elevated).toBe('#0B0D14');
      expect(colors.bg.card).toBe('#0F111A');
    });

    it('defines all seven agent colors', () => {
      const colors = extend?.colors as Record<string, Record<string, string>>;
      const agentKeys = Object.keys(colors.agent);
      expect(agentKeys).toEqual(
        expect.arrayContaining(['cmd', 'plan', 'build', 'review', 'qa', 'sec', 'rel']),
      );
      expect(agentKeys).toHaveLength(7);
    });
  });

  describe('fontFamily', () => {
    it('defines sans, display, and mono families', () => {
      const fontFamily = extend?.fontFamily as Record<string, string[]>;
      expect(fontFamily.sans[0]).toBe('var(--font-inter)');
      expect(fontFamily.display[0]).toBe('var(--font-space-grotesk)');
      expect(fontFamily.mono[0]).toBe('var(--font-jetbrains)');
    });

    it('includes Geist Sans as fallback in sans stack', () => {
      const fontFamily = extend?.fontFamily as Record<string, string[]>;
      expect(fontFamily.sans).toContain('Geist Sans');
    });

    it('includes Geist Mono as fallback in mono stack', () => {
      const fontFamily = extend?.fontFamily as Record<string, string[]>;
      expect(fontFamily.mono).toContain('Geist Mono');
    });
  });

  describe('container', () => {
    it('is centered', () => {
      const container = config.theme?.container as Record<string, unknown>;
      expect(container.center).toBe(true);
    });

    it('has responsive padding', () => {
      const container = config.theme?.container as Record<string, unknown>;
      const padding = container.padding as Record<string, string>;
      expect(padding.DEFAULT).toBe('1rem');
      expect(padding.sm).toBe('1.5rem');
      expect(padding.lg).toBe('2rem');
    });
  });

  describe('animations', () => {
    it('defines fade-in keyframes', () => {
      const keyframes = extend?.keyframes as Record<string, Record<string, Record<string, string>>>;
      expect(keyframes['fade-in']).toBeDefined();
      expect(keyframes['fade-in']['0%'].opacity).toBe('0');
      expect(keyframes['fade-in']['100%'].opacity).toBe('1');
    });

    it('defines slide-up keyframes', () => {
      const keyframes = extend?.keyframes as Record<string, Record<string, Record<string, string>>>;
      expect(keyframes['slide-up']).toBeDefined();
      expect(keyframes['slide-up']['0%'].transform).toBe('translateY(20px)');
      expect(keyframes['slide-up']['100%'].transform).toBe('translateY(0)');
    });

    it('defines animation shorthand values', () => {
      const animation = extend?.animation as Record<string, string>;
      expect(animation['fade-in']).toContain('fade-in');
      expect(animation['slide-up']).toContain('slide-up');
    });
  });

  describe('content paths', () => {
    it('scans src directory for ts, tsx, and mdx files', () => {
      expect(config.content).toContain('./src/**/*.{ts,tsx,mdx}');
    });
  });

  describe('screens', () => {
    it('defines sm, md, lg breakpoints', () => {
      const screens = config.theme?.screens as Record<string, string>;
      expect(screens.sm).toBe('600px');
      expect(screens.md).toBe('900px');
      expect(screens.lg).toBe('1100px');
    });
  });
});
