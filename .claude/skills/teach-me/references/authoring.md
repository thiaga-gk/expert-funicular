# Authoring a chapter

How to write or rewrite a fragment in `assets/chapters/<id>.html`. Read this before touching a fragment. The builder
(`scripts/build_guide.py`) wraps each fragment in its `<section>`, adds the chapter number, `<h2>` title and the
"Where this lives" panel from `references/chapter-map.yaml`, so a fragment starts with its lede paragraph.

## Contents
1. Reader and voice
2. Accuracy rules
3. Chapter shape
4. Placeholders
5. Components (HTML vocabulary)
6. Diagrams (inline SVG)
7. Quizzes
8. Worked-example keys

## 1. Reader and voice

The reader is an engineer who can read Python and knows SQL warehouses only loosely. They want to answer, after one
chapter: what does this layer decide, from which evidence, with which formula, and where is the code.

- Plain, direct sentences in active voice. Name things the way the code and the contract name them.
- Explain the mechanism and the reason ("the low track pairs with the high cost because...") rather than restating a
  rule list. A chapter that only lists modules has failed.
- Describe the current state in generic wording. No finding IDs, decision IDs, batch numbers or history
  ("since 2.9.0", "G56") - that belongs to the archive.
- No mannered devices: no em-dash asides, no "not X, but Y", no "worth noting", no scare quotes.

## 2. Accuracy rules

The guide's value is that it matches the code. Every claim must survive a reviewer opening the cited file.

- Read the code you cite before writing about it. Docs can lag; when a document and the code disagree, the code
  wins, write what the code does, and tell the user about the disagreement (do not fix other docs from here).
- Cite at the point of the claim with `{{cite:path::Symbol}}`. Symbols must be a `def`, `class` or module-level
  assignment in that file; `--verify` fails on a missing one.
- Never type a threshold, product constant, version, count or example value as a literal. Use `{{fact:...}}`,
  `{{pkg:...}}`, `{{rpkg:...}}` or `{{ex:...}}`, so a refresh carries the new value (`{{fact:realizer.*}}` holds the
  Realizer policy defaults and realization constants). Literal numbers are allowed only for constants written inline
  in the code that no fact exposes (for example 365 days in an annualization); cite where they live.
- Topics have one home (`docs/README.md`). The guide teaches and points: name the parameter, show the formula with the
  example's numbers, then link the owning document. Do not copy whole tables by hand; use `{{table:...}}`.
- Module docstrings can carry stale versions (for example in `src/realizer/realizer.py`); take versions only from
  `{{fact:versions...}}`.

## 3. Chapter shape

1. A `<p class="lede">` paragraph: what this part of the system is for, in two or three sentences.
2. At least one diagram that shows the mechanism (section 6).
3. Three to six `<h3 id="<chapter-id>-<slug>">` subsections. The ids feed the sidebar and search; keep them unique.
4. Formula cards for every calculation the chapter owns, filled with the worked example.
5. Callouts for invariants and traps (`callout rule`), for Realizer-side notes (`callout real`).
6. A quiz block of three or four questions at the end (section 7).

Keep a fragment under about 450 lines. Prefer one precise diagram over three decorative ones.

## 4. Placeholders

