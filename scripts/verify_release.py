#!/usr/bin/env python3
"""Enforce ReNebula's one-writer kernel release contract.

Kbuild owns the base ``VERSION.PATCHLEVEL.SUBLEVEL``.  This module is the sole
place that may add ReNebula's suffix, either as a Kleaf defconfig fragment or
as a legacy build.sh post-defconfig hook.  It never edits ``setlocalversion``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


MAKE_VERSION_RE = re.compile(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)\s*$")
RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[^\s]+)?$")
SUFFIX_RE = re.compile(r"^-[A-Za-z0-9._-]+$")
UNAME_SUFFIX_RE = re.compile(r"^(?:|-[A-Za-z0-9][A-Za-z0-9._-]*)$")
KCONFIG_SET_RE = re.compile(r"^CONFIG_([A-Z0-9_]+)=(y|m)$")
KCONFIG_UNSET_RE = re.compile(r"^# CONFIG_([A-Z0-9_]+) is not set$")
LINUX_VERSION_RE = re.compile(rb"Linux version ([^\x00\r\n\t ]+)")
UTS_RELEASE_RE = re.compile(r'^#define\s+UTS_RELEASE\s+"([^"\s]+)"\s*$')
KLEAF_FRAGMENT_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
KLEAF_ADAPTERS = {KLEAF_FRAGMENT_ADAPTER}
LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"
MAX_UTS_RELEASE_LENGTH = 64


class ContractError(ValueError):
    """Raised when a plan or built artifact violates the version contract."""


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return document


def require_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{context}.{key} must be a non-empty string")
    return value


def require_optional_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str):
        raise ContractError(f"{context}.{key} must be a string")
    return value


def parse_base_release(makefile: Path) -> str:
    values: Dict[str, str] = {}
    try:
        lines = makefile.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ContractError(f"cannot read {makefile}: {error}") from error
    for line in lines:
        match = MAKE_VERSION_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    missing = {"VERSION", "PATCHLEVEL", "SUBLEVEL"} - values.keys()
    if missing:
        raise ContractError(f"{makefile} is missing version field(s): {', '.join(sorted(missing))}")
    return f"{values['VERSION']}.{values['PATCHLEVEL']}.{values['SUBLEVEL']}"


def validate_suffix(base_release: str, suffix: str) -> None:
    if not SUFFIX_RE.fullmatch(suffix):
        raise ContractError("local_suffix must use only letters, digits, '.', '_', and '-'")
    if base_release in suffix:
        raise ContractError("local_suffix must not contain the complete base release")
    if not RELEASE_RE.fullmatch(base_release + suffix):
        raise ContractError("local_suffix produces an invalid release string")
    if len(base_release + suffix) > MAX_UTS_RELEASE_LENGTH:
        raise ContractError("local_suffix exceeds the UTS_RELEASE length limit")


def validate_uname_suffix(
    base_release: str, managed_suffix: str, uname_suffix: str, local_suffix: str
) -> None:
    if not SUFFIX_RE.fullmatch(managed_suffix):
        raise ContractError("managed_suffix must use only letters, digits, '.', '_', and '-'")
    if base_release in managed_suffix:
        raise ContractError("managed_suffix must not contain the complete base release")
    if not UNAME_SUFFIX_RE.fullmatch(uname_suffix):
        raise ContractError(
            "uname_suffix must be empty or start with '-' and contain only "
            "letters, digits, '.', '_', and '-'"
        )
    if base_release in uname_suffix:
        raise ContractError("uname_suffix must not contain the complete Google base release")
    if local_suffix != managed_suffix + uname_suffix:
        raise ContractError("local_suffix must equal managed_suffix plus uname_suffix")


def validate_kconfig_lines(configuration: Dict[str, Any]) -> List[str]:
    lines = configuration.get("kconfig_lines")
    if not isinstance(lines, list):
        raise ContractError("plan.configuration.kconfig_lines must be an array")
    result: List[str] = []
    seen = set()
    for index, line in enumerate(lines):
        if not isinstance(line, str):
            raise ContractError(f"plan.configuration.kconfig_lines[{index}] must be a string")
        match = KCONFIG_SET_RE.fullmatch(line) or KCONFIG_UNSET_RE.fullmatch(line)
        if match is None:
            raise ContractError(
                f"plan.configuration.kconfig_lines[{index}] must be a safe y/m/unset Kconfig line"
            )
        key = match.group(1)
        if key in {"LOCALVERSION", "LOCALVERSION_AUTO"}:
            raise ContractError("only the version contract may write LOCALVERSION configuration")
        if key in seen:
            raise ContractError(f"plan.configuration repeats Kconfig key: {key}")
        seen.add(key)
        result.append(line)
    return result


def validate_plan(
    plan: Dict[str, Any], observed_base_release: str
) -> Tuple[str, str, Dict[str, str], List[str]]:
    if plan.get("schema") != 4:
        raise ContractError("plan.schema must be 4")
    selection = plan.get("selection")
    if not isinstance(selection, dict):
        raise ContractError("plan.selection must be an object")
    root_provider = require_string(selection, "root_provider", "plan.selection")
    root_linkage = require_string(selection, "root_linkage", "plan.selection")
    hook_mode = require_string(selection, "hook_mode", "plan.selection")
    config_profile = require_string(selection, "config_profile", "plan.selection")
    selected_uname_suffix = require_optional_string(selection, "uname_suffix", "plan.selection")
    root = plan.get("root")
    if not isinstance(root, dict):
        raise ContractError("plan.root must be an object")
    if require_string(root, "id", "plan.root") != root_provider:
        raise ContractError("plan.root.id must match plan.selection.root_provider")
    if require_string(root, "linkage", "plan.root") != root_linkage:
        raise ContractError("plan.root.linkage must match plan.selection.root_linkage")
    if require_string(root, "hook_mode", "plan.root") != hook_mode:
        raise ContractError("plan.root.hook_mode must match plan.selection.hook_mode")
    configuration = plan.get("configuration")
    if not isinstance(configuration, dict):
        raise ContractError("plan.configuration must be an object")
    if require_string(configuration, "id", "plan.configuration") != config_profile:
        raise ContractError("plan.configuration.id must match plan.selection.config_profile")
    kconfig_lines = validate_kconfig_lines(configuration)
    if root_provider == "none":
        if require_string(root, "adapter", "plan.root") != "none":
            raise ContractError("root=none must use adapter=none")
        if root.get("source_lock") is not None or root.get("source") is not None:
            raise ContractError("root=none must not carry a root source")
        if (
            root_linkage != "none"
            or hook_mode != "none"
            or config_profile != "release"
            or kconfig_lines
            or require_string(configuration, "resolved_id", "plan.configuration") != "none"
        ):
            raise ContractError("root=none must use the empty release configuration")
    elif root_provider == "resukisu":
        if require_string(root, "adapter", "plan.root") != "resukisu-driver-link-v1":
            raise ContractError("ReSukiSU must use the locked driver-link adapter")
        require_string(root, "source_lock", "plan.root")
        source = root.get("source")
        if not isinstance(source, dict):
            raise ContractError("ReSukiSU root plan must include locked source provenance")
        for key in ("repository", "commit", "ref"):
            require_string(source, key, "plan.root.source")
        if root_linkage not in {"lkm", "builtin"} or hook_mode != "tracepoint":
            raise ContractError("ReSukiSU must use lkm/builtin with the Tracepoint hook")
        required = {
            f"CONFIG_KSU={'m' if root_linkage == 'lkm' else 'y'}",
            "CONFIG_KSU_TRACEPOINT_HOOK=y",
            "CONFIG_KSU_MULTI_MANAGER_SUPPORT=y",
        }
        disabled_hook_modes = {"# CONFIG_KSU_MANUAL_HOOK is not set", "# CONFIG_KSU_SUSFS is not set"}
        if config_profile == "release":
            expected_lines = required | disabled_hook_modes | {"# CONFIG_KSU_DEBUG is not set"}
        elif config_profile == "debug":
            expected_lines = required | disabled_hook_modes | {"CONFIG_KSU_DEBUG=y"}
        else:
            raise ContractError("ReSukiSU configuration profile is unsupported")
        if set(kconfig_lines) != expected_lines:
            raise ContractError("ReSukiSU configuration does not match its locked profile")
        expected_resolved_id = f"resukisu-{root_linkage}-{hook_mode}-{config_profile}"
        if require_string(configuration, "resolved_id", "plan.configuration") != expected_resolved_id:
            raise ContractError("ReSukiSU configuration resolved id is inconsistent")
    else:
        raise ContractError(f"unsupported root provider: {root_provider}")
    version = plan.get("version")
    if not isinstance(version, dict):
        raise ContractError("plan.version must be an object")
    expected_base_release = require_string(version, "expected_base_release", "plan.version")
    if expected_base_release != observed_base_release:
        raise ContractError(
            f"source base release is {observed_base_release}, plan locks {expected_base_release}"
        )
    suffix = require_string(version, "local_suffix", "plan.version")
    managed_suffix = require_string(version, "managed_suffix", "plan.version")
    version_uname_suffix = require_optional_string(version, "uname_suffix", "plan.version")
    if version_uname_suffix != selected_uname_suffix:
        raise ContractError("plan.version.uname_suffix must match plan.selection.uname_suffix")
    validate_uname_suffix(
        observed_base_release, managed_suffix, version_uname_suffix, suffix
    )
    validate_suffix(observed_base_release, suffix)
    release_contract = version.get("release_contract")
    if not isinstance(release_contract, dict):
        raise ContractError("plan.version.release_contract must be an object")
    mode = require_string(release_contract, "mode", "plan.version.release_contract")
    source = plan.get("source")
    if not isinstance(source, dict):
        raise ContractError("plan.source must be an object")
    require_string(source, "lock_id", "plan.source")
    build = plan.get("build")
    if not isinstance(build, dict):
        raise ContractError("plan.build must be an object")
    adapter = require_string(build, "adapter", "plan.build")
    if adapter not in KLEAF_ADAPTERS | {LEGACY_ADAPTER}:
        raise ContractError(f"unsupported build adapter: {adapter}")
    image_name = require_string(build, "image_name", "plan.build")
    if image_name != Path(image_name).name:
        raise ContractError("plan.build.image_name must be a file name")
    if mode == "exact":
        if adapter != LEGACY_ADAPTER:
            raise ContractError("only the legacy adapter may use an exact uname contract")
        expected_release = require_string(
            release_contract, "expected_uname_release", "plan.version.release_contract"
        )
        if expected_release != observed_base_release + suffix:
            raise ContractError(
                "exact uname contract must be base release plus the resolved suffix"
            )
    elif mode == "base-prefix-and-suffix":
        if adapter not in KLEAF_ADAPTERS:
            raise ContractError("only the Kleaf adapter may use a boundary uname contract")
        if require_string(release_contract, "prefix", "plan.version.release_contract") != observed_base_release:
            raise ContractError("Kleaf uname contract prefix must be the Makefile base release")
        if require_string(release_contract, "suffix", "plan.version.release_contract") != suffix:
            raise ContractError("Kleaf uname contract suffix must be the resolved suffix")
    else:
        raise ContractError(f"unsupported uname contract mode: {mode}")
    return suffix, adapter, release_contract, kconfig_lines


def write_fragment(path: Path, suffix: str, kconfig_lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Generated by scripts/verify_release.py; do not hand-edit.\n"
        f'CONFIG_LOCALVERSION="{suffix}"\n'
        "# CONFIG_LOCALVERSION_AUTO is not set\n"
        + "".join(f"{line}\n" for line in kconfig_lines)
    )
    path.write_text(content, encoding="utf-8", newline="\n")


def write_build_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Generated by ReNebula's version contract; do not hand-edit.\n"
        'exports_files(["localversion_defconfig"])\n'
    )
    path.write_text(content, encoding="utf-8", newline="\n")


def legacy_config_command(line: str) -> str:
    match = KCONFIG_SET_RE.fullmatch(line)
    if match:
        key, value = match.groups()
        operation = "-e" if value == "y" else "-m"
        return f'"$kernel_dir/scripts/config" --file "$config_path" {operation} {key}\n'
    match = KCONFIG_UNSET_RE.fullmatch(line)
    if match:
        return f'"$kernel_dir/scripts/config" --file "$config_path" -d {match.group(1)}\n'
    raise ContractError(f"unsafe Kconfig line: {line}")


def write_legacy_script(path: Path, suffix: str, kconfig_lines: List[str]) -> None:
    """Write a deterministic post-defconfig editor for the legacy build.sh adapter."""

    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "#!/usr/bin/env bash\n"
        "# Generated by scripts/verify_release.py; do not hand-edit.\n"
        "set -euo pipefail\n"
        'config_path="${1:?missing kernel .config path}"\n'
        'test -f "$config_path"\n'
        'kernel_dir="$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd)"\n'
        'out_dir="$(dirname \"$config_path\")"\n'
        '"$kernel_dir/scripts/config" --file "$config_path" '
        f'-d LOCALVERSION_AUTO --set-str LOCALVERSION "{suffix}"\n'
        + "".join(legacy_config_command(line) for line in kconfig_lines)
        + 'make -C "$kernel_dir" O="$out_dir" olddefconfig\n'
    )
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | 0o111)


def write_legacy_build_config(path: Path) -> None:
    """Append the generated editor through build.sh's fragment contract."""

    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Generated by scripts/verify_release.py; do not hand-edit.\n"
        "# Keep scripts/setlocalversion from adding a Git-derived '+' suffix.\n"
        'export LOCALVERSION=""\n'
        "append_cmd POST_DEFCONFIG_CMDS "
        "'${ROOT_DIR}/renebula/apply-localversion.sh \"${OUT_DIR}/.config\"'\n"
    )
    path.write_text(content, encoding="utf-8", newline="\n")


