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
import resolve_plan  # noqa: E402
from sync_google_gki import canonical_json  # noqa: E402


class RootAdapterTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def make_workspace(self, root):
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
        return workspace

    def fake_checkout(self, workspace):
        checkout = workspace / "KernelSU"
        source = checkout / "kernel"
        source.mkdir(parents=True)
        (source / "Kbuild").write_text("obj-$(CONFIG_KSU) += kernelsu.o\n", encoding="utf-8")
        (source / "Kconfig").write_text('menu "KernelSU"\nendmenu\n', encoding="utf-8")
        return checkout

    def release_id(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        return next(item["id"] for item in registry["releases"] if item["family_id"] == "android14-6.1")

    def test_none_writes_a_record_without_a_checkout(self):
        plan_data = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "none")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "kernel"
            workspace.mkdir()
            plan = root / "plan.json"
            plan.write_bytes(canonical_json(plan_data) + b"\n")
            result = apply_root_adapter.main(
                ["--plan", str(plan), "--variant-id", "baseline-image", "--kernel-workspace", str(workspace)]
            )
            self.assertEqual(result, 0)
            self.assertFalse((workspace / "KernelSU").exists())
            record = json.loads((workspace / "renebula-root-record.json").read_text(encoding="utf-8"))
            self.assertEqual(record["provider"], "none")
            self.assertEqual(record["variant_id"], "baseline-image")

    def test_all_three_providers_use_the_same_immutable_adapter_contract(self):
        locks = self.REPO_ROOT / "locks" / "root-sources.lock.json"
        lock_ids = {
            "kernelsu": "kernelsu.main.4a5f4311",
            "sukisu": "sukisu.main.35467545",
            "resukisu": "resukisu.main.ca62a37f",
        }
        for provider, lock_id in lock_ids.items():
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                workspace = self.make_workspace(Path(temporary))
                checkout = self.fake_checkout(workspace)
                lock = apply_root_adapter.load_root_lock(locks, lock_id)
                with patch.object(apply_root_adapter, "checkout_provider", return_value=checkout), patch.object(
                    apply_root_adapter, "create_symlink"
                ):
                    record = apply_root_adapter.apply_provider(
                        lock,
                        workspace,
                        "lkm-module",
                        apply_root_adapter.KLEAF_FRAGMENT_ADAPTER,
                    )
                self.assertEqual(record["provider"], provider)
                self.assertEqual(record["commit"], lock["commit"])
                self.assertEqual(record["module_out"], "drivers/kernelsu/kernelsu.ko")
                self.assertEqual(record["checkout_mode"], "detached-commit")
                self.assertEqual(
                    (workspace / "common" / "drivers" / "Makefile").read_text(encoding="utf-8").count("kernelsu/"),
                    1,
                )

    def test_only_kleaf_lkm_declares_the_module_output(self):
        lock = apply_root_adapter.load_root_lock(
            self.REPO_ROOT / "locks" / "root-sources.lock.json",
            "kernelsu.main.4a5f4311",
        )
        for variant_id, adapter, expected in (
            ("lkm-module", apply_root_adapter.KLEAF_FRAGMENT_ADAPTER, True),
            ("builtin-image", apply_root_adapter.KLEAF_FRAGMENT_ADAPTER, False),
            ("lkm-module", apply_root_adapter.LEGACY_BUILD_ADAPTER, False),
        ):
            with self.subTest(variant_id=variant_id, adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                workspace = self.make_workspace(Path(temporary))
                checkout = self.fake_checkout(workspace)
                with patch.object(apply_root_adapter, "checkout_provider", return_value=checkout), patch.object(
                    apply_root_adapter, "create_symlink"
                ):
                    record = apply_root_adapter.apply_provider(lock, workspace, variant_id, adapter)
                content = (workspace / "common" / "BUILD.bazel").read_text(encoding="utf-8")
                self.assertEqual("drivers/kernelsu/kernelsu.ko" in content, expected)
                self.assertEqual("module_out" in record, expected)

    def test_plan_rejects_unknown_or_mismatched_variants(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "resukisu")
        with self.assertRaises(apply_root_adapter.AdapterError):
            apply_root_adapter.validate_plan(plan, "baseline-image")
        plan["root"]["id"] = "kernelsu-next"
        with self.assertRaises(apply_root_adapter.AdapterError):
            apply_root_adapter.validate_plan(plan, "builtin-image")

    def test_lock_requires_an_exact_sha(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "locks.json"
            path.write_text(
                json.dumps({"schema": 2, "kind": "static-root-source-locks", "locks": {"bad": {"id": "bad", "provider": "kernelsu", "repository": "https://github.com/tiann/KernelSU.git", "commit": "main", "ref": "main", "source_dir": "kernel", "kernel_license": "GPL-2.0-only"}}}),
                encoding="utf-8",
            )
            with self.assertRaises(apply_root_adapter.AdapterError):
                apply_root_adapter.load_root_lock(path, "bad")


if __name__ == "__main__":
    unittest.main()