| Placeholder | Renders |
|---|---|
| `{{fact:path\|fmt}}` | A value from the facts the builder imports (`collect_facts`): `versions.publisher.publisher_version`, `thresholds.<field>`, `product_constants.<NAME>`, `realizer.policy.<field>`, `realizer.review_above_high`, `realizer.reoptimize_below_low`, `realizer.volume_ratio_band`, `realizer.agreement_band_pct`, `realizer.noise_multiple`, `realizer.mature_days`, `realizer.canary_guard_window_days`, `golden.publisher_count`, `demo_fleet_size`, `blocker_count`, `threshold_count`, `supported_warehouse_types`, `active_knobs`, `size_order`, `scope_scenarios`, `workbook.sheet_names`, `schema.package_required`, `ledger_event_types`, `hash_hex_length`. |
| `{{pkg:path\|fmt}}` | A value from the example RecommendationPackage. Paths take `[n]` and `[field=value]` selectors: `candidates[knob=auto_stop_minutes].confidence.score`. |
| `{{rpkg:path\|fmt}}` | A value from the example realization package: `economics.annualized_realized_savings`. |
| `{{ex:path\|fmt}}` | A recomputed worked-example value (section 8). |
| `{{cite:path}}`, `{{cite:path::Symbol}}`, `{{cite:path::Symbol\|label}}` | A code link, checked by `--verify`. |
| `{{doc:docs/path.md#anchor\|label}}` | A link to a document (GitHub-style anchor). |
| `{{term:key}}`, `{{term:key\|text}}` | A glossary term with a hover definition (`references/glossary.yaml`); the default text is the term with a lower-case first letter (acronyms and names keep their case). |
| `{{status:ACTIONABLE}}` | A status chip coloured by meaning. |
| `{{id:PKG-...}}` | A styled ID; the whole argument is the ID, no label (prefer `{{pkg:recommendation_package_id\|id}}`). |
| `{{explorer:name\|path\|open-depth\|select-path}}` | An interactive JSON explorer. Names: `pkg`, `rpkg`, `ledger`, `measurement`, `hash_payload`. `path` roots the tree (empty for the whole document), `select-path` pre-selects a node. |
| `{{range:source\|path\|label\|obs-source:path}}` | A low/base/high bar from a `{low, base, high}` object (`source` is `pkg`, `rpkg` or `ex`); the optional last part adds an observed tick. |
| `{{table:name}}` | A generated table: `versions`, `thresholds`, `profiles`, `product_constants`, `scope`, `techniques`, `blockers`, `workbook`, `cli_publisher`, `cli_realizer`, `cli_backtest`, `golden_publisher`, `golden_realizer`, `ledger` (the example's ledger as a timeline), `ledger_events`, `envelope`, `chain`, `candidates`, `checks`, `glossary`, `docs_map`. |

Formats (`|fmt`): `money` (12,345.67 with a true minus), `int`, `raw`, `id`, `status`, `json`, `list`, `count`,
`pct1`/`pct2`. With no format the value is HTML-escaped as text.

## 5. Components (HTML vocabulary)

The template styles these classes; use them rather than inline styles (inline colours break dark mode).

```html
<p class="lede">...</p>
<h3 id="estimation-autostop">Auto-stop factor</h3>
<div class="callout"><b>Invariant</b><p>...</p></div>          <!-- also: callout rule | warn | real -->
<div class="grid c2"> <div class="card"><h4>...</h4><p>...</p></div> ... </div>   <!-- c2 or c3 -->
<div class="formula">
  <header><b>Candidate savings</b>{{cite:src/publisher/optimizer/estimator/savings.py::SavingsModel|savings.py}}</header>
  <div class="eq">savings[track] = baseline − cost[opposite track]</div>
  <div class="worked">low  = {{ex:autostop.baseline_dbu}} − {{ex:autostop.cost_high}} = {{pkg:...|money}} <span class="ok">✓</span></div>
  <div class="why">Why the tracks cross: ...</div>
</div>
<ol class="trace"><li><div class="body"><h4>...</h4><p>...</p></div></li><li class="r">...</li></ol>  <!-- class r = Realizer step -->
<pre class="code"><code>publisher --demo --output-dir ./demo_out</code></pre>
<span class="chip good|warn|bad|hold|real|pub">TEXT</span>
<div class="table-wrap"><table class="data">...</table></div>   <!-- hand tables only when no generated table fits -->
```

`<var>` inside `.eq` colours a variable. `.eq` and `.worked` keep whitespace, so align columns with spaces.

## 6. Diagrams (inline SVG)

A diagram earns its place when it shows a mechanism a reader would otherwise assemble from prose: the order of calls,
where data crosses a boundary, which state a step reads and writes, how a decision branches.

```html
<figure class="diagram">
  <svg viewBox="0 0 960 300" role="img" aria-label="One sentence stating what the picture shows">
    <rect class="box pub" x="20" y="40" width="150" height="56" rx="8"/>
    <text class="lbl" x="95" y="64" text-anchor="middle">Analyzer</text>
    <text class="sub" x="95" y="82" text-anchor="middle">observes</text>
    <path class="edge pub" d="M170 68 H 230"/>
    <text class="elbl" x="200" y="60" text-anchor="middle">FleetAnalysis</text>
  </svg>
  <figcaption>What the picture shows, in one or two sentences.</figcaption>
</figure>
```

- Classes: boxes `box` plus `pub` (Publisher), `real` (Realizer), `gate` (a check that can hold or refuse), `bad`,
  `good`, `muted`, `ghost` (dashed, optional or out of scope); text `lbl` (13px bold), `sub` (11.5px), `mono`,
  `cap` (small caps label), colour helpers `t-pub t-real t-warn t-bad t-good`; edges `edge` plus `pub real warn bad`,
  `dash`, `plain` (no arrowhead); edge labels `elbl`; `lane` for a background band; `dot`.
- Arrowheads come from the page's shared markers (`#ah`, `#ah-pub`, `#ah-real`, `#ah-warn`, `#ah-bad`) through the
  edge classes. Do not define markers, `<style>` or `<script>` inside a fragment's SVG.
- Never set a literal `fill` or `stroke` colour: classes carry the theme. `fill="none"` is fine.
- Lay out on an 8px grid with shared baselines. Labels one to three words; sentences go in the caption.
- Label every arrow that carries data with what it carries (`FleetAnalysis`, `PKG JSON`, `ledger entry`).
- Size `viewBox` to the content; wide flows run left to right at about 900-1000 units wide. The figure scrolls
  sideways on a phone (min-width 560px) - add class `narrow` to the figure for small diagrams that should shrink.
- `role="img"` and an `aria-label` that states the claim; a `<figcaption>` saying the same for sighted readers.

## 7. Quizzes

```html
<div class="quiz"><b>Check yourself</b>
  <details><summary>Why can't two candidates' savings be added?</summary>
    <div><p>Each is priced alone against the same baseline ...</p></div></details>
</div>
```

Ask about mechanisms and traps a newcomer gets wrong, not trivia. Answers cite code or a document where useful.

## 8. Worked-example keys

The example is golden case `ge12-full-scope-auto-stop` (warehouse `wh-demo-005`): a full-scope package with five
candidates and two scenarios, of which the owner applied the auto-stop step exactly; the Realizer measured four
settled weeks. `worked_examples()` in `scripts/build_guide.py` recomputes every value below and fails the build when
one no longer matches the published package.

- `ex:autostop.*` - `baseline_dbu`, `cost_low|cost_base|cost_high`, `share_optimistic|share_base|share_conservative`,
  `realized`, `fixed`, `predicted_base`, `factor_base`, `r_optimistic|r_base|r_conservative` (the realization factors
  from `Thresholds`), `savings_dbu_base`, `current`, `target`.
- `ex:confidence_autostop.*`, `ex:confidence_size.*` - `raw`, `cap`, `score`, `level`, `status` and the components
  `coverage`, `sample`, `stability`, `epoch_maturity`, `signal`. `ex:weights.*` - `base`, `coverage`, `sample`,
  `stability`, `epoch`, `signal`.
- `ex:chain` (use `{{table:chain}}`), `ex:envelope.*` - `low`, `base`, `high` (and `{{table:envelope}}`).
- `ex:identity.*` - `payload_keys`, `canonical_prefix`, `canonical_length`, `full`, `id`.
- `ex:realizer.*` - replay `net_minutes`, `dbu_per_min`, `aws_hourly`, `added_idle`, `avoided_starts`,
  `restart_cost`, `replayed_target`; money `dbu_saving`, `aws_saving`, `post_billed`, `observed_aws`, `observed_new`,
  `cf_old`, `realized`, `annual`, `expected`, `low`, `high`, `review_line`, `pre_billed`; `post_minutes`, `days`,
  `ratio`, `verdict`, `weekly`, `noise`, `billed_reduction`, `replay_dbu`, `diff_pct`, `cross_check`, `volume_ratio`,
  `rpkg_id`.
- `{{table:checks}}` lists every recomputation and its result.

If a chapter needs a number that is not here, add the recomputation to `worked_examples()` with a check, rather than
typing the number.

## 9. Explorer notes

The JSON explorers show, for a selected field, its type, flags and meaning from the contract's field tables (Publisher
package only) plus a teaching note from `assets/notes/<explorer>.yaml` (`pkg`, `rpkg`, `ledger`, `measurement`,
`hash_payload`). Keys are explorer paths with array indices written `[]` and evidence-map keys written `{id}`
(`candidates[].status`, `evidence.{id}`, `[].event_type` for the ledger array). Values are short inline HTML: why the
field exists, how it is computed, what to look at in the example. Do not repeat the contract text.
