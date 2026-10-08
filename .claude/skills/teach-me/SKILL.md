---
name: teach-me
description: Teach an engineer how inframate-v1 works through a generated, professional HTML field guide (docs/guide/shared/inframate-guide.html) with light/dark themes, inline SVG flows, worked examples recomputed from a golden case, and interactive explorers for the Publisher's RecommendationPackage and the Realizer's RPKG, ledger and measurement JSON. Use this skill whenever someone wants to learn, onboard to, explain or walk through this repo - its pipeline layers, optimization techniques, estimation or realization math, cost profiles, run scope, IDs and hashes, the recommendation or realizer JSON - or asks to build, regenerate, refresh or update the guide ("teach me", "explain the publisher", "how does the realizer measure savings", "refresh the guide", "onboard a new engineer"), even if they do not name the guide.
---

# teach-me

Act as the repo's teacher. The deliverable is one self-contained HTML guide that an engineer reads to understand the
Publisher and the Realizer end to end, and that stays true to the code because its facts are generated and its
narrative is re-checked against the files it cites.

## What lives where

- `scripts/build_guide.py` - the generator. It imports facts from the code (versions, `Thresholds`, cost profiles,
  product constants, scope, CLI flags, workbook layout, ledger events, schema fields), reads the reference tables
  (techniques, blocker codes, docs map, contract field tables), loads golden case `ge12-full-scope-auto-stop`,
  recomputes every worked-example number and fails when one no longer matches, then renders the chapter fragments into
  `assets/template.html`.
- `references/chapter-map.yaml` - the chapters in order, the source files each one explains (their contents are
  fingerprinted) and the documents that own each topic.
- `assets/chapters/<id>.html` - the narrative fragments, with placeholders instead of literal numbers.
- `assets/notes.yaml` - teaching notes and ID-family text for the JSON explorers.
- `references/glossary.yaml` - glossary terms for hover tips and the reference chapter.
- `references/authoring.md` - how to write a fragment: voice, accuracy rules, placeholders, components, SVG rules,
  quizzes, worked-example keys. Read it before writing or rewriting any fragment.

The output `docs/guide/shared/inframate-guide.html` is committed and generated. Never edit it by hand; change a
fragment, a note, the glossary or the generator and rebuild.

## Modes

Pick the mode from the request. With no argument, check whether the guide exists and is current, then say so and
offer a refresh if it is stale.

### Build (the guide is missing, or "build", "regenerate")

1. Read `references/authoring.md` and `references/chapter-map.yaml`.
2. For any chapter whose fragment is missing, read its cited sources and owning docs, then write the fragment.
3. Run `python .claude/skills/teach-me/scripts/build_guide.py --verify` from the repo root (the repo's `.venv`
   Python). Fix every `FAIL` line: an unresolved placeholder, a citation to a moved file or symbol, a worked-example
   mismatch, an external resource, a duplicate id or a broken in-page link.
4. Look at the result once in a browser (light and dark, desktop and about 390px wide) where the session offers a
   way, for example headless Chrome `--screenshot`. Fix what is visibly broken, rebuild once.
5. Report what was built, the size, the verify result and the chapters written.

### Refresh ("refresh", "update the guide", or the guide is stale)

The point of a refresh is that only what changed is rewritten, and what is rewritten is re-read from the code.

1. Run `build_guide.py --stale`. It compares each chapter's source fingerprints with those stored in the committed
   guide and prints the stale chapters with the files that changed, were added or were removed.
2. For each stale chapter, read the changed files (use `git diff` against the last commit that touched the guide,
   `git log -1 --format=%H -- docs/guide/shared/inframate-guide.html`, when the change is large) and the fragment.
   Update every claim, diagram, formula card and quiz answer the change affects. Leave unaffected prose alone so the
   diff stays reviewable.
3. When a change adds a concept with no home in the guide (a new knob, layer, ID family, output file), add it to the
   right chapter, and to `chapter-map.yaml` sources if it lives in a new file. A new public ID family also needs a line
   in `assets/notes.yaml` `id_families`; a new domain term, a glossary entry.
4. Run `build_guide.py --verify`. Facts, tables, versions and examples refresh on their own; a worked-example
   mismatch means the estimator or the golden case changed, so re-read the formula in code and fix the card or
   `worked_examples()` together.
5. Summarise per chapter what changed and why, and list anything in the docs that disagreed with the code.

`build_guide.py --check` (no write) says whether the committed guide equals a fresh build; use it to confirm a
refresh is complete.

### Topic ("teach me <topic>", a question about one concept)

Answer in chat without rebuilding: read the matching fragment and the code it cites, explain the mechanism with the
worked example's numbers, and point to the chapter anchor (`docs/guide/shared/inframate-guide.html#<chapter-id>`)
and the owning document. If the code and the guide disagree, say so and offer a refresh.

| Topic words | Chapter |
|---|---|
| overview, invariants, read-only, determinism | `product-map` |
| layers, pipeline, publish(), CLI adapter | `publisher-pipeline` |
| system tables, epochs, evidence.json, replay | `evidence-epochs` |
| size, max/min clusters, auto-stop, migration, canary, blockers | `techniques` |
| forecast, savings, confidence, bundle, envelope, low/base/high | `estimation` |
| thresholds, cost profiles, run scope, constants | `policy` |
| package JSON, IDs, hashes, write order | `package-json` |
| realizer, counterfactual, guards, attribution, cross-check | `realizer` |
| RPKG, ledger, measurement, snapshot | `realizer-json` |
| example, walk-through, one warehouse | `trace` |
| outputs, workbook, manifest, commands, live run, skills | `operations` |
| blocker lookup, glossary, docs map | `reference` |

## Ground rules

- The code and its tests are authoritative, then the locked contracts, then the owning documents
  (`docs/README.md`, Precedence). The guide teaches and points; it never becomes a second home for a topic.
- Products are read-only. Commands in the guide never mutate a workspace; a live command always names an OAuth
  profile, never a token.
- Keep the page offline: no CDN, no web fonts, no external images. `--verify` checks this.
- `tests/test_docs.py` checks that every path and symbol the committed guide cites still exists, so a rename that
  breaks a citation fails the test suite until the guide is refreshed.
