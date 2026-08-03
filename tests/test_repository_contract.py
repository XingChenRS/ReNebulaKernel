import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import resolve_plan  # noqa: E402
import validate_repository  # noqa: E402
from sync_google_gki import canonical_json  # noqa: E402


class RepositoryContractTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def write_plan(self, temporary: Path, *selection: str, uname_suffix: str = "") -> Path:
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, *selection, uname_suffix)
        path = temporary / "build-plan.json"
        path.write_bytes(canonical_json(plan) + b"\n")
        return path

    def test_static_gate_accepts_the_entire_four_dimensional_registry(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(validate_repository.main(["--repo-root", str(self.REPO_ROOT)]), 0)
        tuples = resolve_plan.registered_selector_tuples(self.REPO_ROOT)
        self.assertEqual(len(tuples), 36)

    def test_static_gate_accepts_an_exact_custom_uname_plan(self):
        release_id = next(
            release_id
            for release_id, provider, linkage, hook, profile in resolve_plan.registered_selector_tuples(
                self.REPO_ROOT
            )
            if (provider, linkage, hook, profile)
            == ("resukisu", "lkm", "tracepoint", "debug")
        )
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.write_plan(
                Path(temporary),
                release_id,
                "resukisu",
                "lkm",
                "tracepoint",
                "debug",
                uname_suffix="-lab1",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                result = validate_repository.main(
                    ["--repo-root", str(self.REPO_ROOT), "--plan", str(plan)]
                )
            self.assertEqual(result, 0)

    def test_static_gate_rejects_a_noncanonical_axis_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = self.write_plan(
                Path(temporary),
                "android14-6.1-lts-2026-08-03",
                "resukisu",
                "builtin",
                "tracepoint",
                "release",
            )
            content = plan_path.read_text(encoding="utf-8").replace(
                '"hook_mode":"tracepoint"', '"hook_mode":"manual"', 1
            )
            plan_path.write_text(content, encoding="utf-8", newline="\n")
            with contextlib.redirect_stderr(io.StringIO()):
                result = validate_repository.main(
                    ["--repo-root", str(self.REPO_ROOT), "--plan", str(plan_path)]
                )
            self.assertEqual(result, 2)
