import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import resolve_plan  # noqa: E402
import validate_repository  # noqa: E402
from sync_google_gki import canonical_json  # noqa: E402


class RepositoryContractTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def workflow(self):
        path = self.REPO_ROOT / ".github" / "workflows" / "build.yml"
        return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def release_id(self):
        registry, _ = resolve_plan.load_registry(self.REPO_ROOT)
        return next(item["id"] for item in registry["releases"] if item["family_id"] == "android14-6.1")

    def test_repository_has_one_manually_dispatched_workflow(self):
        workflows = sorted((self.REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertEqual([path.name for path in workflows], ["build.yml"])
        document = self.workflow()
        self.assertEqual(set(document["on"]), {"workflow_dispatch"})

    def test_workflow_exposes_only_the_six_public_inputs(self):
        inputs = self.workflow()["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            list(inputs),
            ["release_id", "root_source", "susfs", "kpm", "vivo_vermagic", "uname_tag"],
        )
        self.assertEqual(inputs["root_source"]["options"], ["none", "kernelsu", "sukisu", "resukisu"])
        self.assertEqual(inputs["root_source"]["default"], "none")
        self.assertIn("android16-6.12-2025-12-r1", inputs["release_id"]["options"])
        self.assertIn("6.12.58", inputs["release_id"]["description"])
        self.assertIn("6.12.92", inputs["release_id"]["description"])
        for name in ("susfs", "kpm", "vivo_vermagic"):
            self.assertEqual(inputs[name]["type"], "boolean")
            self.assertEqual(inputs[name]["default"], "false")
        self.assertIn("6.6", inputs["vivo_vermagic"]["description"])
        for value in inputs.values():
            self.assertTrue(any(ord(char) > 127 for char in value["description"]))

    def test_workflow_help_is_readable_utf8(self):
        inputs = self.workflow()["on"]["workflow_dispatch"]["inputs"]
        for name, value in inputs.items():
            with self.subTest(input=name):
                description = value["description"]
                self.assertNotIn("锛", description)
                self.assertNotIn("閫", description)
                self.assertNotIn("鏋", description)

    def test_workflow_builds_the_verified_variant_matrix(self):
        workflow_text = (self.REPO_ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        self.assertIn("fromJSON(needs.prepare.outputs.variants)", workflow_text)
        self.assertIn("matrix.variant.id", workflow_text)
        self.assertIn("scripts/apply_root_adapter.py", workflow_text)
        self.assertIn("scripts/apply_feature_adapter.py", workflow_text)
        self.assertIn("scripts/configure_variant.py", workflow_text)
        self.assertIn("scripts/verify_release.py", workflow_text)
        self.assertNotIn("root_linkage:", workflow_text.split("jobs:", 1)[0])
        self.assertNotIn("hook_mode:", workflow_text.split("jobs:", 1)[0])

    def test_kpm_compiler_is_explicitly_targeted_to_arm64(self):
        workflow_text = (self.REPO_ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('CC="$clang --target=aarch64-linux-android35"', workflow_text)
        self.assertIn("grep -qx '#define __aarch64__ 1'", workflow_text)
        self.assertIn('cp "$kpm_dir/kernel/include/preset.h" "$kpm_dir/tools/preset.h"', workflow_text)
        self.assertIn('cmp "$kpm_dir/kernel/include/preset.h" "$kpm_dir/tools/preset.h"', workflow_text)
        self.assertIn('"$kpm_dir/tools/kptools" -l -k "$kpm_dir/kernel/kpimg"', workflow_text)
        self.assertIn("grep -qx 'config=android,release'", workflow_text)
        self.assertNotIn('ln -s clang "$clang_bin/aarch64-linux-android35-clang"', workflow_text)

    def test_lkm_uses_digest_locked_upstream_ddk_instead_of_in_tree_kleaf(self):
        workflow_text = (self.REPO_ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("使用锁定的 GKI DDK 构建 LKM", workflow_text)
        self.assertEqual(workflow_text.count("ghcr.io/ylarod/ddk-min@sha256:"), 7)
        self.assertIn("docker run --rm --privileged", workflow_text)
        self.assertIn("CONFIG_KSU=m CONFIG_KSU_MULTI_MANAGER_SUPPORT=y CONFIG_KSU_TRACEPOINT_HOOK=y", workflow_text)
        ddk_start = workflow_text.index("docker run --rm --privileged")
        ddk_end = workflow_text.index('test -s "$GITHUB_WORKSPACE/renebula-dist/kernelsu.ko"', ddk_start)
        self.assertIn('2>&1 | tee "$RUNNER_TEMP/build.log"', workflow_text[ddk_start:ddk_end])
        self.assertIn("steps.plan.outputs.artifact == 'image'", workflow_text)
        configure_call = workflow_text.index("python3 scripts/configure_variant.py")
        configure_step = workflow_text[workflow_text.rfind("      - name:", 0, configure_call):configure_call]
        self.assertIn("if: steps.plan.outputs.artifact == 'image'", configure_step)
        self.assertNotIn("--allow_undeclared_modules", workflow_text)

    def test_static_gate_accepts_catalog_and_exact_custom_plan(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(validate_repository.main(["--repo-root", str(self.REPO_ROOT)]), 0)
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT,
            self.release_id(),
            "sukisu",
            susfs=True,
            kpm=True,
            vivo_vermagic=True,
            uname_tag="lab1",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "build-plan.json"
            path.write_bytes(canonical_json(plan) + b"\n")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(validate_repository.main([
                    "--repo-root", str(self.REPO_ROOT), "--plan", str(path)
                ]), 0)

    def test_static_gate_rejects_tampered_plan(self):
        plan = resolve_plan.resolve_plan(self.REPO_ROOT, self.release_id(), "resukisu")
        plan["variants"][0]["root_linkage"] = "lkm"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "build-plan.json"
            path.write_text(json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(validate_repository.main([
                    "--repo-root", str(self.REPO_ROOT), "--plan", str(path)
                ]), 2)


if __name__ == "__main__":
    unittest.main()
