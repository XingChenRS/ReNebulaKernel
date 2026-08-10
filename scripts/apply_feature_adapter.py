#!/usr/bin/env python3
"""Apply locked SUSFS, KPM, and Vivo adaptations for one literal variant."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sync_google_gki import canonical_json


VIVO_SERIES = {"5.10", "5.15", "6.1"}
SUSFS_SERIES = {"5.10", "5.15", "6.1", "6.6", "6.12"}
KPM_SERIES = {"5.10", "5.15", "6.1", "6.6", "6.12"}
SUSFS_REPOSITORY = "https://gitlab.com/simonpunk/susfs4ksu.git"
KPM_REPOSITORY = "https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch.git"
SUKISU_REJECTS = {
    "kernel/Kbuild.rej",
    "kernel/core/init.c.rej",
    "kernel/policy/app_profile.h.rej",
}
COMMON_SUSFS_REJECTS = {
    "android14-6.1": {"fs/namespace.c.rej"},
    "android16-6.12": {
        "fs/exec.c.rej",
        "fs/proc/base.c.rej",
        "fs/proc/task_mmu.c.rej",
        "security/selinux/hooks.c.rej",
    },
}


class FeatureError(ValueError):
    """Raised when a feature cannot be applied to its exact plan variant."""


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FeatureError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise FeatureError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise FeatureError(f"{path} must contain a JSON object")
    return value


def run(command: List[str], *, cwd: Optional[Path] = None) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}")
    try:
        return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as error:
        raise FeatureError(f"command failed ({rendered})\n{error.output.strip()}") from error


def validate_feature_scope(plan: Dict[str, Any], variant_id: str) -> Dict[str, bool]:
    if plan.get("schema") != 5:
        raise FeatureError("plan.schema must be 5")
    selection = plan.get("selection")
    variants = plan.get("variants")
    features = plan.get("features")
    root = plan.get("root")
    if not all(isinstance(value, dict) for value in (selection, features, root)):
        raise FeatureError("plan selection, root, and features must be objects")
    if not isinstance(variants, list):
        raise FeatureError("plan.variants must be an array")
    matches = [item for item in variants if isinstance(item, dict) and item.get("id") == variant_id]
    if len(matches) != 1:
        raise FeatureError(f"variant must appear exactly once in plan: {variant_id}")
    variant_features = matches[0].get("features")
    if not isinstance(variant_features, dict) or set(variant_features) != {
        "susfs", "kpm", "vivo_vermagic"
    }:
        raise FeatureError("variant feature contract is invalid")
    if not all(isinstance(value, bool) for value in variant_features.values()):
        raise FeatureError("variant feature values must be booleans")
    series = selection.get("kernel_series")
    provider = selection.get("root_source")
    if root.get("id") != provider:
        raise FeatureError("plan root does not match selected root source")
    if provider == "none" and any(variant_features.values()):
        raise FeatureError("baseline variant cannot carry root features")
    if variant_features["susfs"] and (variant_id != "builtin-image" or series not in SUSFS_SERIES):
        raise FeatureError("SUSFS is a built-in feature supported only through 6.12")
    if variant_features["kpm"] and (variant_id != "builtin-image" or series not in KPM_SERIES):
        raise FeatureError("KPM is a built-in Image feature supported only through 6.12")
    if variant_features["kpm"] and provider != "sukisu":
        raise FeatureError("KPM requires the SukiSU in-kernel bridge")
    if variant_features["vivo_vermagic"] and (
        variant_id != "lkm-module" or series not in VIVO_SERIES
    ):
        raise FeatureError("Vivo vermagic is an LKM feature supported only on 5.10, 5.15, and 6.1")
    for name, enabled in variant_features.items():
        top = features.get(name)
        if not isinstance(top, dict) or not isinstance(top.get("enabled"), bool):
            raise FeatureError(f"plan.features.{name} is invalid")
        if enabled and not top["enabled"]:
            raise FeatureError(f"variant enables unrequested feature: {name}")
    return dict(variant_features)


def apply_vivo_vermagic(path: Path) -> Dict[str, str]:
    """Insert exactly one ``vivo `` token before build-derived architecture magic."""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read vermagic header {path}: {error}") from error
    if '"vivo "' in content:
        raise FeatureError("Vivo vermagic token is already present")
    matches = list(re.finditer(r"(?m)^(?P<indent>[ \t]*)MODULE_ARCH_VERMAGIC(?P<tail>[ \t]*\\?)$", content))
    if len(matches) != 1:
        raise FeatureError("vermagic header must contain exactly one MODULE_ARCH_VERMAGIC line")
    match = matches[0]
    replacement = f'{match.group("indent")}"vivo " MODULE_ARCH_VERMAGIC{match.group("tail")}'
    path.write_text(content[: match.start()] + replacement + content[match.end() :], encoding="utf-8", newline="\n")
    return {"path": str(path), "token": "vivo ", "anchor": "MODULE_ARCH_VERMAGIC"}


def susfs_provider_strategy(provider: str) -> str:
    strategies = {
        "kernelsu": "official-kernelsu-patch",
        "sukisu": "sukisu-reject-adapter-v1",
        "resukisu": "provider-native-integration",
    }
    try:
        return strategies[provider]
    except KeyError as error:
        raise FeatureError(f"SUSFS has no provider adapter for {provider}") from error


def checkout_locked(source: Dict[str, Any], destination: Path, expected_repository: str) -> Path:
    if source.get("repository") != expected_repository:
        raise FeatureError("feature source repository is not the audited upstream")
    commit = source.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise FeatureError("feature source commit is not immutable")
    if destination.exists():
        raise FeatureError(f"refusing to replace feature checkout: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", str(destination)])
    run(["git", "-C", str(destination), "remote", "add", "origin", source["repository"]])
    run(["git", "-C", str(destination), "fetch", "--depth=1", "--no-tags", "origin", commit])
    run(["git", "-C", str(destination), "checkout", "--detach", "--quiet", commit])
    if run(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip() != commit:
        raise FeatureError("feature checkout does not match its lock")
    return destination


def apply_git_patch(repository: Path, patch: Path) -> None:
    if not patch.is_file():
        raise FeatureError(f"locked patch is missing: {patch}")
    run(["git", "-C", str(repository), "apply", "--check", str(patch)])
    run(["git", "-C", str(repository), "apply", str(patch)])


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read {path}: {error}") from error
    if content.count(old) != 1:
        raise FeatureError(f"SUSFS compatibility anchor drifted: {label}")
    path.write_text(content.replace(old, new, 1), encoding="utf-8", newline="\n")


def finish_common_susfs_reject_adapter(common: Path, family: str) -> None:
    """Resolve only the rejects audited against the locked Google snapshots."""

    expected_rejects = COMMON_SUSFS_REJECTS.get(family)
    if expected_rejects is None:
        raise FeatureError(f"SUSFS has no Google common reject adapter for {family}")
    actual_rejects = {
        path.relative_to(common).as_posix()
        for path in common.rglob("*.rej")
    }
    if actual_rejects != expected_rejects:
        raise FeatureError(
            f"unexpected {family} Google common SUSFS reject set: {sorted(actual_rejects)}"
        )

    if family == "android14-6.1":
        namespace = common / "fs" / "namespace.c"
        replace_once(
            namespace,
            '#include <linux/mnt_idmapping.h>\n\n#include "pnode.h"\n',
            '#include <linux/mnt_idmapping.h>\n'
            '#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n'
            '#include <linux/susfs_def.h>\n'
            '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n\n'
            '#include "pnode.h"\n',
            "android14-6.1 namespace SUSFS include",
        )
        replace_once(
            namespace,
            '#include "internal.h"\n#include <trace/hooks/blk.h>\n\n'
            '/* Maximum number of mounts in a mount namespace */\n',
            '#include "internal.h"\n#include <trace/hooks/blk.h>\n\n'
            '#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n'
            'extern bool susfs_is_current_ksu_domain(void);\n'
            'extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;\n\n'
            '#define CL_COPY_MNT_NS BIT(25) /* used by copy_mnt_ns() */\n\n'
            '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n\n'
            '/* Maximum number of mounts in a mount namespace */\n',
            "android14-6.1 namespace SUSFS declarations",
        )
    else:
        replace_once(
            common / "fs" / "exec.c",
            '#include <linux/ksm.h>\n#include <linux/dma-buf.h>\n',
            '#include <linux/ksm.h>\n'
            '#ifdef CONFIG_KSU_SUSFS\n'
            '#include <linux/susfs_def.h>\n'
            '#endif\n'
            '#include <linux/dma-buf.h>\n',
            "android16-6.12 exec SUSFS include",
        )
        replace_once(
            common / "fs" / "proc" / "base.c",
            '#include <linux/cpufreq_times.h>\n#include <uapi/linux/lsm.h>\n',
            '#include <linux/cpufreq_times.h>\n'
            '#if defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)\n'
            '#include <linux/susfs_def.h>\n'
            '#endif // #if defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)\n\n'
            '#include <uapi/linux/lsm.h>\n',
            "android16-6.12 proc base SUSFS include",
        )
        replace_once(
            common / "fs" / "proc" / "task_mmu.c",
            '\tstruct mem_size_stats mss = {};\n\n\tif (!vma_data_pages(vma))\n',
            '\tstruct mem_size_stats mss = {};\n\n'
            '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
            '\tif (vma->vm_file) {\n'
            '\t\tif (SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
            '\t\t\treturn 0;\n'
            '\t}\n'
            '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n\n'
            '\tif (!vma_data_pages(vma))\n',
            "android16-6.12 show_smap data-page rename",
        )
        replace_once(
            common / "security" / "selinux" / "hooks.c",
            'struct selinux_state selinux_state;\n\n/*\n',
            'struct selinux_state selinux_state;\n'
            '#ifdef CONFIG_KSU_SUSFS\n'
            'extern struct selinux_policy *backup_sepolicy;\n'
            'extern bool ksu_selinux_hide_running __read_mostly;\n'
            'extern int security_context_to_sid_with_policy(struct selinux_policy *policy, const char *scontext, u32 scontext_len,\n'
            '                                               u32 *sid, u32 def_sid, gfp_t gfp_flags);\n'
            '#endif // #ifdef CONFIG_KSU_SUSFS\n\n'
            '/*\n',
            "android16-6.12 SELinux KMI comment",
        )

    for relative in sorted(expected_rejects):
        (common / relative).unlink()


def apply_common_susfs_patch(common: Path, patch: Path, family: str) -> str:
    if family not in COMMON_SUSFS_REJECTS:
        apply_git_patch(common, patch)
        return "strict-git-apply"
    process = subprocess.run(
        ["git", "-C", str(common), "apply", "--reject", "--whitespace=fix", str(patch)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(process.stdout, end="")
    if process.returncode != 1:
        raise FeatureError(
            f"locked {family} SUSFS patch must produce its audited Google common rejects"
        )
    finish_common_susfs_reject_adapter(common, family)
    run(["git", "-C", str(common), "diff", "--check"])
    return "audited-google-reject-adapter-v1"


def finish_sukisu_reject_adapter(provider_checkout: Path) -> None:
    """Resolve the three audited rejects produced by the official KSU patch.

    The official SUSFS patch cleanly applies to every other SukiSU file at the
    locked commit.  These replacements deliberately bind the remaining hook
    transition to exact source anchors instead of accepting fuzzy patches.
    """

    actual_rejects = {
        path.relative_to(provider_checkout).as_posix()
        for path in provider_checkout.rglob("*.rej")
    }
    if actual_rejects != SUKISU_REJECTS:
        raise FeatureError(f"unexpected SukiSU SUSFS reject set: {sorted(actual_rejects)}")
    replace_once(
        provider_checkout / "kernel" / "Kbuild",
        "ifeq ($(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER),y)\nccflags-y += -DCONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER=1\nendif\n",
        "",
        "Kbuild x86 dispatcher",
    )
    init = provider_checkout / "kernel" / "core" / "init.c"
    replace_once(init, '#include "hook/syscall_hook.h"\n', "", "syscall hook include")
    replace_once(init, '#include "infra/symbol_resolver.h"\n', '#include "hook/setuid_hook.h"\n#include "feature/sucompat.h"\n', "SUSFS hook includes")
    replace_once(
        init,
        "#if defined(__x86_64__) && !defined(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER)\n#include <asm/cpufeature.h>\n#include <linux/version.h>\n#ifndef X86_FEATURE_INDIRECT_SAFE\n#error \"FATAL: Your kernel is missing the indirect syscall bypass patches!\"\n#endif\n#endif\n",
        "",
        "x86 dispatcher guard",
    )
    body_re = re.compile(
        r"    ksu_init_symbol_resolver\(\);\n.*?\n#ifdef MODULE\n#ifndef CONFIG_KSU_DEBUG\n"
        r"    kobject_del\(&THIS_MODULE->mkobj.kobj\);\n#endif\n#endif\n    return 0;\n",
        re.DOTALL,
    )
    content = init.read_text(encoding="utf-8")
    replacement = (
        "#ifdef CONFIG_KSU_SUSFS\n    susfs_init();\n#endif\n\n"
        "    if (spoof_release || spoof_version)\n        ksu_spoof_version(spoof_release, spoof_version);\n\n"
        "    ksu_feature_init();\n    ksu_supercalls_init();\n    ksu_sucompat_init();\n"
        "    ksu_setuid_hook_init();\n    ksu_sulog_init();\n    ksu_adb_root_init();\n"
        "    ksu_selinux_hide_init();\n    ksu_allowlist_init();\n    ksu_throne_tracker_init();\n"
        "    ksu_ksud_init();\n    ksu_file_wrapper_init();\n\n    return 0;\n"
    )
    content, count = body_re.subn(replacement, content, count=1)
    if count != 1:
        raise FeatureError("SukiSU SUSFS initialization anchor drifted")
    content = content.replace(
        "    // Phase 1: Stop all hooks first to prevent new callbacks\n    ksu_syscall_hook_manager_exit();\n\n",
        "",
        1,
    )
    content = content.replace("    if (!ksu_late_loaded)\n        ksu_ksud_exit();\n", "    ksu_ksud_exit();\n", 1)
    init.write_text(content, encoding="utf-8", newline="\n")
    replace_once(
        provider_checkout / "kernel" / "policy" / "app_profile.h",
        "void escape_to_root_for_init(void);",
        "int escape_to_root_for_init(void);",
        "escape_to_root_for_init signature",
    )
    for relative in sorted(SUKISU_REJECTS):
        (provider_checkout / relative).unlink()
    run(["git", "-C", str(provider_checkout), "diff", "--check"])


def apply_sukisu_provider_patch(provider_checkout: Path, patch: Path) -> None:
    process = subprocess.run(
        ["git", "-C", str(provider_checkout), "apply", "--reject", "--whitespace=fix", str(patch)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(process.stdout, end="")
    if process.returncode != 1:
        raise FeatureError(
            "locked SukiSU SUSFS patch must produce exactly its three audited rejects"
        )
    finish_sukisu_reject_adapter(provider_checkout)


def apply_susfs(plan: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    provider = plan["selection"]["root_source"]
    family = plan["selection"]["family_id"]
    source = plan["features"]["susfs"].get("source")
    if not isinstance(source, dict):
        raise FeatureError("SUSFS plan is missing locked source provenance")
    checkout = checkout_locked(
        source, workspace / ".renebula-features" / "susfs4ksu", SUSFS_REPOSITORY
    )
    common = workspace / "common"
    provider_checkout = workspace / "KernelSU"
    if not common.is_dir() or not provider_checkout.is_dir():
        raise FeatureError("SUSFS requires materialized common and KernelSU trees")
    for source_path, destination in (
        (checkout / "kernel_patches" / "fs" / "susfs.c", common / "fs" / "susfs.c"),
        (checkout / "kernel_patches" / "include" / "linux" / "susfs.h", common / "include" / "linux" / "susfs.h"),
        (checkout / "kernel_patches" / "include" / "linux" / "susfs_def.h", common / "include" / "linux" / "susfs_def.h"),
    ):
        if not source_path.is_file() or destination.exists():
            raise FeatureError(f"SUSFS source copy contract failed: {source_path} -> {destination}")
        shutil.copy2(source_path, destination)
    kernel_patch_strategy = apply_common_susfs_patch(
        common,
        checkout / "kernel_patches" / f"50_add_susfs_in_gki-{family}.patch",
        family,
    )
    provider_patch = checkout / "kernel_patches" / "KernelSU" / "10_enable_susfs_for_ksu.patch"
    strategy = susfs_provider_strategy(provider)
    if strategy == "official-kernelsu-patch":
        apply_git_patch(provider_checkout, provider_patch)
    elif strategy == "sukisu-reject-adapter-v1":
        apply_sukisu_provider_patch(provider_checkout, provider_patch)
    else:
        kconfig = provider_checkout / "kernel" / "Kconfig"
        if "config KSU_SUSFS" not in kconfig.read_text(encoding="utf-8"):
            raise FeatureError("ReSukiSU source no longer contains its native SUSFS integration")
    return {
        "feature": "susfs",
        "strategy": strategy,
        "source_lock": plan["features"]["susfs"]["source_lock"],
        "commit": source["commit"],
        "kernel_patch": f"50_add_susfs_in_gki-{family}.patch",
        "kernel_patch_strategy": kernel_patch_strategy,
    }


def adapt_kpm_source(makefile: Path, user_event: Path) -> None:
    """Keep Android KPM mode while removing two stale AP-root callbacks."""
    try:
        content = makefile.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read KPM Makefile {makefile}: {error}") from error
    android_mode = "# ifdef ANDROID\n\tCFLAGS += -DANDROID\n# endif\n"
    if content.count(android_mode) != 1:
        raise FeatureError("locked KPM Makefile Android compatibility anchor drifted")
    try:
        events = user_event.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read KPM user-event source {user_event}: {error}") from error
    stale_blocks = (
        (
            '    if (lib_strcmp(safe_event, "post-fs-data") == 0) {\n'
            '        log_boot("post-fs-data: loading ap package config ...\\n");\n'
            "        load_ap_package_config();\n"
            "    }\n",
            "    /* ReNebula: the trimmed KPM provider has no AP package loader. */\n",
        ),
        (
            '    if (lib_strcmp(safe_event, "uid_listener") == 0 && lib_strcmp(safe_args, "package-list-updated") == 0) {\n'
            "        int trust_rc = refresh_trusted_manager_state();\n"
            '        log_boot("boot-completed: trusted manager refresh rc=%d\\n", trust_rc);\n'
            "    }\n",
            "    /* ReNebula: the trimmed KPM provider has no AP trust database. */\n",
        ),
    )
    for anchor, replacement in stale_blocks:
        if events.count(anchor) != 1:
            raise FeatureError("locked KPM AP-root callback anchor drifted")
        events = events.replace(anchor, replacement, 1)
    user_event.write_text(events, encoding="utf-8", newline="\n")


def adapt_sukisu_kpm_bridge(provider_kernel: Path) -> None:
    """Preserve SukiSU's KPM bridge across known upstream compatibility regressions."""
    kbuild = provider_kernel / "Kbuild"
    init = provider_kernel / "core" / "init.c"
    try:
        content = kbuild.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read SukiSU Kbuild {kbuild}: {error}") from error
    resolver = "kernelsu-objs += infra/symbol_resolver.o"
    count = content.count(resolver)
    if count > 1:
        raise FeatureError("SukiSU symbol-resolver Kbuild entry is duplicated")
    if count == 0:
        anchor = "kernelsu-objs += infra/su_mount_ns.o\n"
        if content.count(anchor) != 1:
            raise FeatureError("locked SukiSU symbol-resolver Kbuild anchor drifted")
        content = content.replace(anchor, f"{anchor}{resolver}\n", 1)
        kbuild.write_text(content, encoding="utf-8", newline="\n")

    try:
        init_content = init.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read SukiSU init source {init}: {error}") from error
    resolver_include = '#include "infra/symbol_resolver.h"'
    include_count = init_content.count(resolver_include)
    if include_count > 1:
        raise FeatureError("SukiSU symbol-resolver include is duplicated")
    if include_count == 0:
        include_anchor = '#include "feature/sucompat.h"\n'
        if init_content.count(include_anchor) != 1:
            raise FeatureError("locked SukiSU symbol-resolver include anchor drifted")
        init_content = init_content.replace(
            include_anchor, f"{include_anchor}{resolver_include}\n", 1
        )
    resolver_init = "    ksu_init_symbol_resolver();"
    init_count = init_content.count(resolver_init)
    if init_count > 1:
        raise FeatureError("SukiSU symbol-resolver initialization is duplicated")
    if init_count == 0:
        init_anchor = (
            "    if (!ksu_cred) {\n"
            '        pr_err("prepare cred failed!\\n");\n'
            "        return -ENOSYS;\n"
            "    }\n"
        )
        if init_content.count(init_anchor) != 1:
            raise FeatureError("locked SukiSU symbol-resolver initialization anchor drifted")
        init_content = init_content.replace(
            init_anchor, f"{init_anchor}\n{resolver_init}\n", 1
        )
    init.write_text(init_content, encoding="utf-8", newline="\n")

    super_access = provider_kernel / "kpm" / "super_access.c"
    try:
        access_content = super_access.read_text(encoding="utf-8")
    except OSError as error:
        raise FeatureError(f"cannot read SukiSU KPM source {super_access}: {error}") from error
    cb_mutex = "DEFINE_MEMBER(netlink_kernel_cfg, cb_mutex)\n"
    guarded_cb_mutex = (
        "#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 11, 0)\n"
        f"{cb_mutex}"
        "#endif\n"
    )
    if access_content.count(guarded_cb_mutex) == 0:
        if access_content.count(cb_mutex) != 1:
            raise FeatureError("locked SukiSU netlink cb_mutex compatibility anchor drifted")
        access_content = access_content.replace(cb_mutex, guarded_cb_mutex, 1)
        super_access.write_text(access_content, encoding="utf-8", newline="\n")
    elif access_content.count(guarded_cb_mutex) != 1:
        raise FeatureError("SukiSU netlink cb_mutex compatibility guard is duplicated")


