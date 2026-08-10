#!/usr/bin/env python3
"""Materialize one locked KernelSU-family provider for a schema-5 variant."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from sync_google_gki import LockError, canonical_json, load_json


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
EXCLUDED_ROOT_RE = re.compile(r"(?:kernel.?su.?next|\bksun\b|\bksu.?next\b)", re.IGNORECASE)
PROVIDER_REPOSITORIES = {
    "kernelsu": "https://github.com/tiann/KernelSU.git",
    "sukisu": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "resukisu": "https://github.com/ReSukiSU/ReSukiSU.git",
}
ROOT_ADAPTER = "kernelsu-driver-link-v2"
MAKEFILE_LINE = "obj-$(CONFIG_KSU) += kernelsu/"
KCONFIG_LINE = 'source "drivers/kernelsu/Kconfig"'
KLEAF_FRAGMENT_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
LEGACY_BUILD_ADAPTER = "legacy-build-sh-arm64-v1"
MODULE_OUT = "drivers/kernelsu/kernelsu.ko"
KLEAF_MODULE_OUTS_RE = re.compile(
    r'^(?P<indent>[ \t]*)"module_implicit_outs": get_gki_modules_list\("arm64"\),$',
    re.MULTILINE,
)
KLEAF_MANAGED_MODULE_OUTS_RE = re.compile(
    r'^(?P<indent>[ \t]*)"module_implicit_outs": get_gki_modules_list\("arm64"\) \+ \[\n'
    r'(?P=indent)[ \t]{4}"drivers/kernelsu/kernelsu\.ko",\n'
    r'(?P=indent)\],$',
    re.MULTILINE,
)


class AdapterError(ValueError):
    """Raised when a root adapter cannot be applied without ambiguity."""


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise AdapterError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(document, dict):
        raise AdapterError(f"{path} must contain a JSON object")
    return document


def require_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{context}.{key} must be a non-empty string")
    return value


def require_identifier(value: str, context: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value) or EXCLUDED_ROOT_RE.search(value):
        raise AdapterError(f"{context} is not an allowed identifier")


def run(command: List[str], *, cwd: Optional[Path] = None) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}")
    try:
        return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as error:
        raise AdapterError(f"command failed ({rendered})\n{error.output.strip()}") from error


def load_root_lock(path: Path, lock_id: str) -> Dict[str, Any]:
    try:
        document = load_json(path)
    except LockError as error:
        raise AdapterError(str(error)) from error
    if not isinstance(document, dict) or document.get("schema") != 2:
        raise AdapterError("root source lock must be a schema-2 object")
    if document.get("kind") != "static-root-source-locks" or not isinstance(document.get("locks"), dict):
        raise AdapterError("root source lock has an invalid kind or locks object")
    lock = document["locks"].get(lock_id)
    if not isinstance(lock, dict) or lock.get("id") != lock_id:
        raise AdapterError(f"unknown root source lock: {lock_id}")
    require_identifier(lock_id, "root source lock id")
    provider = require_string(lock, "provider", lock_id)
    require_identifier(provider, f"{lock_id}.provider")
    if provider not in PROVIDER_REPOSITORIES:
        raise AdapterError(f"unsupported root provider: {provider}")
    repository = require_string(lock, "repository", lock_id)
    parsed = urlparse(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or repository != PROVIDER_REPOSITORIES[provider]
    ):
        raise AdapterError(f"{lock_id}.repository is not the audited {provider} upstream")
    if not SHA1_RE.fullmatch(require_string(lock, "commit", lock_id)):
        raise AdapterError(f"{lock_id}.commit must be a lowercase 40-character SHA-1")
    if require_string(lock, "ref", lock_id) != "main":
        raise AdapterError(f"{lock_id}.ref must record main provenance")
    if require_string(lock, "source_dir", lock_id) != "kernel":
        raise AdapterError(f"{lock_id}.source_dir must be kernel")
    if require_string(lock, "kernel_license", lock_id) != "GPL-2.0-only":
        raise AdapterError(f"{lock_id}.kernel_license must be GPL-2.0-only")
    return dict(lock)


def validate_plan(plan: Dict[str, Any], variant_id: str) -> Tuple[str, Optional[str], str]:
    if plan.get("schema") != 5:
        raise AdapterError("plan.schema must be 5")
    selection = plan.get("selection")
    root = plan.get("root")
    variants = plan.get("variants")
    build = plan.get("build")
    if not all(isinstance(value, dict) for value in (selection, root, build)):
        raise AdapterError("plan selection, root, and build must be objects")
    if not isinstance(variants, list):
        raise AdapterError("plan.variants must be an array")
    provider = require_string(selection, "root_source", "plan.selection")
    require_identifier(provider, "plan.selection.root_source")
    if root.get("id") != provider:
        raise AdapterError("plan.root.id must match plan.selection.root_source")
    matches = [item for item in variants if isinstance(item, dict) and item.get("id") == variant_id]
    if len(matches) != 1:
        raise AdapterError(f"variant is not present exactly once in plan: {variant_id}")
    linkage = require_string(matches[0], "root_linkage", f"plan variant {variant_id}")
    build_adapter = require_string(build, "adapter", "plan.build")
    if build_adapter not in {KLEAF_FRAGMENT_ADAPTER, LEGACY_BUILD_ADAPTER}:
        raise AdapterError(f"unsupported build adapter: {build_adapter}")
    if provider == "none":
        if variant_id != "baseline-image" or linkage != "none":
            raise AdapterError("root=none only permits baseline-image")
        if root.get("adapter") != "none" or root.get("source_lock") is not None or root.get("source") is not None:
            raise AdapterError("root=none has an invalid empty adapter contract")
        return provider, None, build_adapter
    if provider not in PROVIDER_REPOSITORIES or root.get("adapter") != ROOT_ADAPTER:
        raise AdapterError(f"unsupported root adapter: {provider}/{root.get('adapter')}")
    expected_linkage = {"builtin-image": "builtin", "lkm-module": "lkm"}.get(variant_id)
    if linkage != expected_linkage:
        raise AdapterError(f"variant {variant_id} has an invalid linkage")
    source = root.get("source")
    if not isinstance(source, dict) or source.get("provider") != provider:
        raise AdapterError("plan.root.source does not match the provider")
    return provider, require_string(root, "source_lock", "plan.root"), build_adapter


def assert_clean_checkout(directory: Path, expected_commit: str) -> None:
    if not directory.is_dir() or not (directory / ".git").exists():
        raise AdapterError(f"root checkout is missing Git metadata: {directory}")
    if run(["git", "-C", str(directory), "rev-parse", "HEAD"]).strip() != expected_commit:
        raise AdapterError("root checkout does not match its lock commit")
    if run(["git", "-C", str(directory), "rev-parse", "--is-shallow-repository"]).strip() != "false":
        raise AdapterError("root checkout must not be shallow")
    if run(["git", "-C", str(directory), "status", "--porcelain"]).strip():
        raise AdapterError("root checkout has local modifications")


def checkout_provider(lock: Dict[str, Any], kernel_workspace: Path) -> Path:
    """Fetch only the exact locked SHA while retaining non-shallow Git metadata."""

    destination = kernel_workspace / "KernelSU"
    if destination.exists():
        raise AdapterError(f"refusing to replace existing root checkout: {destination}")
    run(["git", "init", "-q", str(destination)])
    run(["git", "-C", str(destination), "remote", "add", "origin", lock["repository"]])
    run(["git", "-C", str(destination), "-c", "protocol.version=2", "fetch", "--no-tags", "origin", lock["commit"]])
    run(["git", "-C", str(destination), "checkout", "--detach", "--quiet", lock["commit"]])
    assert_clean_checkout(destination, lock["commit"])
    return destination


def add_line_once(path: Path, line: str, *, before_last_endmenu: bool = False) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(f"cannot read {path}: {error}") from error
    count = content.count(line)
    if count == 1:
        return
    if count > 1:
        raise AdapterError(f"{path} registers KernelSU more than once")
    if before_last_endmenu:
        index = content.rfind("\nendmenu")
        if index < 0:
            raise AdapterError(f"cannot locate final endmenu in {path}")
        content = content[: index + 1] + line + "\n" + content[index + 1 :]
    else:
        content = content.rstrip("\n") + "\n" + line + "\n"
    path.write_text(content, encoding="utf-8", newline="\n")


def create_symlink(destination: Path, source: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() and destination.resolve() == source.resolve():
            return
        raise AdapterError(f"refusing to replace existing adapter path: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(os.path.relpath(source, destination.parent))
    if not destination.is_symlink() or destination.resolve() != source.resolve():
        raise AdapterError("root adapter symlink could not be verified")


def declare_kleaf_lkm_module(build_file: Path) -> None:
    try:
        content = build_file.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(f"cannot read Kleaf BUILD file {build_file}: {error}") from error
    if MODULE_OUT in content:
        if content.count(MODULE_OUT) != 1 or len(KLEAF_MANAGED_MODULE_OUTS_RE.findall(content)) != 1:
            raise AdapterError("Kleaf BUILD file has a non-canonical KernelSU module declaration")
        return
    matches = list(KLEAF_MODULE_OUTS_RE.finditer(content))
    if len(matches) != 1:
        raise AdapterError("Kleaf BUILD file must contain one arm64 module_implicit_outs anchor")
    match = matches[0]
    indent = match.group("indent")
    replacement = (
        f'{indent}"module_implicit_outs": get_gki_modules_list("arm64") + [\n'
        f'{indent}    "{MODULE_OUT}",\n'
        f"{indent}],"
    )
    build_file.write_text(content[: match.start()] + replacement + content[match.end() :], encoding="utf-8", newline="\n")


def apply_provider(
    lock: Dict[str, Any], kernel_workspace: Path, variant_id: str, build_adapter: str
) -> Dict[str, Any]:
    """Register one provider; configuration policy remains outside this adapter."""

    if variant_id not in {"builtin-image", "lkm-module"}:
        raise AdapterError(f"unsupported provider variant: {variant_id}")
    if build_adapter not in {KLEAF_FRAGMENT_ADAPTER, LEGACY_BUILD_ADAPTER}:
        raise AdapterError(f"unsupported build adapter: {build_adapter}")
    checkout = checkout_provider(lock, kernel_workspace)
    source = checkout / lock["source_dir"]
    if not (source / "Kbuild").is_file() or not (source / "Kconfig").is_file():
        raise AdapterError("locked provider has no supported kernel adapter layout")
    drivers = kernel_workspace / "common" / "drivers"
    if not drivers.is_dir():
        raise AdapterError("GKI workspace lacks common/drivers")
    create_symlink(drivers / "kernelsu", source)
    add_line_once(drivers / "Makefile", MAKEFILE_LINE)
    add_line_once(drivers / "Kconfig", KCONFIG_LINE, before_last_endmenu=True)
    record: Dict[str, Any] = {
        "provider": lock["provider"],
        "adapter": ROOT_ADAPTER,
        "variant_id": variant_id,
        "source_lock": lock["id"],
        "repository": lock["repository"],
        "commit": lock["commit"],
        "ref": lock["ref"],
        "checkout": "KernelSU",
        "checkout_mode": "detached-commit",
        "driver_link": "common/drivers/kernelsu",
        "makefile_registration": MAKEFILE_LINE,
        "kconfig_registration": KCONFIG_LINE,
    }
    if variant_id == "lkm-module" and build_adapter == KLEAF_FRAGMENT_ADAPTER:
        declare_kleaf_lkm_module(kernel_workspace / "common" / "BUILD.bazel")
        record["module_out"] = MODULE_OUT
    return record


def write_record(kernel_workspace: Path, record: Dict[str, Any]) -> None:
    (kernel_workspace / "renebula-root-record.json").write_bytes(
        canonical_json({"schema": 2, **record}) + b"\n"
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--variant-id", required=True)
    parser.add_argument("--kernel-workspace", type=Path, required=True)
    parser.add_argument("--root-locks", type=Path, default=Path(__file__).resolve().parents[1] / "locks" / "root-sources.lock.json")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = read_json(args.plan)
        workspace = args.kernel_workspace.resolve()
        if not workspace.is_dir():
            raise AdapterError(f"kernel workspace is not a directory: {workspace}")
        provider, lock_id, build_adapter = validate_plan(plan, args.variant_id)
        if provider == "none":
            record = {"provider": "none", "adapter": "none", "variant_id": args.variant_id}
        else:
            lock = load_root_lock(args.root_locks.resolve(), lock_id or "")
            planned = plan["root"]["source"]
            for key in ("id", "provider", "repository", "commit", "ref", "source_dir"):
                if planned.get(key) != lock.get(key):
                    raise AdapterError(f"plan root source drifted from root lock: {key}")
            record = apply_provider(lock, workspace, args.variant_id, build_adapter)
        write_record(workspace, record)
    except AdapterError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
