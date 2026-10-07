"""The starter prompts of docs/proposals/shared/live-run.md are read from the page by the awk commands the page shows.

The commands are pulled out of the page's "Starter prompts" section and run as printed (system awk), so a heading or
fence edit that breaks extraction fails here, before a live run starts with an empty prompt.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "proposals" / "shared" / "live-run.md"
DOC_ARG = "docs/proposals/shared/live-run.md"
AWK_CALL = re.compile(r"awk '(?P<program>[^']+)' " + re.escape(DOC_ARG))


def _starter_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("\n## Starter prompts")
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else len(text)]


def _extract(program: str) -> str:
    done = subprocess.run(["awk", program, DOC_ARG], cwd=ROOT, capture_output=True, text=True, check=True)
    return done.stdout


@unittest.skipUnless(shutil.which("awk"), "awk is not installed")
class StarterPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.programs = [m.group("program") for m in AWK_CALL.finditer(_starter_section())]

    def test_the_section_shows_three_commands(self):
        self.assertEqual(len(self.programs), 3, "run prompt, run prompt with inputs, weekly prompt")
        self.assertEqual(self.programs[0], self.programs[1])

    def test_run_prompt_is_extracted(self):
        prompt = _extract(self.programs[0])
        self.assertTrue(prompt.strip())
        self.assertTrue(prompt.startswith("Run the live run of the Publisher and the Realizer"), prompt[:80])
        self.assertNotIn("```", prompt)
        self.assertIn("checkpoint B", prompt, "the whole block must be extracted, up to its closing fence")
        self.assertNotIn("Resume the live run", prompt, "the weekly prompt must not leak into the run prompt")

    def test_weekly_prompt_is_extracted_with_its_placeholders(self):
        prompt = _extract(self.programs[2])
        self.assertTrue(prompt.strip())
        self.assertTrue(prompt.startswith("Resume the live run: read CLAUDE.md"), prompt[:80])
        self.assertNotIn("```", prompt)
        for placeholder in ("<date>", "<test warehouse>", "<UTC time>", "<N>"):
            self.assertIn(placeholder, prompt)
        self.assertIn("phase 5", prompt)

    def test_the_sed_placeholders_the_page_substitutes_all_exist_in_the_weekly_prompt(self):
        prompt = _extract(self.programs[2])
        for placeholder in re.findall(r"-e 's\|(<[^|]+>)\|", _starter_section()):
            self.assertIn(placeholder, prompt)


if __name__ == "__main__":
    unittest.main()
