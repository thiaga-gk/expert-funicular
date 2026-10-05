"""Documentation guards: links resolve, contract versions match the code, cited headings exist.

Scope is the four design documents, docs/README.md, README.md, CLAUDE.md, contracts/README.md, docs/contracts/ and docs/reference/. It never globs `**`,
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
from contracts import reporter_contract as rc  # noqa: E402
from contracts import workbook_contract as wc  # noqa: E402
import publisher  # noqa: E402
from tests.golden import case_names, load_case  # noqa: E402

CORE = {
    "prd": ROOT / "docs/product/prd.md",
    "context": ROOT / "docs/architecture/context.md",
    "hld": ROOT / "docs/architecture/hld.md",
    "lld": ROOT / "docs/architecture/lld.md",
}
SCOPE = (
    list(CORE.values())
    + [ROOT / "docs/README.md", ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "contracts/README.md"]
    + sorted((ROOT / "docs/contracts").glob("*.md"))
    + sorted((ROOT / "docs/reference").glob("*.md"))
)
FENCE = re.compile(r"```.*?```", re.S)
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
BACKTICK = re.compile(r"`([^`\n]+)`")
REPO_PATH = re.compile(r"^(docs|contracts|reporters|schema|tests|tools|pricing|examples)/[\w./-]+$")
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
                if version != publisher.PUBLISHER_VERSION:
                    wrong.append(f"{path.relative_to(ROOT)}: publisher {version} != {publisher.PUBLISHER_VERSION}")
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
        text = read(ROOT / "docs/reference/thresholds.md")
        rows = re.findall(r"^\| `(\w+)` \| `([^`]+)` \|", text, re.M)
        documented = {}
        for name, default in rows:
            self.assertNotIn(name, documented, f"{name} is listed twice")
            documented[name] = ast.literal_eval(default)
        fields = {f.name: f.default for f in dataclasses.fields(publisher.Thresholds)}
        self.assertEqual(sorted(set(fields) - set(documented)), [], "parameters missing from thresholds.md")
        self.assertEqual(sorted(set(documented) - set(fields)), [], "thresholds.md lists parameters the code no longer has")
        self.assertEqual({k: v for k, v in documented.items() if v != fields[k]}, {}, "documented default differs from the code")

    def test_blocker_reference_matches_the_code(self):
        text = read(ROOT / "docs/reference/blocker-codes.md")
        documented = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", text, re.M))
        self.assertGreater(len(documented), 20)
        source = read(ROOT / "publisher.py")
        self.assertEqual(sorted(c for c in documented if c not in source), [], "documented blockers the code no longer has")
        evidence = list(publisher.demo_evidence()) + list(publisher.synthetic_demo_evidence())
        for name in case_names():
            evidence += [publisher.evidence_from_dict(x) for x in load_case(name).get("evidence") or []]
        emitted = set()
        # One build per evidence item: fixtures reuse warehouse identities, so they must not be merged into one run.
        for item in evidence:
            for package in publisher.build_recommendation_packages([item], publisher.Thresholds()):
                for candidate in package.to_dict()["candidates"]:
                    emitted.update(candidate["blockers"])
        self.assertEqual(sorted(emitted - documented), [], "blockers emitted by the fixtures but not documented")

    def test_reporter_contract_provenance_row_matches_the_code(self):
        text = read(ROOT / "docs/contracts/reporter-json-contract.md")
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
        self.assertEqual(found, [], "column counts and presentation rules live in docs/contracts/workbook-contract.md")

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
