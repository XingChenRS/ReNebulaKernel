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
import verify_release  # noqa: E402


class VerifyReleaseTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def release_id(self, family_id="android14-6.1"):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        return next(item["id"] for item in registry["releases"] if item["family_id"] == family_id)

    def write_inputs(self, directory, plan):
        base = plan["version"]["expected_base_release"]
        version, patchlevel, sublevel = base.split(".")
        makefile = directory / "Makefile"
        makefile.write_text(f"VERSION = {version}\nPATCHLEVEL = {patchlevel}\nSUBLEVEL = {sublevel}\n", encoding="utf-8")
        plan_path = directory / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return plan_path, makefile

    def test_kleaf_image_accepts_google_middle_segment(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "kernelsu")
        variant = next(item for item in plan["variants"] if item["id"] == "builtin-image")
        release = plan["version"]["expected_base_release"] + "-gki-test" + variant["version"]["local_suffix"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, makefile = self.write_inputs(root, plan)
            image = root / "Image"
            image.write_bytes(f"Linux version {release}\0".encode())
            result = verify_release.main([
                "--plan", str(plan_path), "--variant-id", "builtin-image",
                "--makefile", str(makefile), "--image", str(image),
            ])
            self.assertEqual(result, 0)

    def test_double_base_prefix_is_rejected(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "none")
        variant = plan["variants"][0]
        base = plan["version"]["expected_base_release"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, makefile = self.write_inputs(root, plan)
            image = root / "Image"
            image.write_bytes(f"Linux version {base}-gki-{base}{variant['version']['local_suffix']}\0".encode())
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main([
                    "--plan", str(plan_path), "--variant-id", "baseline-image",
                    "--makefile", str(makefile), "--image", str(image),
                ])
            self.assertEqual(result, 2)

    def test_legacy_release_accepts_google_scm_segment(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id("android13-5.15"), "kernelsu"
        )
        variant = next(item for item in plan["variants"] if item["id"] == "lkm-module")
        expected = (
            plan["version"]["expected_base_release"]
            + "-android13-8-g61d896ee2a80-dirty"
            + variant["version"]["local_suffix"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, makefile = self.write_inputs(root, plan)
            header = root / "utsrelease.h"
            header.write_text(f'#define UTS_RELEASE "{expected}"\n', encoding="utf-8")
            self.assertEqual(verify_release.main([
                "--plan", str(plan_path), "--variant-id", "lkm-module",
                "--makefile", str(makefile), "--utsrelease-header", str(header),
            ]), 0)

    def test_record_uses_schema_5_selection_and_variant(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id(), "resukisu", susfs=True, uname_tag="lab"
        )
        variant = next(item for item in plan["variants"] if item["id"] == "builtin-image")
        release = plan["version"]["expected_base_release"] + "-gki" + variant["version"]["local_suffix"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, makefile = self.write_inputs(root, plan)
            image = root / "Image"
            image.write_bytes(f"Linux version {release}\0".encode())
            record = root / "record.json"
            self.assertEqual(verify_release.main([
                "--plan", str(plan_path), "--variant-id", "builtin-image",
                "--makefile", str(makefile), "--image", str(image), "--record", str(record),
            ]), 0)
            data = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], 5)
            self.assertEqual(data["variant_id"], "builtin-image")
            self.assertEqual(data["selection"]["root_source"], "resukisu")
            self.assertTrue(data["selection"]["susfs"])

    def test_unknown_variant_is_rejected(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "kernelsu")
        with self.assertRaises(verify_release.ContractError):
            verify_release.validate_plan(plan, "baseline-image", plan["version"]["expected_base_release"])


if __name__ == "__main__":
    unittest.main()
