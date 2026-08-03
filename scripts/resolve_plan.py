#!/usr/bin/env python3
"""Resolve one ReNebula build request into canonical JSON.

The dispatcher selects an immutable Google ``release_id``, an audited root
provider, linkage, hook mode, and configuration profile.  Those selectors are
closed allowlists.  The only text input is an optional, strictly validated
``uname_suffix`` which can only be appended to ReNebula's managed suffix; it
can never replace or repeat the Google base release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sync_google_gki import LockError, load_json, load_lock, validate_relative_path


SUFFIX_RE = re.compile(r"^-[A-Za-z0-9._-]+$")
UNAME_SUFFIX_RE = re.compile(r"^(?:|-[A-Za-z0-9][A-Za-z0-9._-]*)$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
KCONFIG_LINE_RE = re.compile(r"^(?:CONFIG_[A-Z0-9_]+=(?:y|m)|# CONFIG_[A-Z0-9_]+ is not set)$")
EXCLUDED_ROOT_RE = re.compile(r"(?:kernel.?su.?next|\bksun\b|\bksu.?next\b)", re.IGNORECASE)
KLEAF_FRAGMENT_ADAPTER = "kleaf-defconfig-fragment-arm64-v1"
KLEAF_ADAPTERS = {KLEAF_FRAGMENT_ADAPTER}
LEGACY_ADAPTER = "legacy-build-sh-arm64-v1"
RELEASE_STATES = {"source-locked", "image-verified", "verified", "deprecated"}
RESUKISU_REPOSITORY = "https://github.com/ReSukiSU/ReSukiSU.git"
RESUKISU_SOURCE_DIR = "kernel"
RESUKISU_REPOSITORY_LICENSE = "GPL-3.0"
RESUKISU_KERNEL_LICENSE = "GPL-2.0-only"
MAX_UTS_RELEASE_LENGTH = 64


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
    if EXCLUDED_ROOT_RE.search(value):
        raise PlanError(f"{context} names an excluded root provider")


def require_selector(value: str, context: str) -> None:
    if value.startswith("__select_"):
        raise PlanError(f"{context} must be explicitly selected")
    require_identifier(value, context)


def validate_uname_suffix(
    uname_suffix: str, expected_base_release: str, managed_suffix: str
) -> None:
    """Validate the optional, append-only user part of UTS_RELEASE.

    Keeping this independent of shell handling is deliberate: the resolved
    plan is the only value later supplied to the kernel's version setting.
    """

    if not isinstance(uname_suffix, str):
        raise PlanError("uname suffix must be a string")
    if not UNAME_SUFFIX_RE.fullmatch(uname_suffix):
        raise PlanError(
            "uname suffix must be empty or start with '-' and contain only "
            "letters, digits, '.', '_', and '-'"
        )
    if expected_base_release in uname_suffix:
        raise PlanError("uname suffix must not contain the complete Google base release")
    final_release = expected_base_release + managed_suffix + uname_suffix
    if len(final_release) > MAX_UTS_RELEASE_LENGTH:
        available = MAX_UTS_RELEASE_LENGTH - len(expected_base_release + managed_suffix)
        raise PlanError(
            "uname suffix exceeds the UTS_RELEASE limit for this selection "
            f"(at most {max(available, 0)} characters including its leading '-')"
        )


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
    registry = load_object(path, "selector registry")
    if registry.get("schema") != 4 or registry.get("kind") != "static-selector-registry":
        raise PlanError("selector registry must be a schema-4 static-selector-registry")

    releases = registry.get("releases")
    if not isinstance(releases, list) or not releases:
        raise PlanError("selector registry must contain a non-empty releases array")
    if len(releases) > 25:
        raise PlanError("selector registry exceeds the static workflow input limit")
    seen_releases = set()
    for index, entry in enumerate(releases):
        context = f"selector registry.releases[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{context} must be an object")
        release_id = require_string(entry, "id", context)
        family_id = require_string(entry, "family_id", context)
        require_identifier(release_id, f"{context}.id")
        require_identifier(family_id, f"{context}.family_id")
        if release_id in seen_releases:
            raise PlanError(f"selector registry repeats release id: {release_id}")
        seen_releases.add(release_id)
        state = require_string(entry, "state", context)
        if state not in RELEASE_STATES:
            raise PlanError(f"{context}.state is unsupported: {state}")
        profile = require_string(entry, "profile", context)
        expected_profile = f"profiles/releases/{release_id}.json"
        if profile != expected_profile:
            raise PlanError(f"{context}.profile must be {expected_profile}")
        safe_profile_path(repo_root, profile, f"{context}.profile")

    validate_profile_index(
        repo_root,
        registry,
        key="root_providers",
        directory="root-providers",
        label="root provider",
    )
    validate_selector_value_list(registry, "root_linkages", "root linkage")
    validate_selector_value_list(registry, "hook_modes", "hook mode")
    validate_profile_index(
        repo_root,
        registry,
        key="config_profiles",
        directory="config-profiles",
        label="config profile",
    )
    return registry, path


def validate_selector_value_list(registry: Dict[str, Any], key: str, label: str) -> None:
    values = registry.get(key)
    if not isinstance(values, list) or not values:
        raise PlanError(f"selector registry must contain a non-empty {key} array")
    if len(values) != len(set(values)):
        raise PlanError(f"selector registry repeats a {label}")
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise PlanError(f"selector registry.{key}[{index}] must be a string")
        require_identifier(value, f"selector registry.{key}[{index}]")


def validate_profile_index(
    repo_root: Path,
    registry: Dict[str, Any],
    *,
    key: str,
    directory: str,
    label: str,
) -> None:
    entries = registry.get(key)
    if not isinstance(entries, list) or not entries:
        raise PlanError(f"selector registry must contain a non-empty {key} array")
    seen = set()
    for index, entry in enumerate(entries):
        context = f"selector registry.{key}[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{context} must be an object")
        identifier = require_string(entry, "id", context)
        require_identifier(identifier, f"{context}.id")
        if identifier in seen:
            raise PlanError(f"selector registry repeats {label} id: {identifier}")
        seen.add(identifier)
        profile = require_string(entry, "profile", context)
        expected_profile = f"profiles/{directory}/{identifier}.json"
        if profile != expected_profile:
            raise PlanError(f"{context}.profile must be {expected_profile}")
        safe_profile_path(repo_root, profile, f"{context}.profile")


def registry_entries(registry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {entry["id"]: entry for entry in registry["releases"]}


def profile_entries(registry: Dict[str, Any], key: str) -> Dict[str, Dict[str, Any]]:
    return {entry["id"]: entry for entry in registry[key]}


def load_release(
    repo_root: Path, release_id: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Path]:
    entry = registry_entries(registry).get(release_id)
    if entry is None:
        raise PlanError(f"release is not registered: {release_id}")
    path = safe_profile_path(repo_root, entry["profile"], "registry release profile")
    release = load_object(path, "release profile")
    if release.get("schema") != 4 or release.get("kind") != "gki-release":
        raise PlanError("release profile must be a schema-4 gki-release")
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
    suffix_prefix = require_string(version, "local_suffix_prefix", "release profile.version")
    if not SUFFIX_RE.fullmatch(suffix_prefix):
        raise PlanError(
            "release profile.version.local_suffix_prefix must use only letters, digits, '.', '_', and '-'"
        )
    if base_release in suffix_prefix:
        raise PlanError("release profile.version.local_suffix_prefix must not repeat the base release")
    return entry, release, path


def load_family(repo_root: Path, family_id: str) -> Tuple[Dict[str, Any], Path]:
    require_identifier(family_id, "family id")
    path = repo_root / "profiles" / "families" / f"{family_id}.json"
    family = load_object(path, "family profile")
    if family.get("schema") != 4 or family.get("kind") != "kmi-family":
        raise PlanError("family profile must be a schema-4 kmi-family")
    if require_string(family, "id", "family profile") != family_id:
        raise PlanError("family profile id does not match its filename")
    selector = family.get("selector")
    if not isinstance(selector, dict):
        raise PlanError("family profile.selector must be an object")
    allowed_combinations = selector.get("allowed_combinations")
    if not isinstance(allowed_combinations, list) or not allowed_combinations:
        raise PlanError("family profile.selector.allowed_combinations must be a non-empty array")
    seen_combinations = set()
    required_keys = {"root_provider", "root_linkage", "hook_mode", "config_profile"}
    for index, combination in enumerate(allowed_combinations):
        context = f"family profile.selector.allowed_combinations[{index}]"
        if not isinstance(combination, dict) or set(combination) != required_keys:
            raise PlanError(f"{context} must contain exactly {sorted(required_keys)}")
        root_provider = require_string(combination, "root_provider", context)
        root_linkage = require_string(combination, "root_linkage", context)
        hook_mode = require_string(combination, "hook_mode", context)
        config_profile = require_string(combination, "config_profile", context)
        require_identifier(root_provider, f"{context}.root_provider")
        require_identifier(root_linkage, f"{context}.root_linkage")
        require_identifier(hook_mode, f"{context}.hook_mode")
        require_identifier(config_profile, f"{context}.config_profile")
        key = (root_provider, root_linkage, hook_mode, config_profile)
        if key in seen_combinations:
            raise PlanError(f"family profile repeats allowed selector combination: {key}")
        seen_combinations.add(key)
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


def load_root_provider(
    repo_root: Path, root_provider_id: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Path]:
    entry = profile_entries(registry, "root_providers").get(root_provider_id)
    if entry is None:
        raise PlanError(f"root provider is not registered: {root_provider_id}")
    path = safe_profile_path(repo_root, entry["profile"], "registry root provider profile")
    profile = load_object(path, "root provider profile")
    if profile.get("schema") != 4 or profile.get("kind") != "root-provider":
        raise PlanError("root provider profile must be a schema-4 root-provider")
    if require_string(profile, "id", "root provider profile") != root_provider_id:
        raise PlanError("root provider profile id does not match registry")
    adapter = require_string(profile, "adapter", "root provider profile")
    require_identifier(adapter, "root provider profile.adapter")
    source_lock = profile.get("source_lock")
    if root_provider_id == "none":
        if adapter != "none" or source_lock is not None:
            raise PlanError("root provider none must use adapter=none and source_lock=null")
    else:
        if not isinstance(source_lock, str) or not source_lock:
            raise PlanError("a non-none root provider requires a source_lock")
        require_identifier(source_lock, "root provider profile.source_lock")
    return profile, path


def load_config_profile(
    repo_root: Path, config_profile_id: str, registry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Path]:
    entry = profile_entries(registry, "config_profiles").get(config_profile_id)
    if entry is None:
        raise PlanError(f"config profile is not registered: {config_profile_id}")
    path = safe_profile_path(repo_root, entry["profile"], "registry config profile")
    profile = load_object(path, "config profile")
    if profile.get("schema") != 4 or profile.get("kind") != "config-profile":
        raise PlanError("config profile must be a schema-4 config-profile")
    if require_string(profile, "id", "config profile") != config_profile_id:
        raise PlanError("config profile id does not match registry")
    ksu_debug = profile.get("ksu_debug")
    if not isinstance(ksu_debug, bool):
        raise PlanError("config profile.ksu_debug must be a boolean")
    suffix_token = require_string(profile, "suffix_token", "config profile")
    require_identifier(suffix_token, "config profile.suffix_token")
    return profile, path


def load_root_source_lock(path: Path, lock_id: str) -> Dict[str, Any]:
    document = load_object(path, "root source lock")
    if document.get("schema") != 1 or document.get("kind") != "static-root-source-locks":
        raise PlanError("root source lock must be a schema-1 static-root-source-locks")
    locks = document.get("locks")
    if not isinstance(locks, dict):
        raise PlanError("root source lock must contain a locks object")
    lock = locks.get(lock_id)
    if not isinstance(lock, dict):
        raise PlanError(f"root source lock is not registered: {lock_id}")
    if require_string(lock, "id", f"root source lock {lock_id}") != lock_id:
        raise PlanError("root source lock id does not match its enclosing key")
    provider = require_string(lock, "provider", f"root source lock {lock_id}")
    require_identifier(provider, f"root source lock {lock_id}.provider")
    repository = require_string(lock, "repository", f"root source lock {lock_id}")
    if not repository.startswith("https://") or any(character.isspace() for character in repository):
        raise PlanError("root source lock repository must be a fixed HTTPS URL")
    commit = require_string(lock, "commit", f"root source lock {lock_id}")
    if not SHA1_RE.fullmatch(commit):
        raise PlanError("root source lock commit must be a lowercase 40-character SHA-1")
    ref = require_string(lock, "ref", f"root source lock {lock_id}")
    if ref != "main":
        raise PlanError("ReSukiSU root source lock ref must be the provenance ref main")
    if provider == "resukisu":
        if repository != RESUKISU_REPOSITORY:
            raise PlanError(
                f"ReSukiSU root source lock repository must be {RESUKISU_REPOSITORY}"
            )
        if require_string(lock, "source_dir", f"root source lock {lock_id}") != RESUKISU_SOURCE_DIR:
            raise PlanError("ReSukiSU root source lock source_dir must be kernel")
        if (
            require_string(lock, "repository_license", f"root source lock {lock_id}")
            != RESUKISU_REPOSITORY_LICENSE
        ):
            raise PlanError("ReSukiSU repository license must be GPL-3.0")
        if (
            require_string(lock, "kernel_license", f"root source lock {lock_id}")
            != RESUKISU_KERNEL_LICENSE
        ):
            raise PlanError("ReSukiSU kernel directory license must be GPL-2.0-only")
    return {
        "provider": provider,
        "repository": repository,
        "commit": commit,
        "ref": ref,
    }


def allowed_selector_combinations(family: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """Return the complete, explicitly enabled root selector tuples."""

    selector = family["selector"]
    return [
        (
            combination["root_provider"],
            combination["root_linkage"],
            combination["hook_mode"],
            combination["config_profile"],
        )
        for combination in selector["allowed_combinations"]
    ]


def registered_selector_tuples(repo_root: Path) -> List[Tuple[str, str, str, str, str]]:
    registry, _ = load_registry(repo_root)
    tuples: List[Tuple[str, str, str, str, str]] = []
    for entry in registry["releases"]:
        family, _ = load_family(repo_root, entry["family_id"])
        for root_provider, root_linkage, hook_mode, config_profile in allowed_selector_combinations(
            family
        ):
            tuples.append(
                (entry["id"], root_provider, root_linkage, hook_mode, config_profile)
            )
    return tuples


def registry_selector_values(registry: Dict[str, Any], key: str) -> set[str]:
    values = registry[key]
    return set(values)


def resolve_configuration(
    root_provider_id: str,
    root_linkage: str,
    hook_mode: str,
    config_profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Map an allowlisted UI tuple to the only Kconfig fragment it may produce.

    This makes linkage, hook, and debug independently visible in Actions while
    deliberately keeping raw Kconfig outside the user-controlled surface.
    """

    config_profile_id = config_profile["id"]
    if root_provider_id == "none":
        if (
            root_linkage != "none"
            or hook_mode != "none"
            or config_profile_id != "release"
            or config_profile["ksu_debug"]
        ):
            raise PlanError("root=none only permits none/none/release")
        return {
            "id": config_profile_id,
            "resolved_id": "none",
            "kconfig_lines": [],
            "suffix_token": "none",
        }
    if root_provider_id != "resukisu":
        raise PlanError(f"unsupported root provider: {root_provider_id}")
    if root_linkage not in {"lkm", "builtin"}:
        raise PlanError("ReSukiSU linkage must be lkm or builtin")
    if hook_mode != "tracepoint":
        raise PlanError("ReSukiSU only exposes the audited tracepoint hook")
    if config_profile_id not in {"release", "debug"}:
        raise PlanError("ReSukiSU configuration profile is unsupported")
    ksu_value = "m" if root_linkage == "lkm" else "y"
    kconfig_lines = [
        f"CONFIG_KSU={ksu_value}",
        "CONFIG_KSU_TRACEPOINT_HOOK=y",
        "CONFIG_KSU_MULTI_MANAGER_SUPPORT=y",
        "# CONFIG_KSU_MANUAL_HOOK is not set",
        "# CONFIG_KSU_SUSFS is not set",
        "CONFIG_KSU_DEBUG=y"
        if config_profile["ksu_debug"]
        else "# CONFIG_KSU_DEBUG is not set",
    ]
    return {
        "id": config_profile_id,
        "resolved_id": f"resukisu-{root_linkage}-{hook_mode}-{config_profile_id}",
        "kconfig_lines": kconfig_lines,
        "suffix_token": (
            f"rsu-{'bi' if root_linkage == 'builtin' else 'lkm'}-tp-"
            f"{'dbg' if config_profile['ksu_debug'] else 'rel'}"
        ),
    }


