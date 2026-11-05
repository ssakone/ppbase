import type { Config } from 'tailwindcss'
import colors from 'tailwindcss/colors'
import plugin from 'tailwindcss/plugin'
import tailwindAnimate from 'tailwindcss-animate'

/*
 * Dark mode for hard-coded palette classes.
 *
 * Most components use palette utilities directly (bg-white, text-slate-600,
 * bg-emerald-50, border-red-200...). Instead of adding a `dark:` variant to
 * each of them, the light-oriented shades are routed through CSS variables
 * whose fallback is the regular Tailwind color, and `.dark` redefines them.
 * Backgrounds, text and borders get separate mappings so e.g. `text-white`
 * on a solid button stays white while `bg-white` becomes a dark surface.
 */

type Rgb = [number, number, number]
type Role = 'bg' | 'text' | 'border'
type Shade = '50' | '100' | '200' | '300' | '400' | '500' | '600' | '700' | '800' | '900' | '950'
type Palette = Record<Shade, string>

const hexToRgb = (hex: string): Rgb => {
  let digits = hex.replace('#', '')
  if (digits.length === 3) digits = digits.replace(/./g, '$&$&')
  const n = parseInt(digits, 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}
const mix = (a: Rgb, b: Rgb, weight: number): Rgb =>
  a.map((v, i) => Math.round(v * weight + b[i] * (1 - weight))) as Rgb
const rgb = (c: Rgb) => c.join(' ')

/** Dark card surface (what `bg-white` becomes). */
const DARK_SURFACE = hexToRgb('#0f172a')

const ACCENT_PALETTES = [
  'red', 'orange', 'amber', 'yellow', 'lime', 'green', 'emerald', 'teal', 'cyan',
  'sky', 'blue', 'indigo', 'violet', 'purple', 'fuchsia', 'pink', 'rose',
] as const

const accentDark = (p: Palette): Record<Role, Partial<Record<Shade, Rgb>>> => {
  const c = (shade: Shade) => hexToRgb(p[shade])
  return {
    // Tinted panels become dark tints; solid fills (400+) are unchanged.
    bg: {
      50: mix(c('900'), DARK_SURFACE, 0.35),
      100: mix(c('900'), DARK_SURFACE, 0.55),
      200: mix(c('800'), DARK_SURFACE, 0.65),
      300: mix(c('700'), DARK_SURFACE, 0.8),
    },
    // Dark text on light tints becomes light text on dark tints.
    text: {
      500: c('400'),
      600: c('400'),
      700: c('300'),
      800: c('200'),
      900: c('100'),
      950: c('50'),
    },
    border: {
      50: mix(c('900'), DARK_SURFACE, 0.4),
      100: mix(c('900'), DARK_SURFACE, 0.55),
      200: mix(c('800'), DARK_SURFACE, 0.7),
      300: mix(c('700'), DARK_SURFACE, 0.85),
    },
  }
}

// Neutral scale, tuned by hand: surfaces get darker, text gets lighter.
const NEUTRAL_DARK: Record<Role, Partial<Record<Shade | 'white', Rgb>>> = {
  bg: {
    white: DARK_SURFACE,
    50: hexToRgb('#0a1020'),
    100: hexToRgb('#1a2335'),
    200: hexToRgb('#243047'),
    300: hexToRgb('#334155'),
    400: hexToRgb('#475569'),
  },
  text: {
    300: hexToRgb('#4b5a70'),
    400: hexToRgb('#64748b'),
    500: hexToRgb('#8b9ab0'),
    600: hexToRgb('#a3b0c2'),
    700: hexToRgb('#c4cfdc'),
    800: hexToRgb('#dde4ed'),
    900: hexToRgb('#eef2f7'),
    950: hexToRgb('#f8fafc'),
  },
  border: {
    100: hexToRgb('#1a2335'),
    200: hexToRgb('#222d40'),
    300: hexToRgb('#2e3a50'),
    400: hexToRgb('#3d4a60'),
  },
}

const darkVars: Record<string, string> = {}
const themedColors: Record<Role, Record<string, string | Record<string, string>>> = {
  bg: {},
  text: {},
  border: {},
}

function register(role: Role, name: string, shades: Partial<Record<string, Rgb>>, light: Record<string, string>) {
  for (const [shade, dark] of Object.entries(shades)) {
    if (!dark) continue
    const variable = shade === 'white' ? `--${role}-white` : `--${role}-${name}-${shade}`
    darkVars[variable] = rgb(dark)
    const value = `rgb(var(${variable}, ${rgb(hexToRgb(light[shade]))}) / <alpha-value>)`
    if (shade === 'white') {
      themedColors[role].white = value
    } else {
      const group = (themedColors[role][name] ??= {}) as Record<string, string>
      group[shade] = value
    }
  }
}

for (const role of ['bg', 'text', 'border'] as Role[]) {
  register(role, 'slate', NEUTRAL_DARK[role], { ...colors.slate, white: colors.white })
  for (const name of ACCENT_PALETTES) {
    const palette = colors[name] as Palette
    register(role, name, accentDark(palette)[role], palette)
  }
}

const themeDarkPalette = plugin(({ addBase }) => {
  addBase({ '.dark': darkVars })
})

const config: Config = {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      backgroundColor: themedColors.bg,
      gradientColorStops: themedColors.bg,
      textColor: themedColors.text,
      placeholderColor: themedColors.text,
      borderColor: themedColors.border,
      divideColor: themedColors.border,
      ringColor: themedColors.border,
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
  plugins: [tailwindAnimate, themeDarkPalette],
}

export default config
