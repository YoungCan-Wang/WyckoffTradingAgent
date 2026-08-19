# Design QA — Desktop sidebar interaction

## Evidence

- Problem capture: `/var/folders/c2/j3mv6qys0p5312s_2k786sv80000gn/T/codex-clipboard-9bda0dc2-26ca-49b3-beed-36a7a8c08b19.png`
- Codex interaction reference: `/var/folders/c2/j3mv6qys0p5312s_2k786sv80000gn/T/codex-clipboard-2b572b90-ee9b-424f-81fb-4e8d42486f14.png`
- Expanded implementation: `/tmp/wyckoff-sidebar-ux-expanded.png`
- Collapsed implementation: `/tmp/wyckoff-sidebar-ux-collapsed.png`
- Artifact-pane implementation: `/tmp/wyckoff-sidebar-ux-pane-open.png`
- Combined focused comparison: `/tmp/wyckoff-sidebar-ux-comparison.png`
- Runtime: Electron production renderer (`file://.../desktop/src/renderer/dist/index.html`)
- Implementation viewport: 1391 × 768 px, captured at the runtime's native screenshot density
- Problem capture: 2836 × 182 px; Codex reference: 2940 × 1660 px
- State: light theme; left sidebar expanded and collapsed; right artifact pane opened, collapsed, and restored

## Findings and comparison history

1. **P1 — duplicate-looking sidebar ownership.** The problem capture kept a filled sidebar button in the thread toolbar while the sidebar was already open, so the control looked detached and permanently selected. The implementation now uses one real button: it lives beside the product name when expanded and moves to the thread toolbar only after collapse. Post-fix evidence: expanded and collapsed captures above.
2. **P1 — destructive/empty artifact panel behavior.** The previous panel command could reveal an empty right rail and collapsing it closed every tab. The panel now appears only after a report, chart, browser, or agent artifact exists; collapse hides without destroying tabs, and the existing panel can be restored from the Open menu or shortcut. Post-fix evidence: artifact-pane capture plus the tested collapse/restore sequence.
3. **P2 — blank toolbar hierarchy.** The old thread toolbar had no contextual label. It now shows the active destination title and keeps the Open menu right-aligned, matching the Codex reference's division between navigation ownership and thread context.

## Required fidelity surfaces

- Fonts and typography: existing SF Pro/PingFang stack, weights, and sizes are preserved; the new context title uses the existing secondary-text hierarchy.
- Spacing and layout rhythm: sidebar width, navigation rhythm, window drag regions, and 62 px titlebar alignment are preserved. Controls remain 28 px with existing hit-area conventions.
- Colors and tokens: all new states use `--tx2`, `--tx3`, `--hover`, `--side`, and `--line`; no new standalone palette was introduced.
- Image and icon fidelity: PanelLeftOpen, PanelLeftClose, and PanelRightClose come from the installed Lucide set; no text glyphs or handcrafted SVGs are used for the sidebar controls.
- Copy and content: button names explicitly announce 展开/收起 in both Chinese and English, and decorative icons remain hidden from accessible names.

## Interaction checks

- Expanded left sidebar → one `收起侧栏 ⌘B` control inside the sidebar.
- Collapsed left sidebar → the same control becomes `展开侧栏 ⌘B` in the thread toolbar.
- First launch → wide desktop windows open the sidebar; later launches respect the saved user choice.
- Empty artifact state → no artifact-panel command is shown and no empty rail can be opened.
- Existing artifact state → right pane collapses without deleting its tab and restores the same browser tab.

## Full-view and focused comparison

- Full-view captures verify the thread keeps its readable center column in all three panel states.
- The focused comparison combines the reported before-state, Codex reference, and revised top-left control placement at normalized scale. No additional focused crop is needed because the affected controls and titlebar alignment are legible there.

## Follow-up polish

- P3: the app keeps its own compact Wyckoff brand/header density instead of cloning Codex's taller project-list header; this is intentional because the navigation information architecture differs.

## Final result

passed
