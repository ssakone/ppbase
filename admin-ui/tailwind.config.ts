import type { Config } from 'tailwindcss'
import tailwindAnimate from 'tailwindcss-animate'

const config: Config = {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      colors: {
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        field: {
          text: { bg: '#eff6ff', fg: '#1d4ed8' },
          editor: { bg: '#f8fafc', fg: '#475569' },
          number: { bg: '#f5f3ff', fg: '#7c3aed' },
          bool: { bg: '#ecfdf5', fg: '#059669' },
          email: { bg: '#eef2ff', fg: '#4f46e5' },
          url: { bg: '#eef2ff', fg: '#4f46e5' },
          date: { bg: '#fffbeb', fg: '#d97706' },
          select: { bg: '#fdf4ff', fg: '#a855f7' },
          json: { bg: '#f0fdf4', fg: '#16a34a' },
          file: { bg: '#fff7ed', fg: '#ea580c' },
          relation: { bg: '#fef2f2', fg: '#dc2626' },
        },
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
    },
  },
  plugins: [tailwindAnimate],
}

export default config
