import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_root_adapter  # noqa: E402
from sync_google_gki import canonical_json  # noqa: E402


class RootAdapterTests(unittest.TestCase):
    def make_plan(self, provider="none", linkage="none", hook_mode="none", profile="release"):
        root = {
            "id": provider,
            "adapter": "none" if provider == "none" else "resukisu-driver-link-v1",
            "linkage": linkage,
            "hook_mode": hook_mode,
            "source_lock": None,
            "source": None,
        }
        if provider == "resukisu":
            root.update(
                {
                    "source_lock": "resukisu.main.59c99fdf",
                    "source": {
                        "repository": "https://github.com/ReSukiSU/ReSukiSU.git",
                        "commit": "59c99fdf1735c37681ff18c7ffd7834741dcccbf",
                        "ref": "main",
                    },
                }
            )
        return {
            "schema": 4,
            "selection": {
                "root_provider": provider,
                "root_linkage": linkage,
                "hook_mode": hook_mode,
                "config_profile": profile,
            },
            "root": root,
            "configuration": {"id": profile},
        }

    def test_none_writes_a_provenance_record_without_a_root_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "kernel"
            workspace.mkdir()
            plan = root / "plan.json"
            plan.write_bytes(canonical_json(self.make_plan()) + b"\n")
            with contextlib.redirect_stderr(io.StringIO()):
                result = apply_root_adapter.main(
                    ["--plan", str(plan), "--kernel-workspace", str(workspace)]
                )
            self.assertEqual(result, 0)
            self.assertFalse((workspace / "KernelSU").exists())
            record = json.loads((workspace / "renebula-root-record.json").read_text(encoding="utf-8"))
            self.assertEqual(record, {"schema": 1, "id": "none", "adapter": "none"})

    def test_resukisu_registration_is_idempotent_and_keeps_ref_provenance_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "kernel"
            drivers = workspace / "common" / "drivers"
            drivers.mkdir(parents=True)
            (drivers / "Makefile").write_text("obj-y += base/\n", encoding="utf-8")
            (drivers / "Kconfig").write_text('menu "Drivers"\nendmenu\n', encoding="utf-8")
            (workspace / "common" / "BUILD.bazel").write_text(
                "kernel_aarch64 = {\n"
                '    "module_implicit_outs": get_gki_modules_list("arm64"),\n'
                "}\n",
                encoding="utf-8",
            )
            checkout = workspace / "KernelSU"
            source = checkout / "kernel"
            source.mkdir(parents=True)
            (source / "Kbuild").write_text("obj-$(CONFIG_KSU) += core.o\n", encoding="utf-8")
            (source / "Kconfig").write_text("menu \"KernelSU\"\nendmenu\n", encoding="utf-8")
            lock = {
                "id": "resukisu.main.59c99fdf",
                "repository": "https://github.com/ReSukiSU/ReSukiSU.git",
                "commit": "59c99fdf1735c37681ff18c7ffd7834741dcccbf",
                "ref": "main",
                "source_dir": "kernel",
            }
            with patch.object(apply_root_adapter, "checkout_resukisu", return_value=checkout), patch.object(
                apply_root_adapter, "create_symlink"
            ) as symlink:
                first = apply_root_adapter.apply_resukisu(
                    lock,
                    workspace,
                    root_linkage="lkm",
                    build_adapter=apply_root_adapter.KLEAF_FRAGMENT_ADAPTER,
                )
                second = apply_root_adapter.apply_resukisu(
                    lock,
                    workspace,
                    root_linkage="lkm",
                    build_adapter=apply_root_adapter.KLEAF_FRAGMENT_ADAPTER,
                )
            self.assertEqual(first, second)
            self.assertEqual(first["ref"], "main")
            self.assertEqual(first["checkout_mode"], "detached-commit")
            self.assertEqual(first["kleaf_module_declaration"]["module_out"], "drivers/kernelsu/kernelsu.ko")
            self.assertEqual(symlink.call_count, 2)
            self.assertEqual((drivers / "Makefile").read_text(encoding="utf-8").count("kernelsu/"), 1)
            self.assertEqual((drivers / "Kconfig").read_text(encoding="utf-8").count("drivers/kernelsu/Kconfig"), 1)
            build_content = (workspace / "common" / "BUILD.bazel").read_text(encoding="utf-8")
            self.assertEqual(build_content.count("drivers/kernelsu/kernelsu.ko"), 1)
            self.assertNotIn('"module_implicit_outs": get_gki_modules_list("arm64"),', build_content)

    def test_builtin_and_legacy_lkm_do_not_touch_the_kleaf_module_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "kernel"
            drivers = workspace / "common" / "drivers"
            drivers.mkdir(parents=True)
            (drivers / "Makefile").write_text("obj-y += base/\n", encoding="utf-8")
            (drivers / "Kconfig").write_text('menu "Drivers"\nendmenu\n', encoding="utf-8")
            build_file = workspace / "common" / "BUILD.bazel"
            original = '    "module_implicit_outs": get_gki_modules_list("arm64"),\n'
            build_file.write_text(original, encoding="utf-8")
            checkout = workspace / "KernelSU"
            source = checkout / "kernel"
            source.mkdir(parents=True)
            (source / "Kbuild").write_text("obj-$(CONFIG_KSU) += core.o\n", encoding="utf-8")
            (source / "Kconfig").write_text("menu \"KernelSU\"\nendmenu\n", encoding="utf-8")
            lock = {
                "id": "resukisu.main.59c99fdf",
                "repository": "https://github.com/ReSukiSU/ReSukiSU.git",
                "commit": "59c99fdf1735c37681ff18c7ffd7834741dcccbf",
                "ref": "main",
                "source_dir": "kernel",
            }
            with patch.object(apply_root_adapter, "checkout_resukisu", return_value=checkout), patch.object(
                apply_root_adapter, "create_symlink"
            ):
                builtin_record = apply_root_adapter.apply_resukisu(
                    lock,
                    workspace,
                    root_linkage="builtin",
                    build_adapter=apply_root_adapter.KLEAF_FRAGMENT_ADAPTER,
                )
                legacy_record = apply_root_adapter.apply_resukisu(
                    lock,
                    workspace,
                    root_linkage="lkm",
                    build_adapter=apply_root_adapter.LEGACY_BUILD_ADAPTER,
                )
            self.assertNotIn("kleaf_module_declaration", builtin_record)
            self.assertNotIn("kleaf_module_declaration", legacy_record)
            self.assertEqual(build_file.read_text(encoding="utf-8"), original)

    def test_invalid_provider_is_rejected_before_source_access(self):
        plan = self.make_plan("resukisu", "lkm", "tracepoint", "release")
        plan["selection"]["root_provider"] = "kernelsu-next"
        with self.assertRaises(apply_root_adapter.AdapterError):
            apply_root_adapter.validate_plan(plan)

    def test_lock_requires_fixed_sha_and_main_as_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "locks.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "kind": "static-root-source-locks",
                        "locks": {
                            "bad": {
                                "id": "bad",
                                "provider": "resukisu",
                                "repository": "https://github.com/ReSukiSU/ReSukiSU.git",
                                "commit": "main",
                                "ref": "main",
                                "source_dir": "kernel",
                                "repository_license": "GPL-3.0",
                                "kernel_license": "GPL-2.0-only",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(apply_root_adapter.AdapterError):
                apply_root_adapter.load_root_lock(path, "bad")
