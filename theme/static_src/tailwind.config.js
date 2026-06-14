/**
 * PyRunner — Tailwind config.
 *
 * Colors are driven by CSS custom properties (see src/styles.css) so the whole
 * UI is theme-aware (dark / light) without sprinkling `dark:` variants.
 * Each token resolves to `rgb(var(--token) / <alpha-value>)`, so opacity
 * utilities like `bg-ok/10` still work.
 */

const v = (name) => `rgb(var(${name}) / <alpha-value>)`;

module.exports = {
    content: [
        '../templates/**/*.html',
        '../../templates/**/*.html',
        '../../**/templates/**/*.html',
        // Python files that carry Tailwind classes in form widget attrs.
        '../../core/**/*.py',
    ],
    darkMode: 'class',
    theme: {
        extend: {
            colors: {
                // ── Console design tokens (the new system) ──
                ink: v('--ink'),
                panel: v('--panel'),
                'panel-hi': v('--panel-hi'),
                line: v('--line'),
                'line-soft': v('--line-soft'),
                text: v('--text'),
                muted: v('--muted'),
                faint: v('--faint'),
                ok: v('--ok'),
                warn: v('--warn'),
                fail: v('--fail'),
                info: v('--info'),
                accent: v('--accent'),

                // ── Legacy tokens (now theme-aware; dark = original values) ──
                'code-bg': v('--code-bg'),
                'code-surface': v('--code-surface'),
                'code-border': v('--code-border'),
                'code-text': v('--code-text'),
                'code-muted': v('--code-muted'),
                'code-accent': v('--code-accent'),
                'code-green': v('--code-green'),
                'code-yellow': v('--code-yellow'),
                'code-red': v('--code-red'),
                'code-purple': v('--code-purple'),
            },
            fontFamily: {
                sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
                mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Consolas', 'monospace'],
            },
        },
    },
    plugins: [
        require('@tailwindcss/forms'),
        require('@tailwindcss/typography'),
        require('@tailwindcss/aspect-ratio'),
    ],
}
