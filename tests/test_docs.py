"""Documentation guards: links resolve, contract versions match the code, cited headings exist.

Scope is the four design documents, docs/README.md, README.md, CLAUDE.md, the publisher and shared folders of docs/contracts/ and docs/reference/. It never globs `**`,
which would reach .worktrees/ and docs/archive/. Rules are added as the documentation work makes them true.
"""
import ast
import dataclasses
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from publisher.contracts import reporter_contract as rc  # noqa: E402
from publisher.contracts import workbook_contract as wc  # noqa: E402
from publisher import publisher  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.config import versions as versions_mod  # noqa: E402
from tests.golden import case_names, load_case  # noqa: E402

CORE = {
    "prd": ROOT / "docs/product/publisher/prd.md",
    "context": ROOT / "docs/architecture/publisher/context.md",
    "hld": ROOT / "docs/architecture/publisher/hld.md",
    "lld": ROOT / "docs/architecture/publisher/lld.md",
}
SCOPE = (
    list(CORE.values())
    + [ROOT / "docs/README.md", ROOT / "README.md", ROOT / "CLAUDE.md"]
    + sorted(path for kind in ("contracts", "reference") for product in ("publisher", "shared") for path in (ROOT / "docs" / kind / product).glob("*.md"))
)
# The Realizer documents (docs/<type>/realizer/) are checked for links, and the specification set also for repo paths.
# They are not in SCOPE: they cite the Reporter JSON Contract 1.0.0 the Realizer accepts, and the frozen v1.0.0 release
# records under docs/proposals/realizer/ name the paths of the single-file release.
REALIZER_DOCS = sorted(ROOT.glob("docs/*/realizer/*.md"))
REALIZER_SPEC = sorted(path for path in REALIZER_DOCS if path.parent.parent.name in ("product", "contracts") or path.name in ("context.md", "hld.md", "lld.md"))
FENCE = re.compile(r"```.*?```", re.S)
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
BACKTICK = re.compile(r"`([^`\n]+)`")
REPO_PATH = re.compile(r"^(docs|src|scripts|contracts|reporters|tools|schema|tests|pricing|examples)/[\w./-]+$")
REPORTER_VERSION = re.compile(r"(?<!Workbook )Reporter(?: JSON)?\s+[Cc]ontract\W{0,6}v?(\d+\.\d+\.\d+)")
PUBLISHER_VERSION = re.compile(r"(?<![\w-])[Pp]ublisher v(\d+\.\d+\.\d+)")
WORKBOOK_VERSION = re.compile(r"Workbook(?: Reporter)?\s+[Cc]ontract\W{0,6}v?(\d+\.\d+\.\d+)")

# Sections other files cite by number; they must keep their headings.
CITED_HEADINGS = {
    "prd": [r"^### 6\.4 ", r"^## 23\. ", r"^## 27\. ", r"^#{2,4} FR-OPT-001\b", r"^#{2,4} FR-PLAN-001\b"],
    "context": [r"^## 14\. ", r"^### 14\.1 ", r"^### 22\.1 "],
    "hld": [r"^### 3\.3 ", r"^### 6\.8 "],
    "lld": [r"^## 6\. ", r"^### 15\.1 ", r"^### 15\.8 ", r"^### 28\.3 ", r"^### 32\.3 "],
}


def read(path):
    return path.read_text(encoding="utf-8")


