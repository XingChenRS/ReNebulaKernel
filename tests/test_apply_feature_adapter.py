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

    def test_feature_scope_matches_literal_variants(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT,
            self.release_id(),
            "resukisu",
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
                Path(command[command.index("-o") + 1]).write_bytes(b"patched")
                return ""
            with patch.object(apply_feature_adapter, "run", side_effect=fake_run) as runner:
                record = apply_feature_adapter.patch_kpm_image(tool, kpimg, image, output)
            self.assertEqual(output.read_bytes(), b"patched")
            self.assertEqual(record["output"], str(output))
            command = runner.call_args.args[0]
            self.assertEqual(command[1:4], ["-p", "-i", str(image)])
            self.assertIn(str(kpimg), command)

    def test_vivo_scope_is_rejected_even_if_a_plan_is_tampered(self):
        plan = resolve_plan.resolve_plan(
            self.REPO_ROOT, self.release_id("android15-6.6"), "resukisu"
        )
        plan["variants"][1]["features"]["vivo_vermagic"] = True
        with self.assertRaises(apply_feature_adapter.FeatureError):
            apply_feature_adapter.validate_feature_scope(plan, "lkm-module")


if __name__ == "__main__":
    unittest.main()
