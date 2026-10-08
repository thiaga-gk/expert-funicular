# Design

## Personas and lenses

One drill-down structure, four lenses. A lens picks the landing view; every view stays reachable, so the numbers
never disagree between personas.

| Lens | Who | Lands on | Reads first |
|---|---|---|---|
| Executive | sponsor, finance | Executive summary | five KPI tiles, lifecycle flow, trend, call-outs, top warehouses; print one-pager |
| FinOps | cost analyst | Portfolio | treemap (area = spend, colour = savings share), spend-vs-opportunity scatter, warehouse table; then Realization: realized run-rate, realization rate, realized vs. expected per change, claim status, trend, the fleet configuration-change log (linked to a recommendation or not), applied-change table |
| Engineer | warehouse owner, platform | Recommendations | aging heatmap, implementation roadmap, status board, then the warehouse view: config, daily cost with epochs and change markers, configuration history with before/after figures, the scenario tree (warehouse → scenarios, recommended first → steps in execution order, gates marked), recommendation cards in the same order with evidence collapsed, realization |
| Audit | governance, reviewer | Audit | run provenance, skipped inputs, IDs and hashes, applied thresholds, raw package JSON |

Depth: L0 executive → L1 portfolio, recommendations and realization → L2 warehouse → L3 audit. Every row, tile and chart mark
that names a warehouse opens the warehouse view.

## Visual system

An instrument panel for compute cost: a navy masthead, flat ruled surfaces, one brass accent, and figures set in an
engineering face. It deliberately avoids the default dashboard look (an indigo accent, a gradient logo tile and soft
shadows on every card).

- Type: IBM Plex Sans for the interface and IBM Plex Mono for readouts (labels, config values, IDs, axis figures),
  both embedded from `assets/vendor/fonts/` (SIL Open Font License, sha256-pinned in the generator) so the page
  works offline. Tabular numerals throughout.
- Masthead: navy in both themes (`--mast`), with the three-bar mark and the brass tab underline.
- Light: cool canvas `#F3F4F7`, white surfaces, hairline `#E1E4EA`, navy ink `#0E1726`, brass accent `#9A6B2F`.
- Dark (toggle, or the system setting): navy canvas `#09111E`, surfaces `#0F1A2B`, brass `#D5AB6D`.
- Surfaces: 8px radius on panels, 3-6px on controls and chips, hairline rules instead of shadows. The five KPI tiles
  are one ruled ledger strip, not five cards. Section titles are mono labels with a rule.
- Tokens live on `:root`; dark is defined under `prefers-color-scheme: dark` guarded by `:root:not([data-theme=light])`
  and again under `:root[data-theme=dark]`. ECharts reads the same tokens through a registered theme and is
  re-initialized on a theme change.
- Categorical series: the dataviz skill's validated reference palette, the Tableau-style hue family (blue, orange,
  aqua, yellow, magenta, green, violet, red) with separate light and dark steps. Raw Tableau 10 fails the validator
  (two slots too light, three too grey, green next to teal too close), so its re-stepped values are used. Validated
  on the white surface (light) and on `#111A2E` (dark): lightness, chroma, CVD separation and normal-vision floor
  pass; the light-mode contrast warning is answered with visible labels and table views.
- Semantic colours are reserved: emerald = savings, rose = cost increase, amber = review/aging, blue = information.
  Signed impact uses a diverging rose → grey → emerald scale. Brass is the interface accent only, never data.
- Composition (confidence mix, cost profiles, DBU vs AWS, query sources) is a segmented bar with a labelled list,
  not a donut. Config readouts carry subject detail: the cluster glyph shows always-on and maximum clusters.
- Compact currency on tiles and axes ("$1.24M"), full figures in tooltips; negative values use a true minus sign.

## Interaction

- Run selector with previous/next; filters (workspace, owner, type, cost profile, status, confidence) in one row
  above the charts, synced to the URL hash; KPI tiles show the change against the previous run.
- Warehouse lists default to top spenders, with a top-savers toggle.
- Every chart has a hover tooltip; legends wherever more than one series is shown.
- Print stylesheet: the Executive summary on one landscape page.
- Works offline; the only per-viewer storage is the theme and lens choice (`localStorage`, guarded).
