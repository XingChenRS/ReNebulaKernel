#!/usr/bin/env python3
"""Verify built artifacts against one immutable schema-5 variant contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


MAKE_VERSION_RE = re.compile(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)\s*$")
LINUX_VERSION_RE = re.compile(rb"Linux version ([^\x00\r\n\t ]+)")
UTS_RELEASE_RE = re.compile(r'^#define\s+UTS_RELEASE\s+"([^"\s]+)"\s*$')
VERMAGIC_RE = re.compile(rb"vermagic=([^\x00\r\n]+)")
MAX_UTS_RELEASE_LENGTH = 64
KLEAF_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"


class ContractError(ValueError):
    """Raised when a built artifact violates its selected plan variant."""


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
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def parse_base_release(makefile: Path) -> str:
    try:
        lines = makefile.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ContractError(f"cannot read {makefile}: {error}") from error
    values: Dict[str, str] = {}
    for line in lines:
        match = MAKE_VERSION_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    missing = {"VERSION", "PATCHLEVEL", "SUBLEVEL"} - values.keys()
    if missing:
        raise ContractError(f"{makefile} is missing version fields: {sorted(missing)}")
    return f"{values['VERSION']}.{values['PATCHLEVEL']}.{values['SUBLEVEL']}"


def find_variant(plan: Dict[str, Any], variant_id: str) -> Dict[str, Any]:
    variants = plan.get("variants")
    if not isinstance(variants, list):
        raise ContractError("plan.variants must be an array")
    matches = [item for item in variants if isinstance(item, dict) and item.get("id") == variant_id]
    if len(matches) != 1:
        raise ContractError(f"variant must appear exactly once in plan: {variant_id}")
    return matches[0]


def validate_plan(
    plan: Dict[str, Any], variant_id: str, observed_base_release: str
) -> Tuple[str, str, Dict[str, str], Dict[str, Any]]:
    if plan.get("schema") != 5:
        raise ContractError("plan.schema must be 5")
    selection = plan.get("selection")
    root = plan.get("root")
    version = plan.get("version")
    build = plan.get("build")
    if not all(isinstance(value, dict) for value in (selection, root, version, build)):
        raise ContractError("plan selection, root, version, and build must be objects")
    if root.get("id") != selection.get("root_source"):
        raise ContractError("plan root does not match selected root source")
    if version.get("expected_base_release") != observed_base_release:
        raise ContractError(
            f"source base release is {observed_base_release}, plan locks {version.get('expected_base_release')}"
        )
    variant = find_variant(plan, variant_id)
    variant_version = variant.get("version")
    if not isinstance(variant_version, dict):
        raise ContractError("variant.version must be an object")
    suffix = variant_version.get("local_suffix")
    release_contract = variant_version.get("release_contract")
    if not isinstance(suffix, str) or not suffix.startswith("-"):
        raise ContractError("variant local_suffix is invalid")
    if observed_base_release in suffix or len(observed_base_release + suffix) > MAX_UTS_RELEASE_LENGTH:
        raise ContractError("variant local_suffix violates UTS_RELEASE boundaries")
    if not isinstance(release_contract, dict):
        raise ContractError("variant release_contract must be an object")
    adapter = build.get("adapter")
    mode = release_contract.get("mode")
    if adapter == LEGACY_ADAPTER:
        if mode != "exact" or release_contract.get("expected_uname_release") != observed_base_release + suffix:
            raise ContractError("legacy variant must use the exact uname contract")
    elif adapter == KLEAF_ADAPTER:
        if (
            mode != "base-prefix-and-suffix"
            or release_contract.get("prefix") != observed_base_release
            or release_contract.get("suffix") != suffix
        ):
            raise ContractError("Kleaf variant has an invalid uname boundary contract")
    else:
        raise ContractError(f"unsupported build adapter: {adapter}")
    return suffix, adapter, release_contract, variant


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
    matches = {match.group(1).decode("ascii", errors="strict") for match in LINUX_VERSION_RE.finditer(image)}
    if len(matches) != 1:
        raise ContractError(f"Image must contain one Linux version string, found {sorted(matches)}")
    return matches.pop()


def parse_module_vermagic(path: Path) -> str:
    try:
        module = path.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    matches = {match.group(1).decode("ascii", errors="strict") for match in VERMAGIC_RE.finditer(module)}
    if len(matches) != 1:
        raise ContractError(f"module must contain one vermagic field, found {len(matches)}")
    return matches.pop()


def validate_observed_release(
    actual_release: str,
    base_release: str,
    suffix: str,
    release_contract: Dict[str, str],
) -> None:
    if len(actual_release) > MAX_UTS_RELEASE_LENGTH:
        raise ContractError("observed release exceeds the UTS_RELEASE length limit")
    if release_contract["mode"] == "exact":
        expected = release_contract["expected_uname_release"]
        if actual_release != expected:
            raise ContractError(f"release is {actual_release}, expected {expected}")
        return
    if not actual_release.startswith(base_release) or not actual_release.endswith(suffix):
        raise ContractError(f"release {actual_release} violates its base/suffix boundary")
    if actual_release.count(base_release) != 1:
        raise ContractError("release repeats the base version; possible double-prefix bug")


def validate_module_vermagic(
    vermagic: str,
    base_release: str,
    suffix: str,
    release_contract: Dict[str, str],
    vivo_enabled: bool,
) -> None:
    parts = vermagic.split()
    if not parts:
        raise ContractError("module vermagic is empty")
    validate_observed_release(parts[0], base_release, suffix, release_contract)
    count = parts[1:].count("vivo")
    if vivo_enabled and count != 1:
        raise ContractError("Vivo LKM must contain exactly one vivo vermagic token")
    if not vivo_enabled and count:
        raise ContractError("standard LKM must not contain a vivo vermagic token")
    if vivo_enabled and "aarch64" in parts and parts.index("vivo") + 1 != parts.index("aarch64"):
        raise ContractError("vivo vermagic token must immediately precede aarch64")


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ContractError(f"cannot hash {path}: {error}") from error


def write_record(
    path: Path,
    plan_path: Path,
    plan: Dict[str, Any],
    variant_id: str,
    observed_base: str,
    observed_release: Optional[str],
    image: Optional[Path],
    module: Optional[Path],
    vermagic: Optional[str],
) -> None:
    selection = plan["selection"]
    record: Dict[str, Any] = {
        "schema": 5,
        "plan_sha256": sha256(plan_path),
        "variant_id": variant_id,
        "observed_base_release": observed_base,
        "selection": {
            key: selection.get(key)
            for key in ("release_id", "root_source", "susfs", "kpm", "vivo_vermagic", "uname_tag")
        },
    }
    if observed_release is not None:
        record["observed_uts_release"] = observed_release
    if image is not None:
        record["image_sha256"] = sha256(image)
    if module is not None:
        record["module_sha256"] = sha256(module)
        record["module_vermagic"] = vermagic
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--variant-id", required=True)
    parser.add_argument("--makefile", type=Path, required=True)
    parser.add_argument("--utsrelease-header", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--module", type=Path)
    parser.add_argument("--record", type=Path)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = load_json(args.plan)
        base = parse_base_release(args.makefile)
        suffix, _adapter, release_contract, variant = validate_plan(plan, args.variant_id, base)
        observed_release: Optional[str] = None
        if args.utsrelease_header:
            observed_release = parse_utsrelease(args.utsrelease_header)
            validate_observed_release(observed_release, base, suffix, release_contract)
        if args.image:
            image_release = parse_image_release(args.image)
            validate_observed_release(image_release, base, suffix, release_contract)
            if observed_release is not None and observed_release != image_release:
                raise ContractError("UTS_RELEASE and Image report different releases")
            observed_release = image_release
        vermagic: Optional[str] = None
        if args.module:
            vermagic = parse_module_vermagic(args.module)
            features = variant.get("features")
            if not isinstance(features, dict):
                raise ContractError("variant.features must be an object")
            validate_module_vermagic(
                vermagic, base, suffix, release_contract, features.get("vivo_vermagic") is True
            )
            module_release = vermagic.split()[0]
            if observed_release is not None and observed_release != module_release:
                raise ContractError("UTS_RELEASE and module vermagic report different releases")
            observed_release = module_release
        if args.record:
            write_record(
                args.record, args.plan, plan, args.variant_id, base,
                observed_release, args.image, args.module, vermagic,
            )
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
