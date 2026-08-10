import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import configure_variant  # noqa: E402
import resolve_plan  # noqa: E402


class ConfigureVariantTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def release_id(self, family_id="android14-6.1"):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        return next(item["id"] for item in registry["releases"] if item["family_id"] == family_id)

    def makefile(self, directory, release):
        version, patchlevel, sublevel = release.split(".")
        path = directory / "Makefile"
        path.write_text(f"VERSION = {version}\nPATCHLEVEL = {patchlevel}\nSUBLEVEL = {sublevel}\n", encoding="utf-8")
        return path

    def test_resukisu_lkm_configuration_is_fixed_and_debug_is_off(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "resukisu")
        lines = configure_variant.compile_kconfig(plan, "lkm-module")
        self.assertIn("CONFIG_KSU=m", lines)
        self.assertIn("CONFIG_KSU_TRACEPOINT_HOOK=y", lines)
        self.assertIn("CONFIG_KSU_MULTI_MANAGER_SUPPORT=y", lines)
        self.assertIn("# CONFIG_KSU_MANUAL_HOOK is not set", lines)
        self.assertIn("# CONFIG_KSU_SUSFS is not set", lines)
        self.assertIn("# CONFIG_KSU_DEBUG is not set", lines)

    def test_sukisu_kpm_is_enabled_only_in_the_builtin_variant(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id(), "sukisu", kpm=True
        )
        self.assertIn("CONFIG_KPM=y", configure_variant.compile_kconfig(plan, "builtin-image"))
        self.assertIn("# CONFIG_KPM is not set", configure_variant.compile_kconfig(plan, "lkm-module"))

    def test_kleaf_backend_writes_one_localversion_fragment(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id(), "kernelsu", uname_tag="lab1"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "renebula"
            record = configure_variant.write_backend_contract(
                plan,
                "builtin-image",
                self.makefile(root, plan["version"]["expected_base_release"]),
                output,
            )
            content = (output / "localversion_defconfig").read_text(encoding="utf-8")
            self.assertIn('CONFIG_LOCALVERSION="-ReNebula-v4-a14-6.1-ksu-bi-lab1"', content)
            self.assertIn("CONFIG_KSU=y", content)
            self.assertEqual(record["variant_id"], "builtin-image")
            self.assertEqual(record["backend"], "kleaf-defconfig-fragment-arm64-v1")

    def test_legacy_backend_writes_post_defconfig_contract(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id("android13-5.15"), "kernelsu"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "renebula"
            record = configure_variant.write_backend_contract(
                plan,
                "lkm-module",
                self.makefile(root, plan["version"]["expected_base_release"]),
                output,
            )
            self.assertTrue((output / "apply-localversion.sh").is_file())
            self.assertIn("POST_DEFCONFIG_CMDS", (output / "localversion.build.config").read_text())
            self.assertEqual(record["backend"], "legacy-build-sh-arm64-v1")

    def test_configuration_rejects_unknown_variant_and_localversion_injection(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "kernelsu")
        with self.assertRaises(configure_variant.ConfigurationError):
            configure_variant.compile_kconfig(plan, "baseline-image")
        plan["variants"][0]["configuration"]["LOCALVERSION"] = "y"
        with self.assertRaises(configure_variant.ConfigurationError):
            configure_variant.compile_kconfig(plan, "builtin-image")


if __name__ == "__main__":
    unittest.main()
