import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_release  # noqa: E402


class VerifyReleaseTests(unittest.TestCase):
    def write_makefile(self, directory: Path, base_release: str) -> Path:
        version, patchlevel, sublevel = base_release.split(".")
        path = directory / "Makefile"
        path.write_text(
            f"VERSION = {version}\nPATCHLEVEL = {patchlevel}\nSUBLEVEL = {sublevel}\n",
            encoding="utf-8",
        )
        return path

    def plan(
        self,
        base_release="6.1.175",
        suffix="-ReNebula-v3-a14-6.1-none",
        adapter="kleaf-defconfig-fragment-arm64-v1",
        provider="none",
        linkage="none",
        hook_mode="none",
        profile="release",
        uname_suffix="",
    ):
        kconfig_lines = []
        resolved_id = "none"
        root = {
            "id": provider,
            "adapter": "none",
            "linkage": linkage,
            "hook_mode": hook_mode,
            "source_lock": None,
            "source": None,
        }
        if provider == "resukisu":
            kconfig_lines = [
                f"CONFIG_KSU={'m' if linkage == 'lkm' else 'y'}",
                "CONFIG_KSU_TRACEPOINT_HOOK=y",
                "CONFIG_KSU_MULTI_MANAGER_SUPPORT=y",
                "# CONFIG_KSU_MANUAL_HOOK is not set",
                "# CONFIG_KSU_SUSFS is not set",
                "CONFIG_KSU_DEBUG=y" if profile == "debug" else "# CONFIG_KSU_DEBUG is not set",
            ]
            resolved_id = f"resukisu-{linkage}-{hook_mode}-{profile}"
            root = {
                "id": "resukisu",
                "adapter": "resukisu-driver-link-v1",
                "linkage": linkage,
                "hook_mode": hook_mode,
                "source_lock": "resukisu.main.59c99fdf",
                "source": {
                    "repository": "https://github.com/ReSukiSU/ReSukiSU.git",
                    "commit": "59c99fdf1735c37681ff18c7ffd7834741dcccbf",
                    "ref": "main",
                },
            }
        managed_suffix = suffix[: -len(uname_suffix)] if uname_suffix else suffix
        release_contract = (
            {"mode": "exact", "expected_uname_release": base_release + suffix}
            if adapter == "legacy-build-sh-arm64-v1"
            else {"mode": "base-prefix-and-suffix", "prefix": base_release, "suffix": suffix}
        )
        return {
            "schema": 4,
            "selection": {
                "release_id": "test-release",
                "root_provider": provider,
                "root_linkage": linkage,
                "hook_mode": hook_mode,
                "config_profile": profile,
                "uname_suffix": uname_suffix,
            },
            "root": root,
            "configuration": {
                "id": profile,
                "resolved_id": resolved_id,
                "kconfig_lines": kconfig_lines,
                "suffix_token": "test",
            },
            "source": {"lock_id": "test-lock"},
            "build": {"adapter": adapter, "image_name": "Image"},
            "version": {
                "expected_base_release": base_release,
                "managed_suffix": managed_suffix,
                "uname_suffix": uname_suffix,
                "local_suffix": suffix,
                "release_contract": release_contract,
            },
        }

    def write_plan(self, directory: Path, **kwargs) -> Path:
        path = directory / "plan.json"
        path.write_text(json.dumps(self.plan(**kwargs)), encoding="utf-8")
        return path

    def test_kleaf_baseline_writes_fragment_and_accepts_google_middle_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            makefile = self.write_makefile(directory, "6.1.175")
            plan = self.write_plan(directory)
            fragment = directory / "renebula" / "localversion_defconfig"
            image = directory / "Image"
            image.write_bytes(b"Linux version 6.1.175-gki-test-ReNebula-v3-a14-6.1-none\x00")
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main(
                    [
                        "--plan", str(plan), "--makefile", str(makefile),
                        "--write-fragment", str(fragment), "--image", str(image),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn('CONFIG_LOCALVERSION="-ReNebula-v3-a14-6.1-none"', fragment.read_text())

    def test_resukisu_lkm_tracepoint_fragment_is_exactly_controlled(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            makefile = self.write_makefile(directory, "6.1.175")
            plan = self.write_plan(
                directory,
                suffix="-ReNebula-v3-a14-6.1-rsu-lkm-tp-rel",
                provider="resukisu",
                linkage="lkm",
                hook_mode="tracepoint",
            )
            fragment = directory / "fragment"
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main(
                    ["--plan", str(plan), "--makefile", str(makefile), "--write-fragment", str(fragment)]
                )
            self.assertEqual(result, 0)
            content = fragment.read_text(encoding="utf-8")
            self.assertIn("CONFIG_KSU=m", content)
            self.assertIn("CONFIG_KSU_TRACEPOINT_HOOK=y", content)
            self.assertIn("# CONFIG_KSU_MANUAL_HOOK is not set", content)
            self.assertIn("# CONFIG_KSU_SUSFS is not set", content)

    def test_resukisu_builtin_debug_uses_builtin_ksu_and_debug(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            makefile = self.write_makefile(directory, "6.1.175")
            plan = self.write_plan(
                directory,
                suffix="-ReNebula-v3-a14-6.1-rsu-bi-tp-dbg",
                provider="resukisu",
                linkage="builtin",
                hook_mode="tracepoint",
                profile="debug",
            )
            fragment = directory / "fragment"
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main(
                    ["--plan", str(plan), "--makefile", str(makefile), "--write-fragment", str(fragment)]
                )
            self.assertEqual(result, 0)
            content = fragment.read_text(encoding="utf-8")
            self.assertIn("CONFIG_KSU=y", content)
            self.assertIn("CONFIG_KSU_DEBUG=y", content)

    def test_legacy_plan_writes_the_exact_post_defconfig_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            makefile = self.write_makefile(directory, "5.15.160")
            suffix = "-ReNebula-v3-a13-5.15-none"
            plan = self.write_plan(
                directory, base_release="5.15.160", suffix=suffix, adapter="legacy-build-sh-arm64-v1"
            )
            script = directory / "renebula" / "apply-localversion.sh"
            build_config = directory / "renebula" / "localversion.build.config"
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main(
                    [
                        "--plan", str(plan), "--makefile", str(makefile),
                        "--write-legacy-script", str(script),
                        "--write-legacy-build-config", str(build_config),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn('LOCALVERSION "-ReNebula-v3-a13-5.15-none"', script.read_text())
            self.assertIn("append_cmd POST_DEFCONFIG_CMDS", build_config.read_text())

    def test_rejects_a_double_base_prefix_in_a_kleaf_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            makefile = self.write_makefile(directory, "6.1.175")
            plan = self.write_plan(directory)
            image = directory / "Image"
            image.write_bytes(b"Linux version 6.1.175-gki-6.1.175-ReNebula-v3-a14-6.1-none\x00")
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main(
                    ["--plan", str(plan), "--makefile", str(makefile), "--image", str(image)]
                )
            self.assertEqual(result, 2)

    def test_safe_uname_suffix_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            makefile = self.write_makefile(directory, "6.1.175")
            managed = "-ReNebula-v3-a14-6.1-rsu-bi-tp-rel"
            plan = self.write_plan(
                directory,
                suffix=managed + "-lab1",
                provider="resukisu",
                linkage="builtin",
                hook_mode="tracepoint",
                uname_suffix="-lab1",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                result = verify_release.main(["--plan", str(plan), "--makefile", str(makefile)])
            self.assertEqual(result, 0)
