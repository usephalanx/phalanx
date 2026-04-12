import type { Config } from 'tailwindcss';
import defaultTheme from 'tailwindcss/defaultTheme';
import typography from '@tailwindcss/typography';

/** Tailwind configuration for the Phalanx marketing site. */
const config: Config = {
  content: [
    './src/**/*.{ts,tsx,mdx}',
  ],
  theme: {
    screens: {
      sm: '600px',
      md: '900px',
      lg: '1100px',
    },
    container: {
      center: true,
      padding: {
        DEFAULT: '1rem',
        sm: '1.5rem',
        lg: '2rem',
      },
    },
    extend: {
      colors: {
        primary: {
          DEFAULT: '#4F46E5',
          50: '#EEF2FF',
          100: '#E0E7FF',
          200: '#C7D2FE',
          300: '#A5B4FC',
          400: '#818CF8',
          500: '#6366F1',
          600: '#4F46E5',
          700: '#4338CA',
          800: '#3730A3',
          900: '#312E81',
          950: '#1E1B4B',
        },
        accent: {
          DEFAULT: '#8B5CF6',
          50: '#F5F3FF',
          100: '#EDE9FE',
          200: '#DDD6FE',
          300: '#C4B5FD',
          400: '#A78BFA',
          500: '#8B5CF6',
          600: '#7C3AED',
          700: '#6D28D9',
          800: '#5B21B6',
          900: '#4C1D95',
          950: '#2E1065',
        },
        bg: {
          DEFAULT: '#050608',
          elevated: '#0B0D14',
          card: '#0F111A',
          'card-hover': '#141725',
        },
        border: {
          DEFAULT: '#1A1D2E',
          hover: '#272B45',
        },
        text: {
          DEFAULT: '#E4E8F1',
          secondary: '#8891AB',
          muted: '#555E78',
        },
        brand: {
          blue: '#7AA2F7',
          'blue-dim': 'rgba(122,162,247,0.12)',
          amber: '#D4A853',
          'amber-dim': 'rgba(212,168,83,0.10)',
        },
        agent: {
          cmd: '#7AA2F7',
          plan: '#BB9AF7',
          build: '#9ECE6A',
          review: '#E0AF68',
          qa: '#73DACA',
          sec: '#F7768E',
          rel: '#FF9E64',
        },
      },
      fontFamily: {
        sans: [
          'var(--font-inter)',
          'Geist Sans',
          ...defaultTheme.fontFamily.sans,
        ],
        display: [
          'var(--font-space-grotesk)',
          'var(--font-inter)',
          'Geist Sans',
          ...defaultTheme.fontFamily.sans,
        ],
        mono: ['var(--font-jetbrains)', 'Geist Mono', 'Menlo', 'monospace'],
      },
      maxWidth: {
        content: '1100px',
      },
      spacing: {
        section: '120px',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'slide-up': {
          '0%': { opacity: '0', transform: 'translateY(20px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.6s ease-out forwards',
        'slide-up': 'slide-up 0.6s ease-out forwards',
      },
    },
  },
  plugins: [typography],
};

export default config;
