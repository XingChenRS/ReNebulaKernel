import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_kernel_compat_adapter  # noqa: E402


PID_SOURCE = """static struct pid **task_pid_ptr(struct task_struct *task, enum pid_type type)
{
\treturn (type == PIDTYPE_PID) ?
\t\t&task->thread_pid :
\t\t&task->signal->pids[type];
}

struct pid *get_task_pid(struct task_struct *task, enum pid_type type)
{
\tstruct pid *pid;
\trcu_read_lock();
\tpid = get_pid(rcu_dereference(*task_pid_ptr(task, type)));
\trcu_read_unlock();
\treturn pid;
}
EXPORT_SYMBOL_GPL(get_task_pid);

pid_t __task_pid_nr_ns(struct task_struct *task, enum pid_type type,
\t\t\tstruct pid_namespace *ns)
{
\tpid_t nr = 0;

\trcu_read_lock();
\tif (!ns)
\t\tns = task_active_pid_ns(current);
\tnr = pid_nr_ns(rcu_dereference(*task_pid_ptr(task, type)), ns);
\trcu_read_unlock();

\treturn nr;
}
EXPORT_SYMBOL(__task_pid_nr_ns);
"""


class KernelCompatAdapterTests(unittest.TestCase):
    def plan(self, family_id, variant_id, artifact):
        return {
            "schema": 5,
            "selection": {"family_id": family_id},
            "variants": [{"id": variant_id, "artifact": artifact}],
        }

    def workspace(self, root, source=PID_SOURCE):
        workspace = root / "kernel"
        pid_c = workspace / "common" / "kernel" / "pid.c"
        pid_c.parent.mkdir(parents=True)
        pid_c.write_text(source, encoding="utf-8", newline="\n")
        return workspace, pid_c

    def test_android16_612_image_guards_init_task_pid_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, pid_c = self.workspace(Path(temporary))

            record = apply_kernel_compat_adapter.prepare_kernel_compat(
                self.plan("android16-6.12", "builtin-image", "image"),
                "builtin-image",
                workspace,
            )

            content = pid_c.read_text(encoding="utf-8")
            self.assertEqual(record["applied"], ["android16-6.12-init-task-pid-read-v1"])
            self.assertIn("static struct pid *renebula_task_pid_read", content)
            self.assertIn("task == &init_task && pid != &init_struct_pid", content)
            self.assertIn("pr_warn_once(\"ReNebula: repaired corrupted init_task PID reference\\n\")", content)
            self.assertEqual(content.count("renebula_task_pid_read(task, type)"), 2)
            self.assertNotIn(
                "get_pid(rcu_dereference(*task_pid_ptr(task, type)))", content
            )
            self.assertNotIn(
                "pid_nr_ns(rcu_dereference(*task_pid_ptr(task, type)), ns)", content
            )

    def test_lkm_and_other_families_are_noops(self):
        cases = (
            ("android16-6.12", "lkm-module", "module"),
            ("android15-6.6", "builtin-image", "image"),
        )
        for family_id, variant_id, artifact in cases:
            with self.subTest(family_id=family_id, variant_id=variant_id), tempfile.TemporaryDirectory() as temporary:
                workspace, pid_c = self.workspace(Path(temporary))
                original = pid_c.read_bytes()

                record = apply_kernel_compat_adapter.prepare_kernel_compat(
                    self.plan(family_id, variant_id, artifact), variant_id, workspace
                )

                self.assertEqual(record["applied"], [])
                self.assertEqual(pid_c.read_bytes(), original)

    def test_anchor_drift_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            drifted = PID_SOURCE.replace("task_pid_ptr(task, type)", "task_pid_ptr(task, PIDTYPE_PID)", 1)
            workspace, pid_c = self.workspace(Path(temporary), drifted)
            original = pid_c.read_bytes()

            with self.assertRaises(apply_kernel_compat_adapter.CompatError):
                apply_kernel_compat_adapter.prepare_kernel_compat(
                    self.plan("android16-6.12", "builtin-image", "image"),
                    "builtin-image",
                    workspace,
                )

            self.assertEqual(pid_c.read_bytes(), original)

    def test_main_writes_canonical_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, _ = self.workspace(root)
            plan_path = root / "build-plan.json"
            plan_path.write_text(
                json.dumps(self.plan("android16-6.12", "builtin-image", "image")),
                encoding="utf-8",
            )

            result = apply_kernel_compat_adapter.main(
                [
                    "--plan",
                    str(plan_path),
                    "--variant-id",
                    "builtin-image",
                    "--kernel-workspace",
                    str(workspace),
                ]
            )

            self.assertEqual(result, 0)
            record_path = workspace / "renebula-compat-record.json"
            self.assertTrue(record_path.read_bytes().endswith(b"\n"))
            self.assertEqual(
                json.loads(record_path.read_text(encoding="utf-8")),
                {
                    "applied": ["android16-6.12-init-task-pid-read-v1"],
                    "family_id": "android16-6.12",
                    "schema": 1,
                    "variant_id": "builtin-image",
                },
            )

    def test_plan_requires_one_literal_variant(self):
        plan = self.plan("android16-6.12", "builtin-image", "image")
        plan["variants"].append(dict(plan["variants"][0]))
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.workspace(Path(temporary))
            with self.assertRaises(apply_kernel_compat_adapter.CompatError):
                apply_kernel_compat_adapter.prepare_kernel_compat(
                    plan, "builtin-image", workspace
                )


if __name__ == "__main__":
    unittest.main()
