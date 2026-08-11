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

    def release_for(self, family_id):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        return next(item["id"] for item in registry["releases"] if item["family_id"] == family_id)

    def test_registry_exposes_only_the_four_root_sources(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        self.assertEqual(
            [item["id"] for item in registry["root_sources"]],
            ["none", "kernelsu", "sukisu", "resukisu"],
        )
        self.assertNotIn("root_linkages", registry)
        self.assertNotIn("hook_modes", registry)
        self.assertNotIn("config_profiles", registry)

    def test_plain_requests_resolve_deterministically_for_every_kmi(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        for release in registry["releases"]:
            for root_source in ("none", "kernelsu", "sukisu", "resukisu"):
                with self.subTest(release=release["id"], root_source=root_source):
                    first = resolve_plan.resolve_plan(
                        self.REPO_ROOT, release["id"], root_source
                    )
                    second = resolve_plan.resolve_plan(
                        self.REPO_ROOT, release["id"], root_source
                    )
                    self.assertEqual(first, second)
                    self.assertEqual(first["schema"], 5)
                    expected = (
                        ["baseline-image"]
                        if root_source == "none"
                        else ["builtin-image", "lkm-module"]
                    )
                    self.assertEqual([item["id"] for item in first["variants"]], expected)
                    self.assertEqual(first["selection"]["root_source"], root_source)
                    if root_source != "none":
                        self.assertRegex(first["root"]["source"]["commit"], r"^[0-9a-f]{40}$")

    def test_android16_6_12_58_is_a_complete_locked_snapshot(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT,
            "android16-6.12-2025-12-r1",
            "resukisu",
            susfs=True,
        )

        self.assertEqual(plan["selection"]["family_id"], "android16-6.12")
        self.assertEqual(plan["version"]["expected_base_release"], "6.12.58")
        self.assertEqual(
            plan["source"]["common_commit"],
            "67fe3c9df146f5752b3cd5c69c8e0460221a8018",
        )

    def test_features_require_a_root_source(self):
        release_id = self.release_for("android14-6.1")
        for feature in ("susfs", "kpm", "vivo_vermagic"):
            kwargs = {feature: True}
            with self.subTest(feature=feature), self.assertRaises(resolve_plan.PlanError):
                resolve_plan.resolve_plan(self.REPO_ROOT, release_id, "none", **kwargs)

    def test_vivo_is_allowed_through_6_1_and_rejected_on_6_6_or_newer(self):
        for family_id in ("android12-5.10", "android13-5.10", "android13-5.15", "android14-5.15", "android14-6.1"):
            plan = resolve_plan.resolve_plan(
                self.REPO_ROOT,
                self.release_for(family_id),
                "resukisu",
                vivo_vermagic=True,
            )
            self.assertFalse(plan["variants"][0]["features"]["vivo_vermagic"])
            self.assertTrue(plan["variants"][1]["features"]["vivo_vermagic"])
        for family_id in ("android15-6.6", "android16-6.12", "android17-6.18"):
            with self.subTest(family_id=family_id), self.assertRaisesRegex(
                resolve_plan.PlanError, "vivo_vermagic.*5.10, 5.15, or 6.1"
            ):
                resolve_plan.resolve_plan(
                    self.REPO_ROOT,
                    self.release_for(family_id),
                    "resukisu",
                    vivo_vermagic=True,
                )

    def test_susfs_is_provider_wide_but_kpm_requires_the_sukisu_bridge(self):
        release_id = self.release_for("android14-6.1")
        for root_source in ("kernelsu", "sukisu", "resukisu"):
            plan = resolve_plan.resolve_plan(
                self.REPO_ROOT,
                release_id,
                root_source,
                susfs=True,
            )
            builtin, lkm = plan["variants"]
            self.assertTrue(builtin["features"]["susfs"])
            self.assertFalse(lkm["features"]["susfs"])
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, release_id, "sukisu", kpm=True
        )
        builtin, lkm = plan["variants"]
        self.assertTrue(builtin["features"]["kpm"])
        self.assertEqual(builtin["configuration"]["KPM"], "y")
        self.assertFalse(lkm["features"]["kpm"])
        for root_source in ("kernelsu", "resukisu"):
            with self.subTest(root_source=root_source), self.assertRaisesRegex(
                resolve_plan.PlanError, "KPM.*SukiSU"
            ):
                resolve_plan.resolve_plan(
                    self.REPO_ROOT, release_id, root_source, kpm=True
                )
        release_id = self.release_for("android17-6.18")
        for feature in ("susfs", "kpm"):
            with self.subTest(feature=feature), self.assertRaises(resolve_plan.PlanError):
                resolve_plan.resolve_plan(
                    self.REPO_ROOT,
                    release_id,
                    "resukisu",
                    **{feature: True},
                )

    def test_kernelsu_next_is_always_rejected(self):
        with self.assertRaises(resolve_plan.PlanError):
            resolve_plan.resolve_plan(
                self.REPO_ROOT,
                self.release_for("android14-6.1"),
                "kernelsu-next",
            )

    def test_uname_tag_is_normalized_and_bounded_for_every_variant(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT,
            self.release_for("android14-6.1"),
            "sukisu",
            susfs=True,
            kpm=True,
            vivo_vermagic=True,
            uname_tag="MLXC_RENB",
        )
        self.assertEqual(plan["selection"]["uname_tag"], "MLXC_RENB")
        self.assertEqual(plan["version"]["user_suffix"], "-MLXC_RENB")
        for variant in plan["variants"]:
            self.assertTrue(variant["version"]["local_suffix"].endswith("-MLXC_RENB"))
            self.assertTrue(variant["version"]["managed_suffix"].startswith("-RN4-"))
            self.assertNotIn("a14-6.1", variant["version"]["managed_suffix"])
            final = (
                "6.1.175-android14-11-maybe-dirty"
                + variant["version"]["local_suffix"]
            )
            self.assertLessEqual(len(final), 64)
        for tag in ("-bad", "bad tag", "bad;tag", "6.1.175", "a" * 65):
            with self.subTest(tag=tag), self.assertRaises(resolve_plan.PlanError):
                resolve_plan.resolve_plan(
                    self.REPO_ROOT,
                    self.release_for("android14-6.1"),
                    "sukisu",
                    uname_tag=tag,
                )
        legacy = resolve_plan.resolve_plan(
            self.REPO_ROOT,
            self.release_for("android12-5.10"),
            "sukisu",
            susfs=True,
            kpm=True,
            vivo_vermagic=True,
            uname_tag="MLXC_RENB",
        )
        self.assertTrue(
            all(
                len(legacy["version"]["expected_base_release"])
                + variant["version"]["google_localversion_budget"]
                + len(variant["version"]["local_suffix"])
                <= 64
                for variant in legacy["variants"]
            )
        )

    def test_cli_uses_the_public_schema_5_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "build-plan.json"
            result = resolve_plan.main(
                [
                    "--repo-root", str(self.REPO_ROOT),
                    "--release-id", self.release_for("android14-6.1"),
                    "--root-source", "resukisu",
                    "--susfs", "true",
                    "--kpm", "false",
                    "--vivo-vermagic", "true",
                    "--uname-tag", "lab1",
                    "--output", str(output),
                ]
            )
            self.assertEqual(result, 0)
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["selection"]["root_source"], "resukisu")
            self.assertTrue(plan["features"]["susfs"]["enabled"])
            self.assertTrue(plan["features"]["vivo_vermagic"]["enabled"])


if __name__ == "__main__":
    unittest.main()
