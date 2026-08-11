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


LEGACY_INIT_PGRP = """static int do_set_init_pgrp(void __user *arg)
{
    int err;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
    struct pid *pids[PIDTYPE_MAX] = { 0 };
#endif

    write_lock_irq(&tasklist_lock);
    struct task_struct *p = current->group_leader;
    struct pid *init_group = task_pgrp(&init_task);

    err = -EPERM;
    if (task_session(p) != task_session(&init_task))
        goto out;

    err = 0;
    if (task_pgrp(p) != init_group) {
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
        change_pid(pids, p, PIDTYPE_PGID, init_group);
#else
        change_pid(p, PIDTYPE_PGID, init_group);
#endif
    }

out:
    write_unlock_irq(&tasklist_lock);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
    free_pids(pids);
#endif

    return err;
}
"""


class RootAdapterTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def make_workspace(self, root):
        workspace = root / "kernel"
        drivers = workspace / "common" / "drivers"
        drivers.mkdir(parents=True)
        (drivers / "Makefile").write_text("obj-y += base/\n", encoding="utf-8")
        (drivers / "Kconfig").write_text('menu "Drivers"\nendmenu\n', encoding="utf-8")
        (workspace / "common" / "BUILD.bazel").write_text(
            "define_common_kernels(target_configs = {\n"
            '    "kernel_aarch64": {\n'
            '        "module_implicit_outs": get_gki_modules_list("arm64"),\n'
            "    },\n"
            "})\n",
            encoding="utf-8",
        )
        return workspace

    def fake_checkout(self, workspace):
        checkout = workspace / "KernelSU"
        source = checkout / "kernel"
        source.mkdir(parents=True)
        (source / "Kbuild").write_text(
            "obj-$(CONFIG_KSU) += kernelsu.o\n"
            "KSU_SRC := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))\n",
            encoding="utf-8",
        )
        (source / "Kconfig").write_text('menu "KernelSU"\nendmenu\n', encoding="utf-8")
        dispatch = source / "supercall" / "dispatch.c"
        dispatch.parent.mkdir()
        dispatch.write_text(
            "#include <linux/capability.h>\n"
            "#include <linux/version.h>\n\n"
            + LEGACY_INIT_PGRP
            + "\nstatic int do_get_sulog_fd(void __user *arg)\n{\n    return 0;\n}\n",
            encoding="utf-8",
        )
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

    def test_all_three_providers_and_both_linkages_repair_init_pgrp(self):
        locks = self.REPO_ROOT / "locks" / "root-sources.lock.json"
        lock_ids = {
            "kernelsu": "kernelsu.main.4a5f4311",
            "sukisu": "sukisu.main.35467545",
            "resukisu": "resukisu.main.ca62a37f",
        }
        for provider, lock_id in lock_ids.items():
            for variant_id in ("builtin-image", "lkm-module"):
                with self.subTest(provider=provider, variant_id=variant_id), tempfile.TemporaryDirectory() as temporary:
                    workspace = self.make_workspace(Path(temporary))
                    checkout = self.fake_checkout(workspace)
                    lock = apply_root_adapter.load_root_lock(locks, lock_id)
                    with patch.object(apply_root_adapter, "checkout_provider", return_value=checkout), patch.object(
                        apply_root_adapter, "create_symlink"
                    ):
                        record = apply_root_adapter.apply_provider(
                            lock,
                            workspace,
                            variant_id,
                            apply_root_adapter.KLEAF_FRAGMENT_ADAPTER,
                        )
                    dispatch = (checkout / "kernel" / "supercall" / "dispatch.c").read_text(
                        encoding="utf-8"
                    )
                    self.assertEqual(record["provider"], provider)
                    self.assertEqual(record["commit"], lock["commit"])
                    self.assertEqual(record["checkout_mode"], "detached-commit")
                    self.assertEqual(
                        record["compatibility_adapters"], ["pid0-canonical-pid-v2"]
                    )
                    self.assertNotIn("task_pgrp(&init_task)", dispatch)
                    self.assertNotIn("task_session(&init_task)", dispatch)
                    self.assertNotIn("find_pid_ns(1, &init_pid_ns)", dispatch)
                    self.assertNotIn("get_pid_task(", dispatch)
                    self.assertIn("#ifdef MODULE", dispatch)
                    self.assertIn("struct pid *init_group = task_pid(&init_task);", dispatch)
                    self.assertIn("struct pid *init_group = &init_struct_pid;", dispatch)
                    self.assertIn("task_session(p) != init_group", dispatch)
                    self.assertIn("change_pid(p, PIDTYPE_PGID, init_group);", dispatch)
                    self.assertEqual(dispatch.count("#include <linux/pid.h>"), 1)
                    if variant_id == "lkm-module":
                        self.assertEqual(record["integration"], "external-gki-ddk")
                        self.assertNotIn("provider_metadata_adapter", record)
                        self.assertEqual(
                            (workspace / "common" / "drivers" / "Makefile").read_text(encoding="utf-8").count("kernelsu/"),
                            0,
                        )

    def test_init_pgrp_repair_rejects_source_drift_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            dispatch = Path(temporary) / "dispatch.c"
            original = (
                "#include <linux/capability.h>\n"
                + LEGACY_INIT_PGRP.replace("task_pgrp(&init_task)", "task_pgrp(current)")
            )
            dispatch.write_text(original, encoding="utf-8")

            with self.assertRaises(apply_root_adapter.AdapterError):
                apply_root_adapter.repair_init_pgrp(dispatch)

            self.assertEqual(dispatch.read_text(encoding="utf-8"), original)

    def test_only_builtin_integrates_provider_into_the_kernel_tree(self):
        lock = apply_root_adapter.load_root_lock(
            self.REPO_ROOT / "locks" / "root-sources.lock.json",
            "kernelsu.main.4a5f4311",
        )
        for variant_id, adapter, expected in (
            ("lkm-module", apply_root_adapter.KLEAF_FRAGMENT_ADAPTER, False),
            ("builtin-image", apply_root_adapter.KLEAF_FRAGMENT_ADAPTER, True),
            ("lkm-module", apply_root_adapter.LEGACY_BUILD_ADAPTER, False),
        ):
            with self.subTest(variant_id=variant_id, adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                workspace = self.make_workspace(Path(temporary))
                checkout = self.fake_checkout(workspace)
                with patch.object(apply_root_adapter, "checkout_provider", return_value=checkout), patch.object(
                    apply_root_adapter, "create_symlink"
                ):
                    record = apply_root_adapter.apply_provider(lock, workspace, variant_id, adapter)
                content = (workspace / "common" / "drivers" / "Makefile").read_text(encoding="utf-8")
                self.assertEqual("kernelsu/" in content, expected)
                self.assertEqual(record["integration"] == "in-tree-builtin", expected)

    def test_kleaf_pins_provider_metadata_to_checkout_outside_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "KernelSU" / "kernel"
            source.mkdir(parents=True)
            kbuild = source / "Kbuild"
            kbuild.write_text(
                "KSU_SRC := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))\n",
                encoding="utf-8",
            )

            apply_root_adapter.pin_kleaf_ksu_src(kbuild, source)

            self.assertEqual(
                kbuild.read_text(encoding="utf-8"),
                f"KSU_SRC := {source.resolve().as_posix()}\n",
            )

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
