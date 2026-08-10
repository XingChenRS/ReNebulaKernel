import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_feature_adapter  # noqa: E402
import resolve_plan  # noqa: E402


class FeatureAdapterTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def release_id(self, family_id="android14-6.1"):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        return next(item["id"] for item in registry["releases"] if item["family_id"] == family_id)

    def test_vivo_vermagic_inserts_one_token_before_architecture(self):
        with tempfile.TemporaryDirectory() as temporary:
            header = Path(temporary) / "vermagic.h"
            header.write_text(
                '#define VERMAGIC_STRING \\\n\tUTS_RELEASE " " \\\n\tMODULE_VERMAGIC_MODVERSIONS \\\n\tMODULE_ARCH_VERMAGIC \\\n\tMODULE_RANDSTRUCT\n',
                encoding="utf-8",
            )
            record = apply_feature_adapter.apply_vivo_vermagic(header)
            content = header.read_text(encoding="utf-8")
            self.assertIn('"vivo " MODULE_ARCH_VERMAGIC', content)
            self.assertEqual(content.count('"vivo "'), 1)
            self.assertEqual(record["token"], "vivo ")
            with self.assertRaises(apply_feature_adapter.FeatureError):
                apply_feature_adapter.apply_vivo_vermagic(header)

    def test_vivo_module_modinfo_inserts_one_token_before_architecture(self):
        original = (
            b"name=kernelsu\0"
            b"vermagic=6.1.75-android14 SMP preempt mod_unload modversions aarch64\0"
            b"depends=\0"
        )

        updated = apply_feature_adapter.add_vivo_modinfo_token(original)

        self.assertIn(
            b"vermagic=6.1.75-android14 SMP preempt mod_unload modversions vivo aarch64\0",
            updated,
        )
        with self.assertRaises(apply_feature_adapter.FeatureError):
            apply_feature_adapter.add_vivo_modinfo_token(updated)

    def test_feature_scope_matches_literal_variants(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT,
            self.release_id(),
            "sukisu",
            susfs=True,
            kpm=True,
            vivo_vermagic=True,
        )
        builtin = apply_feature_adapter.validate_feature_scope(plan, "builtin-image")
        lkm = apply_feature_adapter.validate_feature_scope(plan, "lkm-module")
        self.assertEqual(builtin, {"susfs": True, "kpm": True, "vivo_vermagic": False})
        self.assertEqual(lkm, {"susfs": False, "kpm": False, "vivo_vermagic": True})
        with self.assertRaises(apply_feature_adapter.FeatureError):
            apply_feature_adapter.validate_feature_scope(plan, "baseline-image")

    def test_susfs_has_explicit_provider_strategies(self):
        self.assertEqual(apply_feature_adapter.susfs_provider_strategy("kernelsu"), "official-kernelsu-patch")
        self.assertEqual(apply_feature_adapter.susfs_provider_strategy("sukisu"), "sukisu-reject-adapter-v1")
        self.assertEqual(apply_feature_adapter.susfs_provider_strategy("resukisu"), "provider-native-integration")
        with self.assertRaises(apply_feature_adapter.FeatureError):
            apply_feature_adapter.susfs_provider_strategy("kernelsu-next")

    def test_android14_6_1_common_reject_is_resolved_against_google_trace_include(self):
        adapter = getattr(apply_feature_adapter, "finish_common_susfs_reject_adapter", None)
        self.assertIsNotNone(adapter)
        with tempfile.TemporaryDirectory() as temporary:
            common = Path(temporary)
            namespace = common / "fs" / "namespace.c"
            namespace.parent.mkdir(parents=True)
            namespace.write_text(
                "#include <linux/fs_context.h>\n"
                "#include <linux/shmem_fs.h>\n"
                "#include <linux/mnt_idmapping.h>\n\n"
                '#include "pnode.h"\n'
                '#include "internal.h"\n'
                "#include <trace/hooks/blk.h>\n\n"
                "/* Maximum number of mounts in a mount namespace */\n"
                "static unsigned int sysctl_mount_max __read_mostly = 100000;\n",
                encoding="utf-8",
            )
            reject = common / "fs" / "namespace.c.rej"
            reject.write_text("locked android14-6.1 reject fixture\n", encoding="utf-8")
            adapter(common, "android14-6.1")
            content = namespace.read_text(encoding="utf-8")
            self.assertIn("#include <linux/susfs_def.h>", content)
            self.assertIn("extern bool susfs_is_current_ksu_domain(void);", content)
            self.assertIn("#include <trace/hooks/blk.h>", content)
            self.assertFalse(reject.exists())

    def test_android16_6_12_common_rejects_follow_locked_google_renames(self):
        adapter = getattr(apply_feature_adapter, "finish_common_susfs_reject_adapter", None)
        self.assertIsNotNone(adapter)
        with tempfile.TemporaryDirectory() as temporary:
            common = Path(temporary)
            fixtures = {
                "fs/exec.c": (
                    "#include <linux/user_events.h>\n"
                    "#include <linux/rseq.h>\n"
                    "#include <linux/ksm.h>\n"
                    "#include <linux/dma-buf.h>\n\n"
                    "#include <linux/uaccess.h>\n"
                ),
                "fs/proc/base.c": (
                    "#include <linux/cn_proc.h>\n"
                    "#include <linux/ksm.h>\n"
                    "#include <linux/cpufreq_times.h>\n"
                    "#include <uapi/linux/lsm.h>\n"
                    "#include <linux/dma-buf.h>\n"
                    "#include <trace/events/oom.h>\n"
                ),
                "fs/proc/task_mmu.c": (
                    "static int show_smap(struct seq_file *m, void *v)\n"
                    "{\n"
                    "\tstruct vm_area_struct *vma = v;\n"
                    "\tstruct mem_size_stats mss = {};\n\n"
                    "\tif (!vma_data_pages(vma))\n"
                    "\t\tgoto show_pad;\n"
                ),
                "security/selinux/hooks.c": (
                    "#define SELINUX_INODE_INIT_XATTRS 1\n\n"
                    "struct selinux_state selinux_state;\n\n"
                    "/*\n"
                    " * ANDROID: selinux_state is part of the KMI, and backporting capabilities into\n"
                ),
            }
            rejects = {
                "fs/exec.c.rej",
                "fs/proc/base.c.rej",
                "fs/proc/task_mmu.c.rej",
                "security/selinux/hooks.c.rej",
            }
            for relative, content in fixtures.items():
                path = common / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            for relative in rejects:
                path = common / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("locked android16-6.12 reject fixture\n", encoding="utf-8")
            adapter(common, "android16-6.12")
            self.assertIn(
                "#include <linux/susfs_def.h>",
                (common / "fs" / "exec.c").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "SUSFS_IS_INODE_SUS_MAP",
                (common / "fs" / "proc" / "task_mmu.c").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "backup_sepolicy",
                (common / "security" / "selinux" / "hooks.c").read_text(encoding="utf-8"),
            )
            self.assertFalse(any(common.rglob("*.rej")))

    def test_kpm_image_patch_uses_explicit_input_and_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "kptools"
            kpimg = root / "kpimg"
            image = root / "Image"
            output = root / "Image-kpm"
            for path in (tool, kpimg, image):
                path.write_bytes(b"fixture")
            def fake_run(command, cwd=None):
                if "-l" in command and "-k" in command:
                    return "[kpimg]\nversion=0xd02\nconfig=android,release\n"
                if "-l" in command and "-i" in command:
                    return "[kernel]\npatched=true\n[kpimg]\nconfig=android,release\n"
                Path(command[command.index("-o") + 1]).write_bytes(b"patched")
                return ""
            with patch.object(apply_feature_adapter, "run", side_effect=fake_run) as runner:
                record = apply_feature_adapter.patch_kpm_image(tool, kpimg, image, output)
            self.assertEqual(output.read_bytes(), b"patched")
            self.assertEqual(record["output"], str(output))
            command = next(
                call.args[0] for call in runner.call_args_list if "-p" in call.args[0]
            )
            self.assertEqual(command[1:4], ["-p", "-i", str(image)])
            self.assertIn(str(kpimg), command)

    def test_kpm_image_patch_rejects_an_unverified_output_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "kptools"
            kpimg = root / "kpimg"
            image = root / "Image"
            output = root / "Image-kpm"
            for path in (tool, kpimg, image):
                path.write_bytes(b"fixture")

            def fake_run(command, cwd=None):
                if "-l" in command and "-k" in command:
                    return "[kpimg]\nconfig=android,release\n"
                if "-l" in command and "-i" in command:
                    return "[kernel]\npatched=false\n"
                output.write_bytes(b"bad-patch")
                return ""

            with patch.object(apply_feature_adapter, "run", side_effect=fake_run):
                with self.assertRaisesRegex(
                    apply_feature_adapter.FeatureError, "patched Android KPM"
                ):
                    apply_feature_adapter.patch_kpm_image(tool, kpimg, image, output)

    def test_kpm_image_patch_rejects_non_android_kpimg(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "kptools"
            kpimg = root / "kpimg"
            image = root / "Image"
            output = root / "Image-kpm"
            for path in (tool, kpimg, image):
                path.write_bytes(b"fixture")
            with patch.object(
                apply_feature_adapter,
                "run",
                return_value="[kpimg]\nversion=0xd02\nconfig=linux,release\n",
            ):
                with self.assertRaisesRegex(
                    apply_feature_adapter.FeatureError, "Android release mode"
                ):
                    apply_feature_adapter.patch_kpm_image(tool, kpimg, image, output)
            self.assertFalse(output.exists())

    def test_kpm_source_adapter_keeps_android_and_removes_only_stale_ap_callbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            makefile = root / "Makefile"
            user_event = root / "user_event.c"
            makefile.write_text(
                "# ifdef ANDROID\n"
                "\tCFLAGS += -DANDROID\n"
                "# endif\n",
                encoding="utf-8",
            )
            user_event.write_text(
                "    #ifdef ANDROID\n"
                "    if (lib_strcmp(safe_event, \"post-fs-data\") == 0) {\n"
                "        log_boot(\"post-fs-data: loading ap package config ...\\n\");\n"
                "        load_ap_package_config();\n"
                "    }\n"
                "    if (lib_strcmp(safe_event, \"boot-completed\") == 0) {\n\n"
                "    }\n"
                "    if (lib_strcmp(safe_event, \"uid_listener\") == 0 && lib_strcmp(safe_args, \"package-list-updated\") == 0) {\n"
                "        int trust_rc = refresh_trusted_manager_state();\n"
                "        log_boot(\"boot-completed: trusted manager refresh rc=%d\\n\", trust_rc);\n"
                "    }\n"
                "    #endif\n"
                "    logki(\"user report event: %s, args: %s\\n\", safe_event, safe_args);\n",
                encoding="utf-8",
            )

            apply_feature_adapter.adapt_kpm_source(makefile, user_event)

            self.assertIn("CFLAGS += -DANDROID", makefile.read_text(encoding="utf-8"))
            adapted = user_event.read_text(encoding="utf-8")
            self.assertNotIn("load_ap_package_config", adapted)
            self.assertNotIn("refresh_trusted_manager_state", adapted)
            self.assertIn("#ifdef ANDROID", adapted)
            self.assertIn("user report event", adapted)

    def test_vivo_scope_is_rejected_even_if_a_plan_is_tampered(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id("android15-6.6"), "resukisu"
        )
        plan["variants"][1]["features"]["vivo_vermagic"] = True
        with self.assertRaises(apply_feature_adapter.FeatureError):
            apply_feature_adapter.validate_feature_scope(plan, "lkm-module")


if __name__ == "__main__":
    unittest.main()
