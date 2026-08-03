#!/usr/bin/env python3
"""Resolve one static ReNebula release selection into canonical JSON.

``family_id`` describes a stable KMI/build compatibility contract, while a
``release_id`` describes one immutable Google source snapshot.  The dispatcher
may select only a committed release id from ``profiles/registry.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sync_google_gki import LockError, load_json, load_lock, validate_relative_path


SUFFIX_RE = re.compile(r"^-[A-Za-z0-9._-]+$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
KLEAF_FRAGMENT_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
KLEAF_ADAPTERS = {KLEAF_FRAGMENT_ADAPTER}
LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"
RELEASE_STATES = {"source-locked", "image-verified", "verified", "deprecated"}


class PlanError(ValueError):
    """Raised when a registry entry cannot yield a safe immutable plan."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PlanError(f"cannot hash {path}: {error}") from error


def require_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise PlanError(f"{context}.{key} must be a non-empty string")
    return value


def require_identifier(value: str, context: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise PlanError(f"{context} must be a lowercase identifier")


def safe_profile_path(repo_root: Path, relative_path: str, context: str) -> Path:
    try:
        validate_relative_path(relative_path, context)
    except LockError as error:
        raise PlanError(str(error)) from error
    path = (repo_root / relative_path).resolve()
    profiles_root = (repo_root / "profiles").resolve()
    try:
        path.relative_to(profiles_root)
    except ValueError as error:
        raise PlanError(f"{context} must be inside profiles/") from error
    return path


def load_object(path: Path, context: str) -> Dict[str, Any]:
    try:
        document = load_json(path)
    except LockError as error:
        raise PlanError(str(error)) from error
    if not isinstance(document, dict):
        raise PlanError(f"{context} must be a JSON object")
    return document


def load_registry(repo_root: Path) -> Tuple[Dict[str, Any], Path]:
    path = repo_root / "profiles" / "registry.json"
    registry = load_object(path, "release registry")
    if registry.get("schema") != 2 or registry.get("kind") != "static-release-registry":
        raise PlanError("release registry must be a schema-2 static-release-registry")
    releases = registry.get("releases")
    if not isinstance(releases, list) or not releases:
        raise PlanError("release registry must contain a non-empty releases array")
    if len(releases) > 25:
        raise PlanError("release registry exceeds the static workflow input limit")
    seen = set()
    for index, entry in enumerate(releases):
        context = f"release registry.releases[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{context} must be an object")
        release_id = require_string(entry, "id", context)
        family_id = require_string(entry, "family_id", context)
        require_identifier(release_id, f"{context}.id")
        require_identifier(family_id, f"{context}.family_id")
        if release_id in seen:
            raise PlanError(f"release registry repeats id: {release_id}")
        seen.add(release_id)
        state = require_string(entry, "state", context)
        if state not in RELEASE_STATES:
            raise PlanError(f"{context}.state is unsupported: {state}")
        profile = require_string(entry, "profile", context)
        expected_profile = f"profiles/releases/{release_id}.json"
        if profile != expected_profile:
            raise PlanError(f"{context}.profile must be {expected_profile}")
        safe_profile_path(repo_root, profile, f"{context}.profile")
    return registry, path