class DocsGuardTests(unittest.TestCase):
    def test_scope_files_exist(self):
        for path in SCOPE:
            self.assertTrue(path.exists(), path)

    def test_markdown_links_resolve(self):
        broken = []
        for path in SCOPE:
            for target in LINK.findall(FENCE.sub("", read(path))):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not (path.parent / target).resolve().exists():
                    broken.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])

    def test_realizer_documents_exist_and_their_links_resolve(self):
        self.assertEqual(len(REALIZER_DOCS), 17)
        self.assertEqual(len(REALIZER_SPEC), 8)
        broken = []
        for path in REALIZER_DOCS:
            for target in LINK.findall(FENCE.sub("", read(path))):
                if not target.startswith(("http://", "https://", "mailto:")) and not (path.parent / target).resolve().exists():
                    broken.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])

    def test_realizer_specification_repo_paths_exist(self):
        missing = []
        for path in REALIZER_SPEC:
            for token in BACKTICK.findall(FENCE.sub("", read(path))):
                token = token.rstrip("/.")
                if not any(ch in token for ch in "<*{") and REPO_PATH.match(token) and not (ROOT / token).exists():
                    missing.append(f"{path.relative_to(ROOT)}: {token}")
        self.assertEqual(missing, [])

    def test_realizer_documents_are_in_the_documentation_map(self):
        index = read(ROOT / "docs/README.md")
        unlisted = [str(path.relative_to(ROOT / "docs")) for path in REALIZER_DOCS if str(path.relative_to(ROOT / "docs")) not in index]
        self.assertEqual(unlisted, [])

    def test_backticked_repo_paths_exist(self):
        missing = []
        for path in SCOPE:
            for token in BACKTICK.findall(FENCE.sub("", read(path))):
                if any(ch in token for ch in "<*{"):
                    continue
                token = token.rstrip("/.")
                # Repo-relative, or relative to the file's own folder (inside docs/, `contracts/` means docs/contracts/).
                if REPO_PATH.match(token) and not ((ROOT / token).exists() or (path.parent / token).exists()):
                    missing.append(f"{path.relative_to(ROOT)}: {token}")
        self.assertEqual(missing, [])

    def test_contract_versions_match_the_code(self):
        wrong = []
        for path in SCOPE:
            text = read(path)
            for version in REPORTER_VERSION.findall(text):
                if version != rc.CONTRACT_VERSION:
                    wrong.append(f"{path.relative_to(ROOT)}: Reporter contract {version} != {rc.CONTRACT_VERSION}")
            for version in PUBLISHER_VERSION.findall(text):
                if version != versions_mod.PUBLISHER_VERSION:
                    wrong.append(f"{path.relative_to(ROOT)}: publisher {version} != {versions_mod.PUBLISHER_VERSION}")
            for version in WORKBOOK_VERSION.findall(text):
                if version != wc.WORKBOOK_CONTRACT_VERSION:
                    wrong.append(f"{path.relative_to(ROOT)}: Workbook contract {version} != {wc.WORKBOOK_CONTRACT_VERSION}")
        self.assertEqual(wrong, [])

    def test_cited_headings_exist(self):
        missing = []
        for key, patterns in CITED_HEADINGS.items():
            text = read(CORE[key])
            for pattern in patterns:
                if not re.search(pattern, text, re.M):
                    missing.append(f"{CORE[key].relative_to(ROOT)}: {pattern}")
        self.assertEqual(missing, [])

    def test_top_level_section_numbers_are_unique_and_contiguous(self):
        for key, path in CORE.items():
            with self.subTest(doc=key):
                numbers = [int(n) for n in re.findall(r"^## (\d+)\. ", FENCE.sub("", read(path)), re.M)]
                self.assertEqual(numbers, list(range(1, len(numbers) + 1)))


    def test_thresholds_reference_lists_every_parameter_with_its_default(self):
        text = read(ROOT / "docs/reference/publisher/thresholds.md")
        rows = re.findall(r"^\| `(\w+)` \| `([^`]+)` \|", text, re.M)
        documented = {}
        for name, default in rows:
            self.assertNotIn(name, documented, f"{name} is listed twice")
            documented[name] = ast.literal_eval(default)
        fields = {f.name: f.default for f in dataclasses.fields(Thresholds)}
        self.assertEqual(sorted(set(fields) - set(documented)), [], "parameters missing from thresholds.md")
        self.assertEqual(sorted(set(documented) - set(fields)), [], "thresholds.md lists parameters the code no longer has")
        self.assertEqual({k: v for k, v in documented.items() if v != fields[k]}, {}, "documented default differs from the code")

    def test_thresholds_reference_lists_every_derived_value(self):
        text = read(ROOT / "docs/reference/publisher/thresholds.md")
        rows = re.findall(r"^\| `(\w+)` \| `\w+` − 0\.01 \(([\d.]+)\) \|", text, re.M)
        derived = {name for name, value in vars(Thresholds).items() if isinstance(value, property)}
        self.assertEqual(sorted(name for name, _ in rows), sorted(derived))
        for name, value in rows:
            self.assertEqual(getattr(Thresholds(), name), float(value), name)

    def test_cost_profile_table_matches_the_code(self):
        from publisher.governor.cost_profiles import profile_values
        text = read(ROOT / "docs/reference/publisher/thresholds.md")
        rows = re.findall(r"^\| (\w+) \| `(\w+)` \| `([^`]+)` \| `([^`]+)` \| `([^`]+)` \|", text, re.M)
        documented_groups = {}
        for group, field, conservative, balanced, aggressive in rows:
            documented_groups.setdefault(group, []).append(field)
            for name, value in (("conservative", conservative), ("balanced", balanced), ("aggressive", aggressive)):
                self.assertEqual(ast.literal_eval(value), profile_values(name)[field], f"{field} {name}")
        self.assertEqual(documented_groups, {g: list(f) for g, f in rc.COST_PROFILE_GROUPS.items()})

    def test_product_constants_reference_matches_the_code(self):
        from publisher.config.defaults import PRODUCT_CONSTANTS
        text = read(ROOT / "docs/reference/publisher/product-constants.md")
        documented = {name: ast.literal_eval(value) for name, value in re.findall(r"^\| `([A-Z_]+)` \| `([^`]+)` \|", text, re.M)}
        self.assertEqual(documented, dict(PRODUCT_CONSTANTS))
        lowered = {name.lower() for name in PRODUCT_CONSTANTS}
        self.assertEqual(sorted(lowered & {f.name for f in dataclasses.fields(Thresholds)}), [], "a product constant is also a policy field")

    def test_blocker_reference_matches_the_code(self):
        text = read(ROOT / "docs/reference/publisher/blocker-codes.md")
        documented = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", text, re.M))
        self.assertGreater(len(documented), 20)
        source = "\n".join(read(path) for path in sorted((ROOT / "src" / "publisher").rglob("*.py")))
        self.assertEqual(sorted(c for c in documented if c not in source), [], "documented blockers the code no longer has")
        evidence = list(demo_evidence()) + list(synthetic_demo_evidence())
        for name in case_names():
            evidence += [evidence_from_dict(x) for x in load_case(name).get("evidence") or []]
        emitted = set()
        # One build per evidence item: fixtures reuse warehouse identities, so they must not be merged into one run.
        for item in evidence:
            for package in publisher.build_recommendation_packages([item], Thresholds()):
                for candidate in package.to_dict()["candidates"]:
                    emitted.update(candidate["blockers"])
        self.assertEqual(sorted(emitted - documented), [], "blockers emitted by the fixtures but not documented")

    def test_reporter_contract_provenance_row_matches_the_code(self):
        text = read(ROOT / "docs/contracts/shared/reporter-json-contract.md")
        match = re.search(r"\| `contract_version` \| Must equal the contract version of this document \(`(\d+\.\d+\.\d+)`\)", text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), rc.CONTRACT_VERSION)


    def _core_lines(self):
        for key, path in CORE.items():
            for number, line in enumerate(read(path).splitlines(), 1):
                yield key, number, line

    def test_design_docs_carry_no_approval_status_or_competing_precedence_text(self):
        pattern = re.compile(
            r"Approved / locked|^\**Last updated|^\**Approved (PRD|HLD|LLD)|Status:\s*Approved|"
            r"order of authority|When documents disagree|Where an implementation document is stale"
        )
        found = [f"{k}:{n}: {line.strip()[:80]}" for k, n, line in self._core_lines() if pattern.search(line)]
        self.assertEqual(found, [], "precedence and ownership live only in docs/README.md")

    def test_design_docs_do_not_restate_workbook_or_signed_value_details(self):
        pattern = re.compile(r"\b\d+ columns\b|positive[- ]magnitude")
        found = [f"{k}:{n}: {line.strip()[:80]}" for k, n, line in self._core_lines() if pattern.search(line)]
        self.assertEqual(found, [], "column counts and presentation rules live in docs/contracts/publisher/workbook-contract.md")

    def test_design_docs_carry_no_version_stamps_or_internal_ids(self):
        pattern = re.compile(r"\b[Pp]ublisher v\d|\bv2\.\d+\.\d+\b|\baudit M\d|\bM9\b|\b[GK]\d{1,2}\b")
        # Reporter contract versions are stamped on purpose and checked against the code by the version test above.
        contract = re.compile(REPORTER_VERSION.pattern + r"|JSON Schema v\d+\.\d+\.\d+")
        found = [f"{k}:{n}: {line.strip()[:80]}" for k, n, line in self._core_lines() if pattern.search(contract.sub("", line))]
        self.assertEqual(found, [], "versions live in code constants; findings live in the tracker")

    def test_lld_hierarchy_diagram_matches_the_schema(self):
        text = read(CORE["lld"])
        block = re.search(r"### 22\.1 .*?```mermaid\n(.*?)```", text, re.S).group(1)
        drawn = set(re.findall(r"RecommendationPackage --> \w+: (\w+)", block))
        drawn |= {w for w in re.findall(r"^\s+(\w+)\s*$", block, re.M) if w not in {"RecommendationPackage"}}
        schema = json.loads(read(ROOT / "schema" / "recommendation-package.schema.json"))
        self.assertEqual(sorted(drawn), sorted(schema["required"]))


if __name__ == "__main__":
    unittest.main()
