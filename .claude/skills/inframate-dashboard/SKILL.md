---
name: inframate-dashboard
description: Build, refresh or explain the inframate dashboard - one self-contained, premium HTML page over the Publisher's RecommendationPackages and the Realizer's RealizationPackages, with an executive summary (annual actual spend total/DBU/AWS, identified savings range, realized savings, open recommendations, warehouses analysed), a fleet portfolio, a recommendations board with aging and an implementation roadmap, a realization view (realized vs. expected savings per applied change), a per-warehouse drill-down with a scenario and plan tree, evidence and realization, and an audit view, navigable by run date and by persona lens (Executive, FinOps, Engineer, Audit). Use this skill whenever someone wants to see, present, share, refresh or update optimizer results as a dashboard, report or overview page - "show me the dashboard", "refresh the dashboard with the latest run", "what did the last run find", "build an exec view of the savings", "how much have we realized", "which recommendations are aging", "rebuild the demo dashboard" - even if they do not say "dashboard", and whenever docs/dashboard/shared/inframate-dashboard.html must be regenerated after a Publisher or Realizer change.
---

# inframate dashboard

One HTML file, no server, no network: the generator reads the output folders, validates every package against
the committed schemas, joins Publisher runs with Realizer snapshots, and embeds the result with a vendored,
sha256-pinned ECharts. Opening the file is the whole user experience.

## What lives where

| Path | What it is |
|---|---|
| `scripts/build_dashboard.py` | The generator: discovery, validation, as-of join, KPIs, aging, insights, render, verify, check |
| `assets/template.html` | The page: design tokens (light + navy dark), five views, lens presets, filters, charts |
| `assets/vendor/echarts.min.js` | ECharts 5.5.1 (Apache-2.0, `LICENSE-echarts.txt`, `NOTICE-echarts.txt`); the build refuses another sha256 |
| `assets/vendor/fonts/` | IBM Plex Sans and Mono (SIL OFL, `LICENSE-ibm-plex.txt`), embedded as data URIs; sha256-pinned |
| `references/kpi-rules.md` | Every headline figure: source field, rule, and why (read before changing a KPI) |
| `references/data-model.md` | Inputs, run discovery, the as-of join, aging, the embedded dataset shape |
| `references/design.md` | Personas and lenses, views, palette and its validation, interaction rules |
| `docs/dashboard/shared/inframate-dashboard.html` | The committed demo build (generated; never edit by hand) |

## Modes

Run from the repository root with the project interpreter (`.venv/bin/python`, `PYTHONPATH=src`).

**Build or refresh from real output.** Refreshing is rebuilding: every build rescans the roots, so a new run
appears and becomes the default selection.
```
python .claude/skills/inframate-dashboard/scripts/build_dashboard.py \
  --publisher-root ./warehouse_optimizer_out --realizer-root ./realizer_out --verify
```
It writes `<publisher-root>/dashboard/inframate-dashboard.html`. That page holds owner emails and real cost, so
keep it local: never commit it, and never publish it as an artifact or anywhere else unless the user asks for that
explicitly. Without a Realizer root the realization figures say so instead of showing zero. Options:
`--detail-runs N` (runs with full drill-down; trends always cover every run, default 6), `--raw full|lean|none`
(raw package JSON in the Audit view), `--out`, `--title`.

**Rebuild the committed demo** after any change to the template, the generator, or Publisher/Realizer output:
```
python .claude/skills/inframate-dashboard/scripts/build_dashboard.py --demo --verify
python .claude/skills/inframate-dashboard/scripts/build_dashboard.py --check      # is the committed demo current?
```
`--demo` runs three monthly `publisher --demo --demo-as-of` runs, each followed by `realizer --publisher-run ...
--demo --as-of` before the next one, into a temporary folder (`--demo-workdir DIR` keeps and reuses it, which makes
`--check` fast). The demo is deterministic, so `--check` byte-compares.

**Verify** (`--verify`) fails on: a page script that does not parse (checked with Node when installed), an external resource, an unresolved placeholder, duplicate element ids, a missing
view, a committed demo above 6 MB, or a run whose single-currency KPIs differ from the cells of that run's own
`workbook.xlsx` Executive Summary. That last check keeps the dashboard and the workbook telling the same story.

## Answering questions with it

When someone asks what a run found, build (or refresh) the page and report the executive figures from the build
output, then point to the view that answers the question: Portfolio for where the money is, Recommendations for
what is open and how old it is, Realization for what applied changes are saving against what was expected, Warehouse for why a change is recommended and what happened after it was applied,
Audit for IDs, hashes, versions and skipped inputs. Quote figures as the page shows them (annual, per currency).

## Ground rules

- **Projection only.** The page shows what the packages say. The generator filters, joins, counts, ages and sums
  within one currency, using the workbook's availability rules (`references/kpi-rules.md`). It never estimates,
  prices, annualizes or scales a figure; if a number is missing upstream, fix it upstream (Publisher or Realizer)
  and rebuild.
- **Unknown is not zero.** A null cost is shown as unavailable, with the known subset beside it.
- **Data clocks, not folder names.** Runs are keyed by `generated_at` and snapshots by `as_of_utc`; run folder names
  are wall-clock and never reach the page, which keeps the demo byte-stable.
- **Skips are visible.** A run with an unsupported contract version or an invalid package is listed under Audit,
  never dropped silently.
- **Design changes go through the tokens.** Colours live once on `:root` (light) and twice for dark; the chart
  palette was checked with the dataviz validator (`references/design.md`). Re-run it if you change a series colour.
- After changing the template or the generator: `--demo --verify`, then `python -m unittest
  tests.unit.skills.test_inframate_dashboard -v`, then commit the regenerated demo with the change.