def registry_entries(registry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {entry["id"]: entry for entry in registry["releases"]}


def load_release(
    repo_root: Path, release_id: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Path]:
    entry = registry_entries(registry).get(release_id)
    if entry is None:
        raise PlanError(f"release is not registered: {release_id}")
    path = safe_profile_path(repo_root, entry["profile"], "registry release profile")
    release = load_object(path, "release profile")
    if release.get("schema") != 2 or release.get("kind") != "gki-release":
        raise PlanError("release profile must be a schema-2 gki-release")
    if require_string(release, "id", "release profile") != release_id:
        raise PlanError("release profile id does not match registry")
    family_id = require_string(release, "family_id", "release profile")
    if family_id != entry["family_id"]:
        raise PlanError("release profile family_id does not match registry")
    state = require_string(release, "state", "release profile")
    if state != entry["state"] or state not in RELEASE_STATES:
        raise PlanError("release profile state does not match registry")
    require_string(release, "source_lock", "release profile")
    version = release.get("version")
    if not isinstance(version, dict):
        raise PlanError("release profile.version must be an object")
    base_release = require_string(version, "expected_base_release", "release profile.version")
    suffix = require_string(version, "local_suffix", "release profile.version")
    if not SUFFIX_RE.fullmatch(suffix):
        raise PlanError(
            "release profile.version.local_suffix must use only letters, digits, '.', '_', and '-'"
        )
    if base_release in suffix:
        raise PlanError("release profile.version.local_suffix must not repeat the base release")
    return entry, release, path


def load_family(repo_root: Path, family_id: str) -> Tuple[Dict[str, Any], Path]:
    require_identifier(family_id, "family id")
    path = repo_root / "profiles" / "families" / f"{family_id}.json"
    family = load_object(path, "family profile")
    if family.get("schema") != 2 or family.get("kind") != "kmi-family":
        raise PlanError("family profile must be a schema-2 kmi-family")
    if require_string(family, "id", "family profile") != family_id:
        raise PlanError("family profile id does not match its filename")
    if family.get("roots") != ["none"]:
        raise PlanError("P0 family must expose only root=none")
    if family.get("features") != []:
        raise PlanError("P0 family must not expose optional features")
    build = family.get("build")
    if not isinstance(build, dict):
        raise PlanError("family profile.build must be an object")
    adapter = require_string(build, "adapter", "family profile.build")
    image_name = require_string(build, "image_name", "family profile.build")
    if image_name != Path(image_name).name:
        raise PlanError("family profile.build.image_name must be a file name")
    if adapter in KLEAF_ADAPTERS:
        if require_string(build, "bazel_target", "family profile.build") != "//common:kernel_aarch64_dist":
            raise PlanError("Kleaf family must use the audited arm64 GKI target")
    elif adapter == LEGACY_ADAPTER:
        if require_string(build, "build_config", "family profile.build") != "common/build.config.gki.aarch64":
            raise PlanError("legacy family must use the audited arm64 GKI build config")
    else:
        raise PlanError(f"unsupported family build adapter: {adapter}")
    return family, path


def resolve_plan(repo_root: Path, release_id: str) -> Dict[str, Any]:
    require_identifier(release_id, "release id")
    registry, registry_path = load_registry(repo_root)
    entry, release, release_path = load_release(repo_root, release_id, registry)
    family, family_path = load_family(repo_root, entry["family_id"])
    lock_path = repo_root / "locks" / "sources.lock.json"
    lock_id = release["source_lock"]
    try:
        lock = load_lock(lock_path, lock_id)
    except LockError as error:
        raise PlanError(str(error)) from error
    if lock["family_id"] != entry["family_id"]:
        raise PlanError("source lock family_id does not match release")
    if lock["release_id"] != release_id:
        raise PlanError("source lock release_id does not match release")
    expected_base_release = release["version"]["expected_base_release"]
    if expected_base_release != lock["version"]["expected_base_release"]:
        raise PlanError("release base version does not match the selected source lock")
    local_suffix = release["version"]["local_suffix"]
    build = dict(family["build"])
    if build["adapter"] == LEGACY_ADAPTER:
        release_contract: Dict[str, str] = {
            "mode": "exact",
            "expected_uname_release": expected_base_release + local_suffix,
        }
    elif build["adapter"] == KLEAF_FRAGMENT_ADAPTER:
        # Modern Kleaf independently materializes a Google localversion file.
        # Its locked content precedes our Kconfig suffix, so source-locked
        # releases assert the stable boundary rather than pretending no Google
        # provenance segment exists.  An image-verified release may later pin
        # the full observed value as evidence.
        release_contract = {
            "mode": "base-prefix-and-suffix",
            "prefix": expected_base_release,
            "suffix": local_suffix,
        }
    else:
        raise PlanError(f"unsupported family build adapter: {build['adapter']}")
    return {
        "schema": 2,
        "selection": {
            "family_id": entry["family_id"],
            "release_id": release_id,
            "state": release["state"],
            "root": "none",
            "features": [],
        },
        "definition": {
            "registry_sha256": sha256_file(registry_path),
            "family_sha256": sha256_file(family_path),
            "release_sha256": sha256_file(release_path),
            "sources_lock_sha256": sha256_file(lock_path),
        },
        "source": {
            "lock_id": lock_id,
            "manifest_commit": lock["manifest"]["commit"],
            "superproject_commit": lock["superproject"]["commit"],
            "manifest_ref": lock["superproject"]["manifest_ref"],
            "common_commit": lock["common"]["commit"],
        },
        "build": build,
        "version": {
            "expected_base_release": expected_base_release,
            "local_suffix": local_suffix,
            "release_contract": release_contract,
        },
    }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True, help="static release identifier")
    parser.add_argument("--output", type=Path, required=True, help="canonical build-plan JSON output")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="ReNebula repository root",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = resolve_plan(args.repo_root.resolve(), args.release_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(plan) + b"\n")
    except (LockError, PlanError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