def prepare_kpm(plan: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    if plan.get("selection", {}).get("root_source") != "sukisu":
        raise FeatureError("KPM preparation requires the SukiSU provider bridge")
    provider_kernel = workspace / "KernelSU" / "kernel"
    if not (provider_kernel / "infra" / "symbol_resolver.c").is_file():
        raise FeatureError("locked SukiSU source lacks its KPM symbol resolver")
    adapt_sukisu_kpm_bridge(provider_kernel)
    source = plan["features"]["kpm"].get("source")
    if not isinstance(source, dict):
        raise FeatureError("KPM plan is missing locked source provenance")
    checkout = checkout_locked(source, workspace / ".renebula-features" / "kpm", KPM_REPOSITORY)
    if not (checkout / "tools" / "Makefile").is_file() or not (checkout / "kernel" / "Makefile").is_file():
        raise FeatureError("locked KPM source lacks build inputs")
    adapt_kpm_source(
        checkout / "kernel" / "Makefile",
        checkout / "kernel" / "patch" / "common" / "user_event.c",
    )
    return {
        "feature": "kpm",
        "source_lock": plan["features"]["kpm"]["source_lock"],
        "commit": source["commit"],
        "checkout": str(checkout),
        "source_adapter": "sukisu-android-kpm-no-ap-callbacks-v1",
        "provider_bridge_adapter": "sukisu-susfs-kpm-compat-v2",
        "post_build": True,
    }


def patch_kpm_image(kptools: Path, kpimg: Path, image: Path, output: Path) -> Dict[str, str]:
    for label, path in (("kptools", kptools), ("kpimg", kpimg), ("Image", image)):
        if not path.is_file():
            raise FeatureError(f"{label} is missing: {path}")
    if output.exists():
        raise FeatureError(f"refusing to overwrite KPM output: {output}")
    kpimg_listing = run([str(kptools), "-l", "-k", str(kpimg)])
    if not re.search(r"(?m)^config=android,release$", kpimg_listing):
        raise FeatureError("KPM payload must be built in Android release mode")
    output.parent.mkdir(parents=True, exist_ok=True)
    run([str(kptools), "-p", "-i", str(image), "-k", str(kpimg), "-o", str(output)])
    if not output.is_file() or output.stat().st_size == 0:
        raise FeatureError("kptools did not produce a patched Image")
    image_listing = run([str(kptools), "-l", "-i", str(output)])
    if not re.search(r"(?m)^patched=true$", image_listing) or not re.search(
        r"(?m)^config=android,release$", image_listing
    ):
        raise FeatureError("KPM output is not a verified patched Android KPM Image")
    return {
        "feature": "kpm",
        "input": str(image),
        "kpimg": str(kpimg),
        "output": str(output),
        "verification": "patched-android-release",
    }


def add_vivo_modinfo_token(modinfo: bytes) -> bytes:
    fields = modinfo.split(b"\0")
    vermagic_indexes = [index for index, field in enumerate(fields) if field.startswith(b"vermagic=")]
    if len(vermagic_indexes) != 1:
        raise FeatureError("module .modinfo must contain exactly one vermagic field")
    index = vermagic_indexes[0]
    parts = fields[index].split()
    if parts[1:].count(b"vivo"):
        raise FeatureError("module vermagic already contains a vivo token")
    if parts[1:].count(b"aarch64") != 1:
        raise FeatureError("module vermagic must contain exactly one aarch64 token")
    arch_index = parts.index(b"aarch64")
    parts.insert(arch_index, b"vivo")
    fields[index] = b" ".join(parts)
    return b"\0".join(fields)


def patch_vivo_module(objcopy: Path, module: Path) -> Dict[str, str]:
    if not objcopy.is_file() or not module.is_file():
        raise FeatureError("Vivo module patching requires llvm-objcopy and kernelsu.ko")
    with tempfile.TemporaryDirectory(prefix="renebula-vivo-") as temporary:
        modinfo = Path(temporary) / "modinfo.bin"
        run([str(objcopy), "--dump-section", f".modinfo={modinfo}", str(module)])
        original = modinfo.read_bytes()
        updated = add_vivo_modinfo_token(original)
        modinfo.write_bytes(updated)
        run([str(objcopy), "--update-section", f".modinfo={modinfo}", str(module)])
    module_bytes = module.read_bytes()
    matches = re.findall(rb"vermagic=([^\x00\r\n]+)", module_bytes)
    if len(matches) != 1 or matches[0].split()[1:].count(b"vivo") != 1:
        raise FeatureError("Vivo module vermagic update could not be verified")
    return {"feature": "vivo_vermagic", "strategy": "elf-modinfo-token-v1", "module": str(module)}


def prepare_features(plan: Dict[str, Any], variant_id: str, workspace: Path) -> Dict[str, Any]:
    scope = validate_feature_scope(plan, variant_id)
    applied: List[Dict[str, Any]] = []
    if scope["susfs"]:
        applied.append(apply_susfs(plan, workspace))
    if scope["kpm"]:
        applied.append(prepare_kpm(plan, workspace))
    if scope["vivo_vermagic"]:
        applied.append({"feature": "vivo_vermagic", "strategy": "deferred-ddk-modinfo"})
    record = {"schema": 1, "variant_id": variant_id, "applied": applied}
    (workspace / "renebula-feature-record.json").write_bytes(canonical_json(record) + b"\n")
    return record


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--variant-id", required=True)
    parser.add_argument("--kernel-workspace", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=("prepare", "patch-kpm-image", "patch-vivo-module"),
        default="prepare",
    )
    parser.add_argument("--image", type=Path)
    parser.add_argument("--output-image", type=Path)
    parser.add_argument("--kptools", type=Path)
    parser.add_argument("--kpimg", type=Path)
    parser.add_argument("--module", type=Path)
    parser.add_argument("--objcopy", type=Path)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = load_json(args.plan)
        workspace = args.kernel_workspace.resolve()
        scope = validate_feature_scope(plan, args.variant_id)
        if args.phase == "prepare":
            prepare_features(plan, args.variant_id, workspace)
        elif args.phase == "patch-kpm-image":
            if not scope["kpm"]:
                raise FeatureError("patch-kpm-image requires KPM on this variant")
            if None in (args.image, args.output_image, args.kptools, args.kpimg):
                raise FeatureError("KPM Image patching requires image, output-image, kptools, and kpimg")
            record = patch_kpm_image(args.kptools, args.kpimg, args.image, args.output_image)
            (workspace / "renebula-kpm-record.json").write_bytes(canonical_json({"schema": 1, **record}) + b"\n")
        else:
            if not scope["vivo_vermagic"]:
                raise FeatureError("patch-vivo-module requires Vivo vermagic on this variant")
            if args.module is None or args.objcopy is None:
                raise FeatureError("Vivo module patching requires module and objcopy")
            record = patch_vivo_module(args.objcopy, args.module)
            (workspace / "renebula-vivo-record.json").write_bytes(
                canonical_json({"schema": 1, **record}) + b"\n"
            )
    except (FeatureError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
