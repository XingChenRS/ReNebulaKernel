#!/usr/bin/env python3
"""Compile one public ReNebula request into an immutable schema-5 plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sync_google_gki import LockError, load_json, load_lock, validate_relative_path


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
UNAME_TAG_RE = re.compile(r"^(?:|[A-Za-z0-9][A-Za-z0-9._-]*)$")
EXCLUDED_ROOT_RE = re.compile(r"(?:kernel.?su.?next|\bksun\b|\bksu.?next\b)", re.IGNORECASE)
MAX_UTS_RELEASE_LENGTH = 64
GOOGLE_LOCALVERSION_BUDGET = {
    "kleaf-defconfig-fragment-arm64-v1": 25,
    "legacy-build-sh-arm64-v1": 32,
}
RELEASE_STATES = {"source-locked", "source-verified", "image-verified", "deprecated"}
KLEAF_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"
ROOT_REPOSITORIES = {
    "kernelsu": "https://github.com/tiann/KernelSU.git",
    "sukisu": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "resukisu": "https://github.com/ReSukiSU/ReSukiSU.git",
}
PROVIDER_TOKENS = {"kernelsu": "k", "sukisu": "s", "resukisu": "r"}


class PlanError(ValueError):
    """Raised when a public request cannot produce a safe immutable plan."""


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
    if EXCLUDED_ROOT_RE.search(value):
        raise PlanError(f"{context} names excluded KernelSU-Next support")


def load_object(path: Path, context: str) -> Dict[str, Any]:
    try:
        value = load_json(path)
    except LockError as error:
        raise PlanError(str(error)) from error
    if not isinstance(value, dict):
        raise PlanError(f"{context} must be a JSON object")
    return value


def safe_profile_path(repo_root: Path, relative_path: str, context: str) -> Path:
    try:
        validate_relative_path(relative_path, context)
    except LockError as error:
        raise PlanError(str(error)) from error
    path = (repo_root / relative_path).resolve()
    try:
        path.relative_to((repo_root / "profiles").resolve())
    except ValueError as error:
        raise PlanError(f"{context} must stay inside profiles/") from error
    return path


def _validate_index(repo_root: Path, registry: Dict[str, Any], key: str, directory: str) -> None:
    entries = registry.get(key)
    if not isinstance(entries, list) or not entries:
        raise PlanError(f"selector registry.{key} must be a non-empty array")
    seen = set()
    for index, entry in enumerate(entries):
        context = f"selector registry.{key}[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{context} must be an object")
        identifier = require_string(entry, "id", context)
        require_identifier(identifier, f"{context}.id")
        if identifier in seen:
            raise PlanError(f"selector registry repeats {key} id: {identifier}")
        seen.add(identifier)
        profile = require_string(entry, "profile", context)
        expected = f"profiles/{directory}/{identifier}.json"
        if profile != expected:
            raise PlanError(f"{context}.profile must be {expected}")
        safe_profile_path(repo_root, profile, f"{context}.profile")


def load_registry(repo_root: Path) -> Tuple[Dict[str, Any], Path]:
    path = repo_root / "profiles" / "registry.json"
    registry = load_object(path, "selector registry")
    if registry.get("schema") != 5 or registry.get("kind") != "static-request-registry":
        raise PlanError("selector registry must be a schema-5 static-request-registry")
    releases = registry.get("releases")
    if not isinstance(releases, list) or not releases or len(releases) > 25:
        raise PlanError("selector registry.releases must contain 1 through 25 entries")
    seen = set()
    for index, entry in enumerate(releases):
        context = f"selector registry.releases[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{context} must be an object")
        release_id = require_string(entry, "id", context)
        family_id = require_string(entry, "family_id", context)
        require_identifier(release_id, f"{context}.id")
        require_identifier(family_id, f"{context}.family_id")
        if release_id in seen:
            raise PlanError(f"selector registry repeats release id: {release_id}")
        seen.add(release_id)
        if require_string(entry, "state", context) not in RELEASE_STATES:
            raise PlanError(f"{context}.state is unsupported")
        expected = f"profiles/releases/{release_id}.json"
        if require_string(entry, "profile", context) != expected:
            raise PlanError(f"{context}.profile must be {expected}")
        safe_profile_path(repo_root, expected, f"{context}.profile")
    _validate_index(repo_root, registry, "root_sources", "root-providers")
    _validate_index(repo_root, registry, "features", "features")
    if [item["id"] for item in registry["root_sources"]] != [
        "none", "kernelsu", "sukisu", "resukisu"
    ]:
        raise PlanError("root_sources must be exactly none, kernelsu, sukisu, resukisu")
    return registry, path


def _index(registry: Dict[str, Any], key: str) -> Dict[str, Dict[str, Any]]:
    return {entry["id"]: entry for entry in registry[key]}


def load_release(
    repo_root: Path, release_id: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Path]:
    entry = next((item for item in registry["releases"] if item["id"] == release_id), None)
    if entry is None:
        raise PlanError(f"release id is not registered: {release_id}")
    path = safe_profile_path(repo_root, entry["profile"], "release profile")
    release = load_object(path, "release profile")
    if release.get("schema") != 5 or release.get("kind") != "gki-release":
        raise PlanError("release profile must be a schema-5 gki-release")
    if require_string(release, "id", "release profile") != release_id:
        raise PlanError("release profile id does not match registry")
    if require_string(release, "family_id", "release profile") != entry["family_id"]:
        raise PlanError("release profile family_id does not match registry")
    if require_string(release, "state", "release profile") != entry["state"]:
        raise PlanError("release profile state does not match registry")
    version = release.get("version")
    if not isinstance(version, dict):
        raise PlanError("release profile.version must be an object")
    require_string(version, "expected_base_release", "release profile.version")
    prefix = require_string(version, "local_suffix_prefix", "release profile.version")
    if prefix != "-RN4":
        raise PlanError("release local suffix prefix must be the compact ReNebula v4 marker")
    return entry, release, path


def load_family(repo_root: Path, family_id: str) -> Tuple[Dict[str, Any], Path]:
    require_identifier(family_id, "family id")
    path = repo_root / "profiles" / "families" / f"{family_id}.json"
    family = load_object(path, "family profile")
    if family.get("schema") != 5 or family.get("kind") != "kmi-family":
        raise PlanError("family profile must be a schema-5 kmi-family")
    if require_string(family, "id", "family profile") != family_id:
        raise PlanError("family profile id does not match filename")
    series = require_string(family, "kernel_series", "family profile")
    if not re.fullmatch(r"\d+\.\d+", series):
        raise PlanError("family profile.kernel_series is invalid")
    roots = family.get("root_sources")
    if roots != ["none", "kernelsu", "sukisu", "resukisu"]:
        raise PlanError("family profile.root_sources must expose the complete root chain")
    features = family.get("features")
    if not isinstance(features, dict) or set(features) != {"susfs", "kpm", "vivo_vermagic"}:
        raise PlanError("family profile.features is invalid")
    if not all(isinstance(value, bool) for value in features.values()):
        raise PlanError("family feature capabilities must be booleans")
    build = family.get("build")
    if not isinstance(build, dict):
        raise PlanError("family profile.build must be an object")
    adapter = require_string(build, "adapter", "family profile.build")
    if adapter == KLEAF_ADAPTER:
        if build.get("bazel_target") != "//common:kernel_aarch64_dist":
            raise PlanError("Kleaf family must use the audited arm64 GKI target")
    elif adapter == LEGACY_ADAPTER:
        if build.get("build_config") != "common/build.config.gki.aarch64":
            raise PlanError("legacy family must use the audited arm64 GKI config")
    else:
        raise PlanError(f"unsupported family build adapter: {adapter}")
    return family, path


def load_root_provider(
    repo_root: Path, root_source: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Path]:
    entry = _index(registry, "root_sources").get(root_source)
    if entry is None:
        raise PlanError(f"root source is not registered: {root_source}")
    path = safe_profile_path(repo_root, entry["profile"], "root provider profile")
    profile = load_object(path, "root provider profile")
    if profile.get("schema") != 5 or profile.get("kind") != "root-provider":
        raise PlanError("root provider profile must be a schema-5 root-provider")
    if require_string(profile, "id", "root provider profile") != root_source:
        raise PlanError("root provider profile id does not match registry")
    variants = profile.get("variants")
    expected = {"baseline-image"} if root_source == "none" else {"builtin-image", "lkm-module"}
    if not isinstance(variants, dict) or set(variants) != expected:
        raise PlanError(f"root provider {root_source} has an invalid variant contract")
    return profile, path


def load_feature_profile(
    repo_root: Path, feature_id: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Path]:
    entry = _index(registry, "features").get(feature_id)
    if entry is None:
        raise PlanError(f"feature is not registered: {feature_id}")
    path = safe_profile_path(repo_root, entry["profile"], "feature profile")
    profile = load_object(path, "feature profile")
    if profile.get("schema") != 5 or profile.get("kind") != "build-feature":
        raise PlanError("feature profile must be a schema-5 build-feature")
    if require_string(profile, "id", "feature profile") != feature_id:
        raise PlanError("feature profile id does not match registry")
    root_sources = profile.get("root_sources")
    valid_root_sources = {"kernelsu", "sukisu", "resukisu"}
    if (
        not isinstance(root_sources, list)
        or not root_sources
        or len(root_sources) != len(set(root_sources))
        or not set(root_sources).issubset(valid_root_sources)
    ):
        raise PlanError("feature profile.root_sources is invalid")
    return profile, path


def _load_source_lock(path: Path, lock_id: str, expected_kind: str) -> Dict[str, Any]:
    document = load_object(path, "source lock")
    if document.get("kind") != expected_kind or not isinstance(document.get("locks"), dict):
        raise PlanError(f"{path.name} has an invalid lock document")
    lock = document["locks"].get(lock_id)
    if not isinstance(lock, dict) or lock.get("id") != lock_id:
        raise PlanError(f"source lock is not registered: {lock_id}")
    commit = require_string(lock, "commit", f"source lock {lock_id}")
    if not SHA1_RE.fullmatch(commit):
        raise PlanError(f"source lock {lock_id} commit must be a 40-character SHA-1")
    repository = require_string(lock, "repository", f"source lock {lock_id}")
    if not repository.startswith("https://") or any(char.isspace() for char in repository):
        raise PlanError(f"source lock {lock_id} repository must be a fixed HTTPS URL")
    return dict(lock)


def load_root_source_lock(path: Path, lock_id: str, provider: str) -> Dict[str, Any]:
    lock = _load_source_lock(path, lock_id, "static-root-source-locks")
    if lock.get("provider") != provider:
        raise PlanError("root source lock provider does not match request")
    if lock.get("repository") != ROOT_REPOSITORIES[provider]:
        raise PlanError(f"root source {provider} repository is not the audited upstream")
    if lock.get("ref") != "main" or lock.get("source_dir") != "kernel":
        raise PlanError("root source lock must record main provenance and kernel source_dir")
    if lock.get("kernel_license") != "GPL-2.0-only":
        raise PlanError("root kernel source must record GPL-2.0-only")
    return lock


def load_feature_source_lock(path: Path, lock_id: str, feature: str) -> Dict[str, Any]:
    lock = _load_source_lock(path, lock_id, "static-feature-source-locks")
    if lock.get("feature") != feature:
        raise PlanError("feature source lock does not match feature")
    return lock


def _validate_uname_tag(tag: str, expected_base_release: str) -> str:
    if not isinstance(tag, str) or not UNAME_TAG_RE.fullmatch(tag):
        raise PlanError(
            "uname_tag must omit the leading '-' and contain only letters, digits, '.', '_', and '-'"
        )
    if expected_base_release in tag:
        raise PlanError("uname_tag must not repeat the complete Google base release")
    return f"-{tag}" if tag else ""


def _variant(
    variant_id: str,
    contract: Dict[str, Any],
    family_id: str,
    root_source: str,
    request_features: Dict[str, bool],
    prefix: str,
    user_suffix: str,
    expected_base_release: str,
    build_adapter: str,
) -> Dict[str, Any]:
    feature_flags = {
        "susfs": request_features["susfs"] and variant_id == "builtin-image",
        "kpm": request_features["kpm"] and variant_id == "builtin-image",
        "vivo_vermagic": request_features["vivo_vermagic"],
    }
    if root_source == "none":
        token = "base"
    else:
        token = f"{PROVIDER_TOKENS[root_source]}-{'b' if variant_id == 'builtin-image' else 'l'}"
        if feature_flags["susfs"]:
            token += "-s"
        if feature_flags["kpm"]:
            token += "-k"
        if feature_flags["vivo_vermagic"]:
            token += "-v"
    managed_suffix = f"{prefix}-{token}"
    local_suffix = managed_suffix + user_suffix
    google_localversion_budget = GOOGLE_LOCALVERSION_BUDGET[build_adapter]
    if (
        len(expected_base_release)
        + google_localversion_budget
        + len(local_suffix)
        > MAX_UTS_RELEASE_LENGTH
    ):
        raise PlanError(f"uname_tag exceeds the 64-byte UTS_RELEASE limit for {variant_id}")
    if variant_id == "lkm-module":
        release_contract = {
            "mode": "kmi-portable-module",
            "kmi_family": family_id,
            "kernel_series": ".".join(expected_base_release.split(".")[:2]),
        }
    else:
        release_contract = {
            "mode": "base-prefix-and-suffix",
            "prefix": expected_base_release,
            "suffix": local_suffix,
        }
    return {
        "id": variant_id,
        "artifact": contract["artifact"],
        "root_linkage": contract["linkage"],
        "features": feature_flags,
        "configuration": dict(contract["kconfig"]),
        "version": {
            "managed_suffix": managed_suffix,
            "local_suffix": local_suffix,
            "google_localversion_budget": google_localversion_budget,
            "release_contract": release_contract,
        },
    }


def resolve_plan(
    repo_root: Path,
    release_id: str,
    root_source: str,
    susfs: bool = False,
    kpm: bool = False,
    vivo_vermagic: bool = False,
    uname_tag: str = "",
) -> Dict[str, Any]:
    """Compile six public inputs into literal build variants."""

    require_identifier(release_id, "release id")
    require_identifier(root_source, "root source")
    for name, value in (("susfs", susfs), ("kpm", kpm), ("vivo_vermagic", vivo_vermagic)):
        if not isinstance(value, bool):
            raise PlanError(f"{name} must be a boolean")
    registry, registry_path = load_registry(repo_root)
    entry, release, release_path = load_release(repo_root, release_id, registry)
    family, family_path = load_family(repo_root, entry["family_id"])
    if root_source not in family["root_sources"]:
        raise PlanError(f"root source {root_source} is not enabled for {entry['family_id']}")
    root_profile, root_profile_path = load_root_provider(repo_root, root_source, registry)
    requested = {"susfs": susfs, "kpm": kpm, "vivo_vermagic": vivo_vermagic}
    if root_source == "none" and any(requested.values()):
        raise PlanError("SUSFS, KPM, and vivo_vermagic require a root source")

    feature_profiles: Dict[str, Dict[str, Any]] = {}
    feature_paths: Dict[str, Path] = {}
    for public_name, profile_id in (("susfs", "susfs"), ("kpm", "kpm"), ("vivo_vermagic", "vivo-vermagic")):
        profile, path = load_feature_profile(repo_root, profile_id, registry)
        feature_profiles[public_name] = profile
        feature_paths[public_name] = path
        if requested[public_name]:
            if root_source not in profile["root_sources"]:
                if public_name == "kpm":
                    raise PlanError(
                        "KPM requires SukiSU because only it provides the in-kernel KPM bridge"
                    )
                raise PlanError(f"{public_name} is not supported by root source {root_source}")
            if not family["features"][public_name] or family["kernel_series"] not in profile["supported_series"]:
                if public_name == "vivo_vermagic":
                    raise PlanError("vivo_vermagic is supported only on kernel series 5.10, 5.15, or 6.1")
                raise PlanError(f"{public_name} is not supported on kernel series {family['kernel_series']}")

    google_lock_path = repo_root / "locks" / "sources.lock.json"
    lock_id = require_string(release, "source_lock", "release profile")
    try:
        google_lock = load_lock(google_lock_path, lock_id)
    except LockError as error:
        raise PlanError(str(error)) from error
    if google_lock["release_id"] != release_id or google_lock["family_id"] != entry["family_id"]:
        raise PlanError("Google source lock does not match the selected release")
    expected_base = release["version"]["expected_base_release"]
    if google_lock["version"]["expected_base_release"] != expected_base:
        raise PlanError("Google source lock base release does not match release profile")
    user_suffix = _validate_uname_tag(uname_tag, expected_base)

    root_lock_path = repo_root / "locks" / "root-sources.lock.json"
    root_source_lock: Optional[str] = root_profile.get("source_lock")
    resolved_root_source: Optional[Dict[str, Any]] = None
    if root_source != "none":
        if not isinstance(root_source_lock, str):
            raise PlanError("non-empty root source requires a source lock")
        resolved_root_source = load_root_source_lock(root_lock_path, root_source_lock, root_source)
    elif root_source_lock is not None:
        raise PlanError("root source none must not have a source lock")

    feature_lock_path = repo_root / "locks" / "feature-sources.lock.json"
    resolved_features: Dict[str, Dict[str, Any]] = {}
    for name in ("susfs", "kpm", "vivo_vermagic"):
        profile = feature_profiles[name]
        item: Dict[str, Any] = {
            "enabled": requested[name],
            "adapter": profile["adapter"],
            "variant": profile["variant"],
            "source_lock": None,
            "source": None,
        }
        if requested[name] and name == "susfs":
            source_lock = profile["source_locks"].get(entry["family_id"])
            if not isinstance(source_lock, str):
                raise PlanError(f"SUSFS has no locked source for {entry['family_id']}")
            item["source_lock"] = source_lock
            item["source"] = load_feature_source_lock(feature_lock_path, source_lock, "susfs")
        elif requested[name] and name == "kpm":
            source_lock = require_string(profile, "source_lock", "KPM feature profile")
            item["source_lock"] = source_lock
            item["source"] = load_feature_source_lock(feature_lock_path, source_lock, "kpm")
        resolved_features[name] = item

    variants = [
        _variant(
            variant_id,
            contract,
            entry["family_id"],
            root_source,
            requested,
            release["version"]["local_suffix_prefix"],
            user_suffix,
            expected_base,
            family["build"]["adapter"],
        )
        for variant_id, contract in root_profile["variants"].items()
    ]
    for variant in variants:
        if variant["id"] == "builtin-image":
            if susfs:
                variant["configuration"].update(feature_profiles["susfs"].get("kconfig", {}))
                if root_source == "resukisu":
                    variant["configuration"]["KSU_TRACEPOINT_HOOK"] = "n"
            if kpm:
                variant["configuration"].update(
                    feature_profiles["kpm"].get("provider_kconfig", {}).get(root_source, {})
                )

    definition = {
        "registry_sha256": sha256_file(registry_path),
        "family_sha256": sha256_file(family_path),
        "release_sha256": sha256_file(release_path),
        "root_provider_sha256": sha256_file(root_profile_path),
        "sources_lock_sha256": sha256_file(google_lock_path),
        "feature_profiles_sha256": {
            name: sha256_file(path) for name, path in feature_paths.items()
        },
    }
    if root_source != "none":
        definition["root_sources_lock_sha256"] = sha256_file(root_lock_path)
    if susfs or kpm:
        definition["feature_sources_lock_sha256"] = sha256_file(feature_lock_path)

    return {
        "schema": 5,
        "selection": {
            "family_id": entry["family_id"],
            "release_id": release_id,
            "kernel_series": family["kernel_series"],
            "root_source": root_source,
            "susfs": susfs,
            "kpm": kpm,
            "vivo_vermagic": vivo_vermagic,
            "uname_tag": uname_tag,
            "state": release["state"],
        },
        "definition": definition,
        "source": {
            "lock_id": lock_id,
            "manifest_commit": google_lock["manifest"]["commit"],
            "superproject_commit": google_lock["superproject"]["commit"],
            "manifest_ref": google_lock["superproject"]["manifest_ref"],
            "common_commit": google_lock["common"]["commit"],
        },
        "root": {
            "id": root_source,
            "adapter": root_profile["adapter"],
            "source_lock": root_source_lock,
            "source": resolved_root_source,
        },
        "features": resolved_features,
        "variants": variants,
        "build": dict(family["build"]),
        "version": {
            "expected_base_release": expected_base,
            "uname_tag": uname_tag,
            "user_suffix": user_suffix,
        },
    }


def parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True, help="immutable Google release id")
    parser.add_argument("--root-source", required=True, help="none, kernelsu, sukisu, or resukisu")
    parser.add_argument("--susfs", type=parse_bool, default=False)
    parser.add_argument("--kpm", type=parse_bool, default=False)
    parser.add_argument("--vivo-vermagic", type=parse_bool, default=False)
    parser.add_argument("--uname-tag", default="", help="optional tag without a leading dash")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        plan = resolve_plan(
            args.repo_root.resolve(),
            args.release_id,
            args.root_source,
            args.susfs,
            args.kpm,
            args.vivo_vermagic,
            args.uname_tag,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(plan) + b"\n")
    except (LockError, PlanError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