def parse_utsrelease(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    for line in lines:
        match = UTS_RELEASE_RE.match(line)
        if match:
            return match.group(1)
    raise ContractError(f"UTS_RELEASE is missing from {path}")


def parse_image_release(path: Path) -> str:
    try:
        image = path.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    matches = {
        match.group(1).decode("ascii", errors="strict") for match in LINUX_VERSION_RE.finditer(image)
    }
    if not matches:
        raise ContractError(f"Linux version string is absent from {path}")
    if len(matches) != 1:
        raise ContractError(f"ambiguous Linux version strings in {path}: {sorted(matches)}")
    return matches.pop()


def validate_observed_release(
    actual_release: str,
    base_release: str,
    suffix: str,
    release_contract: Dict[str, str],
) -> None:
    if len(actual_release) > MAX_UTS_RELEASE_LENGTH:
        raise ContractError("observed release exceeds the UTS_RELEASE length limit")
    mode = release_contract["mode"]
    if mode == "exact":
        expected_release = release_contract["expected_uname_release"]
        if actual_release != expected_release:
            raise ContractError(f"release is {actual_release}, expected {expected_release}")
        return
    if mode == "base-prefix-and-suffix":
        if not actual_release.startswith(base_release):
            raise ContractError(f"release is {actual_release}, expected prefix {base_release}")
        if not actual_release.endswith(suffix):
            raise ContractError(f"release is {actual_release}, expected suffix {suffix}")
        if actual_release.count(base_release) != 1:
            raise ContractError(
                "release repeats the base version; this indicates a double-prefix version bug"
            )
        return
    raise ContractError(f"unsupported uname contract mode: {mode}")


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ContractError(f"cannot hash {path}: {error}") from error


def write_record(
    path: Path,
    plan: Path,
    base_release: str,
    release_contract: Dict[str, str],
    observed_release: Optional[str],
    image: Optional[Path],
    selection: Dict[str, Any],
) -> None:
    record: Dict[str, Any] = {
        "schema": 4,
        "plan_sha256": sha256(plan),
        "observed_base_release": base_release,
        "release_contract": release_contract,
        "selection": {
            "release_id": selection.get("release_id"),
            "root_provider": selection.get("root_provider"),
            "root_linkage": selection.get("root_linkage"),
            "hook_mode": selection.get("hook_mode"),
            "config_profile": selection.get("config_profile"),
            "uname_suffix": selection.get("uname_suffix"),
        },
    }
    if observed_release is not None:
        record["observed_uts_release"] = observed_release
    if image is not None:
        record["image_sha256"] = sha256(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--makefile", type=Path, required=True)
    parser.add_argument("--write-fragment", type=Path)
    parser.add_argument("--write-build-file", type=Path)
    parser.add_argument("--write-legacy-script", type=Path)
    parser.add_argument("--write-legacy-build-config", type=Path)
    parser.add_argument("--utsrelease-header", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--record", type=Path)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = load_json(args.plan)
        base_release = parse_base_release(args.makefile)
        suffix, adapter, release_contract, kconfig_lines = validate_plan(plan, base_release)
        wants_kleaf_fragment_files = (
            args.write_fragment is not None or args.write_build_file is not None
        )
        wants_legacy_files = (
            args.write_legacy_script is not None or args.write_legacy_build_config is not None
        )
        if wants_kleaf_fragment_files and adapter != KLEAF_FRAGMENT_ADAPTER:
            raise ContractError(
                "Kleaf defconfig-fragment files requested for an incompatible build adapter"
            )
        if wants_legacy_files and adapter != LEGACY_ADAPTER:
            raise ContractError("legacy version files requested for a non-legacy build adapter")
        if wants_legacy_files and (
            args.write_legacy_script is None or args.write_legacy_build_config is None
        ):
            raise ContractError("legacy version injection requires both generated files")
        if args.write_fragment:
            write_fragment(args.write_fragment, suffix, kconfig_lines)
        if args.write_build_file:
            write_build_file(args.write_build_file)
        if args.write_legacy_script:
            write_legacy_script(args.write_legacy_script, suffix, kconfig_lines)
        if args.write_legacy_build_config:
            write_legacy_build_config(args.write_legacy_build_config)
        observed_release: Optional[str] = None
        if args.utsrelease_header:
            actual_release = parse_utsrelease(args.utsrelease_header)
            validate_observed_release(actual_release, base_release, suffix, release_contract)
            observed_release = actual_release
        if args.image:
            image_release = parse_image_release(args.image)
            validate_observed_release(image_release, base_release, suffix, release_contract)
            if observed_release is not None and observed_release != image_release:
                raise ContractError(
                    "UTS_RELEASE and Image report different kernel releases "
                    f"({observed_release} != {image_release})"
                )
            observed_release = image_release
        if args.record:
            write_record(
                args.record,
                args.plan,
                base_release,
                release_contract,
                observed_release,
                args.image,
                plan["selection"],
            )
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
