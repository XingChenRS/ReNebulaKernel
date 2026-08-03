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


class ResolvePlanTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]
    LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"
    KLEAF_FRAGMENT_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"

    def registered_releases(self):
        registry = json.loads(
            (self.REPO_ROOT / "profiles" / "registry.json").read_text(encoding="utf-8")
        )
        return registry["releases"]

    def test_every_registered_release_resolves_deterministically(self):
        releases = self.registered_releases()
        self.assertGreaterEqual(len(releases), 1)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            observed_adapters = set()
            for entry in releases:
                with self.subTest(release_id=entry["id"]):
                    first = temporary_path / f"{entry['id']}-first.json"
                    second = temporary_path / f"{entry['id']}-second.json"
                    arguments = [
                        "--repo-root",
                        str(self.REPO_ROOT),
                        "--release-id",
                        entry["id"],
                    ]
                    self.assertEqual(resolve_plan.main(arguments + ["--output", str(first)]), 0)
                    self.assertEqual(resolve_plan.main(arguments + ["--output", str(second)]), 0)
                    self.assertEqual(first.read_bytes(), second.read_bytes())

                    plan = json.loads(first.read_text(encoding="utf-8"))
                    self.assertEqual(plan["schema"], 2)
                    self.assertEqual(plan["selection"]["release_id"], entry["id"])
                    self.assertEqual(plan["selection"]["family_id"], entry["family_id"])
                    self.assertEqual(plan["selection"]["root"], "none")
                    self.assertEqual(plan["selection"]["features"], [])
                    self.assertTrue(plan["source"]["lock_id"])
                    adapter = plan["build"]["adapter"]
                    observed_adapters.add(adapter)
                    contract = plan["version"]["release_contract"]
                    if adapter == self.LEGACY_ADAPTER:
                        self.assertEqual(contract["mode"], "exact")
                        self.assertEqual(
                            contract["expected_uname_release"],
                            plan["version"]["expected_base_release"]
                            + plan["version"]["local_suffix"],
                        )
                    else:
                        self.assertEqual(adapter, self.KLEAF_FRAGMENT_ADAPTER)
                        self.assertEqual(contract["mode"], "base-prefix-and-suffix")
                        self.assertEqual(
                            contract["prefix"], plan["version"]["expected_base_release"]
                        )
                        self.assertEqual(contract["suffix"], plan["version"]["local_suffix"])

                    if entry["family_id"].startswith("android13-"):
                        self.assertEqual(adapter, self.LEGACY_ADAPTER)
                        self.assertEqual(contract["mode"], "exact")

            self.assertEqual(
                observed_adapters,
                {
                    self.LEGACY_ADAPTER,
                    self.KLEAF_FRAGMENT_ADAPTER,
                },
            )

    def test_unknown_release_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "invalid.json"
            with contextlib.redirect_stderr(io.StringIO()):
                result = resolve_plan.main(
                    [
                        "--repo-root",
                        str(self.REPO_ROOT),
                        "--release-id",
                        "unregistered-release",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertFalse(output.exists())
