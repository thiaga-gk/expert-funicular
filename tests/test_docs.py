"""Documentation guards: links resolve, contract versions match the code, cited headings exist.

Scope is the four design documents, docs/README.md, README.md, docs/contracts/ and docs/reference/. It never globs `**`,
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

CORE = {
    "prd": ROOT / "docs/product/prd.md",
    "context": ROOT / "docs/architecture/context.md",
    "hld": ROOT / "docs/architecture/hld.md",
    "lld": ROOT / "docs/architecture/lld.md",
}
SCOPE = (
    list(CORE.values())
    + [ROOT / "docs/README.md", ROOT / "README.md"]
    + sorted((ROOT / "docs/contracts").glob("*.md"))
    + sorted((ROOT / "docs/reference").glob("*.md"))
)
FENCE = re.compile(r"```.*?```", re.S)
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
BACKTICK = re.compile(r"`([^`\n]+)`")
REPO_PATH = re.compile(r"^(docs|contracts|reporters|schema|tests|tools|pricing|examples)/[\w./-]+$")
REPORTER_VERSION = re.compile(r"(?<!Workbook )Reporter(?: JSON)?\s+[Cc]ontract\W{0,6}v?(\d+\.\d+\.\d+)")
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
        for fixture in sorted((ROOT / "tests" / "golden").glob("*.json")):
            evidence += [publisher.evidence_from_dict(x) for x in json.loads(read(fixture)).get("evidence") or []]
        emitted = set()
        for package in publisher.build_recommendation_packages(evidence, publisher.Thresholds()):
            for candidate in package.to_dict()["candidates"]:
                emitted.update(candidate["blockers"])
        self.assertEqual(sorted(emitted - documented), [], "blockers emitted by the fixtures but not documented")

    def test_reporter_contract_provenance_row_matches_the_code(self):
        text = read(ROOT / "docs/contracts/reporter-json-contract.md")
        match = re.search(r"\| `contract_version` \| Must equal the contract version of this document \(`(\d+\.\d+\.\d+)`\)", text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), rc.CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()
