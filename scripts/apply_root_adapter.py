#!/usr/bin/env python3
"""Apply a SHA-pinned ReNebula root adapter to a materialized GKI checkout.

This is deliberately separate from Google source synchronization.  A root
provider is a second immutable input with its own lock and provenance record;
it is never a branch name, a setup script, or an unchecked configuration bit.
"""

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
RESUKISU_REPOSITORY = "https://github.com/ReSukiSU/ReSukiSU.git"
RESUKISU_ADAPTER = "resukisu-driver-link-v1"
RESUKISU_MAKEFILE_LINE = "obj-$(CONFIG_KSU) += kernelsu/"
RESUKISU_KCONFIG_LINE = 'source "drivers/kernelsu/Kconfig"'
KLEAF_FRAGMENT_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
LEGACY_BUILD_ADAPTER = "legacy-build-sh-arm64-v1"
RESUKISU_MODULE_OUT = "drivers/kernelsu/kernelsu.ko"
KLEAF_MODULE_OUTS_RE = re.compile(
    r'^(?P<indent>[ \t]*)"module_implicit_outs": get_gki_modules_list\("arm64"\),$',
    re.MULTILINE,
)
KLEAF_RESUKISU_MODULE_OUTS_RE = re.compile(
    r'^(?P<indent>[ \t]*)"module_implicit_outs": get_gki_modules_list\("arm64"\) \+ \[\n'
    r'(?P=indent)[ \t]{4}"drivers/kernelsu/kernelsu\.ko",\n'
    r'(?P=indent)\],$',
    re.MULTILINE,
)


class AdapterError(ValueError):
    """Raised when a root adapter cannot safely be applied."""


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
    except OSError as error:
        raise AdapterError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise AdapterError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(document, dict):
        raise AdapterError(f"{path} must contain a JSON object")
    return document


def require_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{context}.{key} must be a non-empty string")
    return value


def require_identifier(value: str, context: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise AdapterError(f"{context} must be a lowercase identifier")


def run(command: List[str], *, cwd: Optional[Path] = None) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}")
    try:
        return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as error:
        raise AdapterError(
            f"command failed ({rendered})\n{error.output.strip()}"
        ) from error


def load_root_lock(path: Path, lock_id: str) -> Dict[str, Any]:
    try:
        document = load_json(path)
    except LockError as error:
        raise AdapterError(str(error)) from error
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise AdapterError("root source lock must be a schema-1 object")
    if document.get("kind") != "static-root-source-locks":
        raise AdapterError("root source lock has an unexpected kind")
    locks = document.get("locks")
    if not isinstance(locks, dict):
        raise AdapterError("root source lock must contain locks")
    lock = locks.get(lock_id)
    if not isinstance(lock, dict):
        raise AdapterError(f"unknown root source lock: {lock_id}")
    require_identifier(lock_id, "root source lock id")
    if require_string(lock, "id", lock_id) != lock_id:
        raise AdapterError(f"{lock_id}.id must equal its enclosing key")
    if require_string(lock, "provider", lock_id) != "resukisu":
        raise AdapterError(f"{lock_id}.provider must be resukisu")
    repository = require_string(lock, "repository", lock_id)
    parsed = urlparse(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or repository != RESUKISU_REPOSITORY
    ):
        raise AdapterError(f"{lock_id}.repository must be {RESUKISU_REPOSITORY}")
    commit = require_string(lock, "commit", lock_id)
    if not SHA1_RE.fullmatch(commit):
        raise AdapterError(f"{lock_id}.commit must be a lowercase 40-character SHA-1")
    ref = require_string(lock, "ref", lock_id)
    if ref != "main":
        raise AdapterError(f"{lock_id}.ref must be the provenance ref main")
    source_dir = require_string(lock, "source_dir", lock_id)
    if source_dir != "kernel":
        raise AdapterError(f"{lock_id}.source_dir must be kernel")
    if require_string(lock, "repository_license", lock_id) != "GPL-3.0":
        raise AdapterError(f"{lock_id}.repository_license must be GPL-3.0")
    if require_string(lock, "kernel_license", lock_id) != "GPL-2.0-only":
        raise AdapterError(f"{lock_id}.kernel_license must be GPL-2.0-only")
    return lock


