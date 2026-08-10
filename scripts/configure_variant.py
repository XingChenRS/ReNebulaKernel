#!/usr/bin/env python3
"""Compile one schema-5 variant into backend Kconfig and LOCALVERSION inputs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


MAKE_VERSION_RE = re.compile(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)\s*$")
KCONFIG_SYMBOL_RE = re.compile(r"^[A-Z0-9_]+$")
SUFFIX_RE = re.compile(r"^-[A-Za-z0-9._-]+$")
KLEAF_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"
MAX_UTS_RELEASE_LENGTH = 64


class ConfigurationError(ValueError):
    """Raised when a plan cannot be compiled into one unambiguous config."""


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConfigurationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")
    return value


def find_variant(plan: Dict[str, Any], variant_id: str) -> Dict[str, Any]:
    if plan.get("schema") != 5:
        raise ConfigurationError("plan.schema must be 5")
    variants = plan.get("variants")
    if not isinstance(variants, list):
        raise ConfigurationError("plan.variants must be an array")
    matches = [item for item in variants if isinstance(item, dict) and item.get("id") == variant_id]
    if len(matches) != 1:
        raise ConfigurationError(f"variant must appear exactly once in plan: {variant_id}")
    return matches[0]


def compile_kconfig(plan: Dict[str, Any], variant_id: str) -> List[str]:
    """Return deterministic Kconfig lines owned entirely by the selected variant."""

    variant = find_variant(plan, variant_id)
    configuration = variant.get("configuration")
    if not isinstance(configuration, dict):
        raise ConfigurationError(f"variant {variant_id}.configuration must be an object")
    root = plan.get("root")
    if not isinstance(root, dict):
        raise ConfigurationError("plan.root must be an object")
    if root.get("id") == "none" and configuration:
        raise ConfigurationError("baseline variant must not contain root configuration")
    result: List[str] = []
    for symbol in sorted(configuration):
        value = configuration[symbol]
        if not isinstance(symbol, str) or not KCONFIG_SYMBOL_RE.fullmatch(symbol):
            raise ConfigurationError(f"unsafe Kconfig symbol: {symbol}")
        if symbol in {"LOCALVERSION", "LOCALVERSION_AUTO"}:
            raise ConfigurationError("provider configuration may not write LOCALVERSION")
        if value not in {"y", "m", "n"}:
            raise ConfigurationError(f"CONFIG_{symbol} must resolve to y, m, or n")
        if symbol == "KSU_DEBUG" and value != "n":
            raise ConfigurationError("KSU debug is fixed off")
        result.append(f"CONFIG_{symbol}={value}" if value in {"y", "m"} else f"# CONFIG_{symbol} is not set")
    return result


def parse_base_release(makefile: Path) -> str:
    try:
        lines = makefile.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigurationError(f"cannot read {makefile}: {error}") from error
    values: Dict[str, str] = {}
    for line in lines:
        match = MAKE_VERSION_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    missing = {"VERSION", "PATCHLEVEL", "SUBLEVEL"} - values.keys()
    if missing:
        raise ConfigurationError(f"{makefile} is missing version fields: {sorted(missing)}")
    return f"{values['VERSION']}.{values['PATCHLEVEL']}.{values['SUBLEVEL']}"


def validate_version(plan: Dict[str, Any], variant: Dict[str, Any], observed_base: str) -> str:
    version = plan.get("version")
    variant_version = variant.get("version")
    if not isinstance(version, dict) or not isinstance(variant_version, dict):
        raise ConfigurationError("plan and variant version contracts must be objects")
    if version.get("expected_base_release") != observed_base:
        raise ConfigurationError(
            f"source base release is {observed_base}, plan locks {version.get('expected_base_release')}"
        )
    suffix = variant_version.get("local_suffix")
    if not isinstance(suffix, str) or not SUFFIX_RE.fullmatch(suffix):
        raise ConfigurationError("variant local_suffix is invalid")
    google_budget = variant_version.get("google_localversion_budget")
    if (
        not isinstance(google_budget, int)
        or isinstance(google_budget, bool)
        or google_budget < 1
        or observed_base in suffix
        or len(observed_base) + google_budget + len(suffix) > MAX_UTS_RELEASE_LENGTH
    ):
        raise ConfigurationError("variant local_suffix violates the UTS_RELEASE contract")
    return suffix


def write_fragment(path: Path, suffix: str, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Generated by scripts/configure_variant.py; do not hand-edit.\n"
        f'CONFIG_LOCALVERSION="{suffix}"\n'
        "# CONFIG_LOCALVERSION_AUTO is not set\n"
        + "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
        newline="\n",
    )


def legacy_command(line: str) -> str:
    set_match = re.fullmatch(r"CONFIG_([A-Z0-9_]+)=(y|m)", line)
    if set_match:
        operation = "-e" if set_match.group(2) == "y" else "-m"
        return f'"$kernel_dir/scripts/config" --file "$config_path" {operation} {set_match.group(1)}\n'
    unset_match = re.fullmatch(r"# CONFIG_([A-Z0-9_]+) is not set", line)
    if unset_match:
        return f'"$kernel_dir/scripts/config" --file "$config_path" -d {unset_match.group(1)}\n'
    raise ConfigurationError(f"unsafe compiled Kconfig line: {line}")


def write_legacy_files(output_root: Path, suffix: str, lines: List[str]) -> None:
    script = output_root / "apply-localversion.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by scripts/configure_variant.py; do not hand-edit.\n"
        "set -euo pipefail\n"
        'config_path="${1:?missing kernel .config path}"\n'
        'test -f "$config_path"\n'
        'kernel_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
        'out_dir="$(dirname "$config_path")"\n'
        f'"$kernel_dir/scripts/config" --file "$config_path" -d LOCALVERSION_AUTO --set-str LOCALVERSION "{suffix}"\n'
        + "".join(legacy_command(line) for line in lines)
        + 'make -C "$kernel_dir" O="$out_dir" olddefconfig\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(script.stat().st_mode | 0o111)
    (output_root / "localversion.build.config").write_text(
        "# Generated by scripts/configure_variant.py; do not hand-edit.\n"
        'export LOCALVERSION=""\n'
        "append_cmd POST_DEFCONFIG_CMDS "
        "'${ROOT_DIR}/renebula/apply-localversion.sh \"${OUT_DIR}/.config\"'\n",
        encoding="utf-8",
        newline="\n",
    )


def write_backend_contract(
    plan: Dict[str, Any], variant_id: str, makefile: Path, output_root: Path
) -> Dict[str, Any]:
    """Write the sole backend-owned Kconfig and LOCALVERSION contract."""

    variant = find_variant(plan, variant_id)
    build = plan.get("build")
    if not isinstance(build, dict):
        raise ConfigurationError("plan.build must be an object")
    adapter = build.get("adapter")
    if adapter not in {KLEAF_ADAPTER, LEGACY_ADAPTER}:
        raise ConfigurationError(f"unsupported build adapter: {adapter}")
    observed_base = parse_base_release(makefile)
    suffix = validate_version(plan, variant, observed_base)
    lines = compile_kconfig(plan, variant_id)
    output_root.mkdir(parents=True, exist_ok=True)
    if adapter == KLEAF_ADAPTER:
        write_fragment(output_root / "localversion_defconfig", suffix, lines)
        (output_root / "BUILD.bazel").write_text(
            "# Generated by scripts/configure_variant.py; do not hand-edit.\n"
            'exports_files(["localversion_defconfig"])\n',
            encoding="utf-8",
            newline="\n",
        )
    else:
        write_legacy_files(output_root, suffix, lines)
    record = {
        "schema": 1,
        "variant_id": variant_id,
        "backend": adapter,
        "observed_base_release": observed_base,
        "local_suffix": suffix,
        "kconfig_lines": lines,
    }
    (output_root / "renebula-config-record.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return record


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--variant-id", required=True)
    parser.add_argument("--makefile", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        write_backend_contract(
            load_json(args.plan), args.variant_id, args.makefile, args.output_root
        )
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
