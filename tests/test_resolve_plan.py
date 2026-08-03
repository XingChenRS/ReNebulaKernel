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

    def test_every_enabled_four_dimensional_tuple_resolves_deterministically(self):
        tuples = resolve_plan.registered_selector_tuples(self.REPO_ROOT)
        self.assertEqual(len(tuples), 36)
        self.assertEqual(len(tuples), len(set(tuples)))

        for release_id, provider, linkage, hook_mode, profile in tuples:
            with self.subTest(
                release_id=release_id,
                provider=provider,
                linkage=linkage,
                hook_mode=hook_mode,
                profile=profile,
            ):
                first = resolve_plan.resolve_plan(
                    self.REPO_ROOT, release_id, provider, linkage, hook_mode, profile
                )
                second = resolve_plan.resolve_plan(
                    self.REPO_ROOT, release_id, provider, linkage, hook_mode, profile
                )
                self.assertEqual(first, second)
                self.assertEqual(first["schema"], 4)
                self.assertEqual(first["selection"]["root_linkage"], linkage)
                self.assertEqual(first["selection"]["hook_mode"], hook_mode)
                self.assertEqual(first["configuration"]["id"], profile)
                self.assertTrue(first["definition"]["root_sources_lock_sha256"])
                if provider == "none":
                    self.assertEqual(linkage, "none")
                    self.assertEqual(hook_mode, "none")
                    self.assertEqual(first["configuration"]["resolved_id"], "none")
                    self.assertEqual(first["configuration"]["kconfig_lines"], [])
                else:
                    self.assertEqual(provider, "resukisu")
                    self.assertIn(linkage, {"lkm", "builtin"})
                    self.assertEqual(hook_mode, "tracepoint")
                    self.assertEqual(first["root"]["adapter"], "resukisu-driver-link-v1")
                    self.assertEqual(
                        first["root"]["source"]["repository"],
                        "https://github.com/ReSukiSU/ReSukiSU.git",
                    )
                    self.assertEqual(first["root"]["source"]["ref"], "main")
                    expected_ksu = f"CONFIG_KSU={'m' if linkage == 'lkm' else 'y'}"
                    self.assertIn(expected_ksu, first["configuration"]["kconfig_lines"])

    def test_android17_only_permits_the_plain_google_tuple(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        entry = next(item for item in registry["releases"] if item["family_id"] == "android17-6.18")
        family, _ = resolve_plan.load_family(self.REPO_ROOT, entry["family_id"])
        self.assertEqual(
            resolve_plan.allowed_selector_combinations(family),
            [("none", "none", "none", "release")],
        )
        with self.assertRaises(resolve_plan.PlanError):
            resolve_plan.resolve_plan(
                self.REPO_ROOT,
                entry["id"],
                "resukisu",
                "builtin",
                "tracepoint",
                "release",
            )

    def test_invalid_or_unselected_axes_are_rejected(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        release_id = registry["releases"][0]["id"]
        cases = (
            ("__select_root_provider__", "none", "none", "release"),
            ("none", "__select_root_linkage__", "none", "release"),
            ("none", "none", "__select_hook_mode__", "release"),
            ("none", "none", "none", "__select_config_profile__"),
            ("kernelsu-next", "builtin", "tracepoint", "release"),
            ("resukisu", "lkm", "manual", "release"),
            ("resukisu", "lkm", "tracepoint", "susfs"),
            ("resukisu", "lkm", "tracepoint", "kpm"),
        )
        for provider, linkage, hook_mode, profile in cases:
            with self.subTest(provider=provider, linkage=linkage, hook_mode=hook_mode, profile=profile):
                with self.assertRaises(resolve_plan.PlanError):
                    resolve_plan.resolve_plan(
                        self.REPO_ROOT,
                        release_id,
                        provider,
                        linkage,
                        hook_mode,
                        profile,
                    )

    def test_cli_preserves_a_safe_append_only_uname_suffix(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        release_id = next(
            item["id"] for item in registry["releases"] if item["family_id"] == "android14-6.1"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "build-plan.json"
            result = resolve_plan.main(
                [
                    "--repo-root",
                    str(self.REPO_ROOT),
                    "--release-id",
                    release_id,
                    "--root-provider",
                    "resukisu",
                    "--root-linkage",
                    "builtin",
                    "--hook-mode",
                    "tracepoint",
                    "--config-profile",
                    "release",
                    "--uname-suffix=-lab1",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(result, 0)
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["selection"]["uname_suffix"], "-lab1")
            self.assertEqual(
                plan["version"]["local_suffix"], plan["version"]["managed_suffix"] + "-lab1"
            )

    def test_unsafe_or_overlong_uname_suffixes_are_rejected(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        release_id = registry["releases"][0]["id"]
        for suffix in ("lab1", "-5.10.260", "-lab;rm", "-" + "a" * 65):
            with self.subTest(suffix=suffix):
                with self.assertRaises(resolve_plan.PlanError):
                    resolve_plan.resolve_plan(
                        self.REPO_ROOT,
                        release_id,
                        "none",
                        "none",
                        "none",
                        "release",
                        suffix,
                    )
