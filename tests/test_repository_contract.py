import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import resolve_plan  # noqa: E402
import validate_repository  # noqa: E402


class RepositoryContractTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def test_static_gate_accepts_a_valid_registered_schema2_plan(self):
        registry = json.loads(
            (self.REPO_ROOT / "profiles" / "registry.json").read_text(encoding="utf-8")
        )
        release_id = registry["releases"][0]["id"]

        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary) / "build-plan.json"
            self.assertEqual(
                resolve_plan.main(
                    [
                        "--repo-root",
                        str(self.REPO_ROOT),
                        "--release-id",
                        release_id,
                        "--output",
                        str(plan),
                    ]
                ),
                0,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                result = validate_repository.main(
                    ["--repo-root", str(self.REPO_ROOT), "--plan", str(plan)]
                )
            self.assertEqual(result, 0)
