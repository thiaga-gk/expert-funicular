"""Hash-seed and time-zone independence of the Realizer, measured across processes.

``test_single_file_compatibility`` shows that output directories do not matter; this module covers the other claims of
docs/proposals/realizer/repo-reorg-parity.md: the output is identical under different ``PYTHONHASHSEED`` values and
time zones, and the modular engine (src/realizer) equals the frozen oracle (legacy/realizer_single_file/).

A representative subset of the scenarios runs in fresh interpreters, once per (engine, hash seed, time zone), all in
one process each and in parallel (8 processes). Every one of the 8 canonical file maps must equal the others.
"""
import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.regression.realizer import canonical  # noqa: E402

# One scenario per behaviour that could depend on ordering or time: replay through a cut, billing restatement (and its
# reprocessed twin), config drift, rollback, supersession, modified application with scenario scope, a migration plan
# and a policy change.
SUBSET = (
    "replay-through-generation",
    "GR-026",
    "restatement-reprocessed",
    "GR-024",
    "GR-025",
    "GR-022",
    "GR-019",
    "GR-021",
    "GR-034",
)
ENGINES = ("modular", "oracle")
HASH_SEEDS = ("0", "1")
TIME_ZONES = {"UTC": 0, "Asia/Kolkata": 19800}  # TZ -> expected UTC offset in seconds


def _run(job):
    engine, seed, tz = job
    env = dict(os.environ, PYTHONHASHSEED=seed, TZ=tz, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    done = subprocess.run([sys.executable, "-m", "tests.regression.realizer.dump_canonical", engine, *SUBSET],
                          cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{job} failed ({done.returncode}):\n{done.stderr[-3000:]}")
    return job, json.loads(done.stdout)


class SubprocessDeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        jobs = list(product(ENGINES, HASH_SEEDS, TIME_ZONES))
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            cls.results = dict(pool.map(_run, jobs))

    def test_subset_is_representative(self):
        names = set(canonical.scenario_names())
        self.assertTrue(set(SUBSET) <= names)
        self.assertGreaterEqual(len(SUBSET), 6)
        self.assertEqual(len(set(SUBSET)), len(SUBSET))
        replay = [n for n in SUBSET if any(step.get("replay_through") for step in canonical.load_scenario(n)["runs"])]
        self.assertTrue(replay, "the subset needs a replay-through scenario")
        self.assertTrue({"GR-026", "restatement-reprocessed"} <= set(SUBSET))

    def test_environment_variation_took_effect(self):
        """Guards the guard: the processes really ran with the requested seed and zone, and those differ."""
        probes = {}
        for (engine, seed, tz), result in self.results.items():
            meta = result["meta"]
            self.assertEqual((meta["PYTHONHASHSEED"], meta["TZ"]), (seed, tz))
            self.assertEqual(meta["utc_offset_seconds"], TIME_ZONES[tz], f"TZ={tz} not effective in {engine}")
            probes.setdefault(seed, set()).add(meta["hash_probe"])
        self.assertTrue(all(len(v) == 1 for v in probes.values()), "a hash seed must give one hash")
        self.assertEqual(len(probes), len(HASH_SEEDS))
        self.assertNotEqual(probes["0"], probes["1"], "PYTHONHASHSEED did not change string hashing")

    def test_every_process_covers_the_whole_subset_with_output(self):
        for job, result in self.results.items():
            with self.subTest(job=job):
                self.assertEqual(sorted(result["scenarios"]), sorted(SUBSET))
                for name, files in result["scenarios"].items():
                    self.assertTrue(any(rel.startswith("realization/ledger/") for rel in files), name)
                    self.assertTrue({"runs.json", "report.json"} <= set(files), name)

    def test_output_is_identical_across_hash_seeds_and_time_zones(self):
        for engine in ENGINES:
            reference_job = (engine, HASH_SEEDS[0], "UTC")
            reference = self.results[reference_job]["scenarios"]
            for seed, tz in product(HASH_SEEDS, TIME_ZONES):
                with self.subTest(engine=engine, hash_seed=seed, tz=tz):
                    self.assertEqual(self.results[(engine, seed, tz)]["scenarios"], reference)

    def test_modular_engine_equals_oracle_in_every_environment(self):
        for seed, tz in product(HASH_SEEDS, TIME_ZONES):
            with self.subTest(hash_seed=seed, tz=tz):
                self.assertEqual(self.results[("modular", seed, tz)]["scenarios"],
                                 self.results[("oracle", seed, tz)]["scenarios"])

    def test_subprocess_output_equals_the_stored_baseline(self):
        scenarios = self.results[("modular", HASH_SEEDS[0], "UTC")]["scenarios"]
        for name in SUBSET:
            with self.subTest(scenario=name):
                self.assertEqual(canonical.semantic_diff(name, canonical.stored_expected(name), scenarios[name]), [])


if __name__ == "__main__":
    unittest.main()