def validate_plan(plan: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    if plan.get("schema") != 4:
        raise AdapterError("plan.schema must be 4")
    selection = plan.get("selection")
    if not isinstance(selection, dict):
        raise AdapterError("plan.selection must be an object")
    root_provider = require_string(selection, "root_provider", "plan.selection")
    require_identifier(root_provider, "plan.selection.root_provider")
    root_linkage = require_string(selection, "root_linkage", "plan.selection")
    require_identifier(root_linkage, "plan.selection.root_linkage")
    hook_mode = require_string(selection, "hook_mode", "plan.selection")
    require_identifier(hook_mode, "plan.selection.hook_mode")
    config_profile = require_string(selection, "config_profile", "plan.selection")
    require_identifier(config_profile, "plan.selection.config_profile")
    root = plan.get("root")
    if not isinstance(root, dict):
        raise AdapterError("plan.root must be an object")
    if require_string(root, "id", "plan.root") != root_provider:
        raise AdapterError("plan.root.id must match plan.selection.root_provider")
    if require_string(root, "linkage", "plan.root") != root_linkage:
        raise AdapterError("plan.root.linkage must match plan.selection.root_linkage")
    if require_string(root, "hook_mode", "plan.root") != hook_mode:
        raise AdapterError("plan.root.hook_mode must match plan.selection.hook_mode")
    configuration = plan.get("configuration")
    if not isinstance(configuration, dict):
        raise AdapterError("plan.configuration must be an object")
    if require_string(configuration, "id", "plan.configuration") != config_profile:
        raise AdapterError("plan.configuration.id must match plan.selection.config_profile")
    adapter = require_string(root, "adapter", "plan.root")
    if root_provider == "none":
        if (
            adapter != "none"
            or root.get("source_lock") is not None
            or root.get("source") is not None
            or root_linkage != "none"
            or hook_mode != "none"
            or config_profile != "release"
        ):
            raise AdapterError("root=none must use the empty release adapter contract")
        return root_provider, None
    if root_provider != "resukisu" or adapter != RESUKISU_ADAPTER:
        raise AdapterError(f"unsupported root adapter: {root_provider}/{adapter}")
    if root_linkage not in {"lkm", "builtin"} or hook_mode != "tracepoint":
        raise AdapterError("ReSukiSU requires lkm/builtin with the Tracepoint hook")
    if config_profile not in {"release", "debug"}:
        raise AdapterError("ReSukiSU requires a release or debug configuration profile")
    return root_provider, require_string(root, "source_lock", "plan.root")


def assert_clean_checkout(directory: Path, expected_commit: str) -> None:
    if not directory.is_dir() or not (directory / ".git").exists():
        raise AdapterError(f"root checkout is missing Git metadata: {directory}")
    if run(["git", "-C", str(directory), "rev-parse", "HEAD"]).strip() != expected_commit:
        raise AdapterError("root checkout does not match its lock commit")
    if run(["git", "-C", str(directory), "rev-parse", "--is-shallow-repository"]).strip() != "false":
        raise AdapterError("root checkout must not be shallow")
    if run(["git", "-C", str(directory), "status", "--porcelain"]).strip():
        raise AdapterError("root checkout has local modifications")


def checkout_resukisu(lock: Dict[str, Any], kernel_workspace: Path) -> Path:
    """Materialize exactly the locked commit without treating ``ref`` as input.

    ``ref`` is provenance only.  This checkout intentionally never resolves,
    fetches, or checks out ``main``; only the immutable SHA is fetched and
    detached.  The history is deliberately non-shallow because ReSukiSU's
    build metadata can inspect its enclosing repository.
    """

    # ReSukiSU's Kbuild resolves repository metadata through this exact
    # conventional checkout name.  The provider name is ReSukiSU, but the
    # materialized directory must remain KernelSU for the upstream adapter.
    destination = kernel_workspace / "KernelSU"
    if destination.exists():
        raise AdapterError(f"refusing to replace existing root checkout: {destination}")
    run(["git", "init", "-q", str(destination)])
    run(["git", "-C", str(destination), "remote", "add", "origin", lock["repository"]])
    # Do not shallow-fetch, and do not follow lock["ref"] at runtime.
    run(
        [
            "git",
            "-C",
            str(destination),
            "-c",
            "protocol.version=2",
            "fetch",
            "--no-tags",
            "origin",
            lock["commit"],
        ]
    )
    run(["git", "-C", str(destination), "checkout", "--detach", "--quiet", lock["commit"]])
    assert_clean_checkout(destination, lock["commit"])
    return destination


def add_line_once(path: Path, line: str, *, before_last_endmenu: bool = False) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(f"cannot read {path}: {error}") from error
    occurrences = content.count(line)
    if occurrences == 1:
        return
    if occurrences > 1:
        raise AdapterError(f"{path} registers the root adapter more than once")
    if before_last_endmenu:
        marker = "\nendmenu"
        index = content.rfind(marker)
        if index < 0:
            raise AdapterError(f"cannot locate final endmenu in {path}")
        content = content[: index + 1] + line + "\n" + content[index + 1 :]
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += line + "\n"
    path.write_text(content, encoding="utf-8", newline="\n")


def create_symlink(destination: Path, source: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() and destination.resolve() == source.resolve():
            return
        raise AdapterError(f"refusing to replace existing root adapter path: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(os.path.relpath(source, destination.parent))
    if not destination.is_symlink() or destination.resolve() != source.resolve():
        raise AdapterError("root adapter symlink could not be verified")


def declare_kleaf_lkm_module(build_file: Path) -> Dict[str, str]:
    """Declare the one ReSuki LKM in Kleaf's audited module output list.

    Kleaf rejects generated modules that are absent from ``module_implicit_outs``.
    Replacing this unique, lock-specific anchor is intentionally narrower than
    permitting arbitrary undeclared modules: a layout drift fails before build.
    """

    try:
        content = build_file.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(f"cannot read Kleaf BUILD file {build_file}: {error}") from error
    existing_count = content.count(RESUKISU_MODULE_OUT)
    if existing_count:
        if existing_count != 1:
            raise AdapterError("Kleaf BUILD file declares the ReSukiSU module more than once")
        if len(KLEAF_RESUKISU_MODULE_OUTS_RE.findall(content)) != 1 or KLEAF_MODULE_OUTS_RE.search(content):
            raise AdapterError(
                "Kleaf BUILD file does not contain the exact audited ReSukiSU module declaration"
            )
        return {
            "build_file": "common/BUILD.bazel",
            "module_out": RESUKISU_MODULE_OUT,
            "mode": "ensured",
        }

    matches = list(KLEAF_MODULE_OUTS_RE.finditer(content))
    if len(matches) != 1:
        raise AdapterError(
            "Kleaf BUILD file must contain exactly one audited arm64 module_implicit_outs anchor"
        )
    match = matches[0]
    indent = match.group("indent")
    replacement = (
        f'{indent}"module_implicit_outs": get_gki_modules_list("arm64") + [\n'
        f'{indent}    "{RESUKISU_MODULE_OUT}",\n'
        f"{indent}],"
    )
    content = content[: match.start()] + replacement + content[match.end() :]
    build_file.write_text(content, encoding="utf-8", newline="\n")
    return {
        "build_file": "common/BUILD.bazel",
        "module_out": RESUKISU_MODULE_OUT,
        "mode": "ensured",
    }


def apply_resukisu(
    lock: Dict[str, Any],
    kernel_workspace: Path,
    *,
    root_linkage: str,
    build_adapter: str,
) -> Dict[str, Any]:
    checkout = checkout_resukisu(lock, kernel_workspace)
    source = checkout / lock["source_dir"]
    if not (source / "Kbuild").is_file() or not (source / "Kconfig").is_file():
        raise AdapterError("locked ReSukiSU checkout has no supported kernel adapter layout")
    drivers = kernel_workspace / "common" / "drivers"
    if not drivers.is_dir():
        raise AdapterError("GKI workspace lacks common/drivers")
    create_symlink(drivers / "kernelsu", source)
    add_line_once(drivers / "Makefile", RESUKISU_MAKEFILE_LINE)
    add_line_once(drivers / "Kconfig", RESUKISU_KCONFIG_LINE, before_last_endmenu=True)
    record: Dict[str, Any] = {
        "id": "resukisu",
        "adapter": RESUKISU_ADAPTER,
        "source_lock": lock["id"],
        "repository": lock["repository"],
        "commit": lock["commit"],
        "ref": lock["ref"],
        "checkout": "KernelSU",
        "checkout_mode": "detached-commit",
        "driver_link": "common/drivers/kernelsu",
        "makefile_registration": RESUKISU_MAKEFILE_LINE,
        "kconfig_registration": RESUKISU_KCONFIG_LINE,
    }
    if root_linkage == "lkm" and build_adapter == KLEAF_FRAGMENT_ADAPTER:
        record["kleaf_module_declaration"] = declare_kleaf_lkm_module(
            kernel_workspace / "common" / "BUILD.bazel"
        )
    elif build_adapter not in {KLEAF_FRAGMENT_ADAPTER, LEGACY_BUILD_ADAPTER}:
        raise AdapterError(f"unsupported build adapter for ReSukiSU: {build_adapter}")
    return record


def write_record(kernel_workspace: Path, record: Dict[str, Any]) -> None:
    path = kernel_workspace / "renebula-root-record.json"
    path.write_bytes(canonical_json({"schema": 1, **record}) + b"\n")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--kernel-workspace", type=Path, required=True)
    parser.add_argument(
        "--root-locks",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "locks" / "root-sources.lock.json",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = read_json(args.plan)
        kernel_workspace = args.kernel_workspace.resolve()
        if not kernel_workspace.is_dir():
            raise AdapterError(f"kernel workspace is not a directory: {kernel_workspace}")
        provider, lock_id = validate_plan(plan)
        if provider == "none":
            write_record(kernel_workspace, {"id": "none", "adapter": "none"})
        else:
            lock = load_root_lock(args.root_locks.resolve(), lock_id or "")
            selection = plan["selection"]
            build = plan.get("build")
            if not isinstance(build, dict):
                raise AdapterError("plan.build must be an object")
            build_adapter = require_string(build, "adapter", "plan.build")
            write_record(
                kernel_workspace,
                apply_resukisu(
                    lock,
                    kernel_workspace,
                    root_linkage=require_string(
                        selection, "root_linkage", "plan.selection"
                    ),
                    build_adapter=build_adapter,
                ),
            )
    except AdapterError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