def resolve_plan(
    repo_root: Path,
    release_id: str,
    root_provider_id: str,
    root_linkage: str,
    hook_mode: str,
    config_profile_id: str,
    uname_suffix: str = "",
) -> Dict[str, Any]:
    require_selector(release_id, "release id")
    require_selector(root_provider_id, "root provider")
    require_selector(root_linkage, "root linkage")
    require_selector(hook_mode, "hook mode")
    require_selector(config_profile_id, "config profile")
    registry, registry_path = load_registry(repo_root)
    if root_linkage not in registry_selector_values(registry, "root_linkages"):
        raise PlanError(f"root linkage is not registered: {root_linkage}")
    if hook_mode not in registry_selector_values(registry, "hook_modes"):
        raise PlanError(f"hook mode is not registered: {hook_mode}")
    entry, release, release_path = load_release(repo_root, release_id, registry)
    family, family_path = load_family(repo_root, entry["family_id"])
    selection_key = (root_provider_id, root_linkage, hook_mode, config_profile_id)
    if selection_key not in allowed_selector_combinations(family):
        raise PlanError(
            "selector tuple is not enabled for the selected KMI family: "
            f"{entry['family_id']}/{'/'.join(selection_key)}"
        )
    root_provider, root_provider_path = load_root_provider(repo_root, root_provider_id, registry)
    config_profile, config_profile_path = load_config_profile(repo_root, config_profile_id, registry)
    configuration = resolve_configuration(
        root_provider_id, root_linkage, hook_mode, config_profile
    )

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
    managed_suffix = (
        release["version"]["local_suffix_prefix"] + "-" + configuration["suffix_token"]
    )
    if not SUFFIX_RE.fullmatch(managed_suffix) or expected_base_release in managed_suffix:
        raise PlanError("resolved local suffix is invalid")
    validate_uname_suffix(uname_suffix, expected_base_release, managed_suffix)
    suffix = managed_suffix + uname_suffix

    root_lock_path = repo_root / "locks" / "root-sources.lock.json"
    root_source: Optional[Dict[str, str]] = None
    root_source_lock = root_provider["source_lock"]
    if root_source_lock is not None:
        root_source = load_root_source_lock(root_lock_path, root_source_lock)
        if root_source["provider"] != root_provider_id:
            raise PlanError("root source lock provider does not match selected root provider")
    elif root_provider_id != "none":
        raise PlanError("a non-none root provider requires a root source lock")
    root_lock_digest = sha256_file(root_lock_path)

    build = dict(family["build"])
    if build["adapter"] == LEGACY_ADAPTER:
        release_contract: Dict[str, str] = {
            "mode": "exact",
            "expected_uname_release": expected_base_release + suffix,
        }
    elif build["adapter"] == KLEAF_FRAGMENT_ADAPTER:
        release_contract = {
            "mode": "base-prefix-and-suffix",
            "prefix": expected_base_release,
            "suffix": suffix,
        }
    else:
        raise PlanError(f"unsupported family build adapter: {build['adapter']}")

    return {
        "schema": 4,
        "selection": {
            "family_id": entry["family_id"],
            "release_id": release_id,
            "root_provider": root_provider_id,
            "root_linkage": root_linkage,
            "hook_mode": hook_mode,
            "config_profile": config_profile_id,
            "uname_suffix": uname_suffix,
            "state": release["state"],
        },
        "definition": {
            "registry_sha256": sha256_file(registry_path),
            "family_sha256": sha256_file(family_path),
            "release_sha256": sha256_file(release_path),
            "root_provider_sha256": sha256_file(root_provider_path),
            "config_profile_sha256": sha256_file(config_profile_path),
            "sources_lock_sha256": sha256_file(lock_path),
            "root_sources_lock_sha256": root_lock_digest,
        },
        "source": {
            "lock_id": lock_id,
            "manifest_commit": lock["manifest"]["commit"],
            "superproject_commit": lock["superproject"]["commit"],
            "manifest_ref": lock["superproject"]["manifest_ref"],
            "common_commit": lock["common"]["commit"],
        },
        "root": {
            "id": root_provider_id,
            "adapter": root_provider["adapter"],
            "linkage": root_linkage,
            "hook_mode": hook_mode,
            "source_lock": root_source_lock,
            "source": root_source,
        },
        "configuration": configuration,
        "build": build,
        "version": {
            "expected_base_release": expected_base_release,
            "managed_suffix": managed_suffix,
            "uname_suffix": uname_suffix,
            "local_suffix": suffix,
            "release_contract": release_contract,
        },
    }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True, help="static immutable Google release id")
    parser.add_argument("--root-provider", required=True, help="static root provider id")
    parser.add_argument("--root-linkage", required=True, help="static root linkage id")
    parser.add_argument("--hook-mode", required=True, help="static root hook mode id")
    parser.add_argument("--config-profile", required=True, help="static config profile id")
    parser.add_argument(
        "--uname-suffix",
        default="",
        help="optional append-only uname suffix (empty or starts with '-')",
    )
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
        plan = resolve_plan(
            args.repo_root.resolve(),
            args.release_id,
            args.root_provider,
            args.root_linkage,
            args.hook_mode,
            args.config_profile,
            args.uname_suffix,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(plan) + b"\n")
    except (LockError, PlanError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
