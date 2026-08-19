# Design QA — Desktop sidebar icons

- Reference: `/var/folders/c2/j3mv6qys0p5312s_2k786sv80000gn/T/codex-clipboard-31ae3eb2-4d3e-4f53-921d-d42de8a2d5df.png`
- Implementation: `/tmp/wyckoff-sidebar-icons-implemented.png`
- Combined comparison: `/tmp/wyckoff-sidebar-icons-comparison.png`
- Runtime: Electron production renderer (`file://.../desktop/src/renderer/dist/index.html`)
- Viewport: 1391 × 768 px; focused sidebar crop: 220 × 768 px
- Reference pixels: 532 × 770 px; implementation crop: 220 × 768 px
- State: light theme, sidebar expanded, `今天` selected

## Comparison

The existing spacing, grouping, typography, selection background, and sidebar width remain unchanged. The eight placeholder character glyphs are replaced by one coherent Lucide outline set at 17 px with a shared 1.7 stroke weight. Every icon is vertically centered, uses the existing text color tokens, and gains the same hover/selected emphasis as the label.

## Findings and iteration history

1. Verified the reference and live Electron renderer together in one side-by-side comparison.
2. Confirmed distinct, recognizable semantics for today, task runs, approvals, portfolio, schedules, tracking, attribution, and reports.
3. Confirmed the active icon aligns with the active label and does not change the existing navigation hit targets.
4. Confirmed decorative icons are excluded from accessible button names; the navigation labels remain the accessible names.
5. Confirmed the icon colors are driven by the existing theme variables, so light and dark themes share the same contrast behavior.

## Result

passed
