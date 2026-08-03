#!/usr/bin/env python3
"""Static gate for ReNebula's immutable multi-KMI selector definitions.

This runs before any GKI or root-provider source is downloaded.  It validates
the release/root/config graph, the generated Actions selector, and boundaries
that prevent floating sources, a second kernel-version writer, retired Vivo
inputs, or KernelSU-Next support from returning.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import resolve_plan
from sync_google_gki import LockError, canonical_json, load_json, load_lock


PINNED_ACTION_RE = re.compile(r"^[^@\s]+@[0-9a-fA-F]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
FORBIDDEN_WORKFLOW_RE = re.compile(
    r"(?:"
    r"curl\s*\|"
    r"|wget\s*\|"
    r"|git\s+(?:clone|pull)\b"
    r"|repo\s+(?:init|sync)\b"
    r"|continue-on-error\s*:\s*true"
    r"|\|\|\s*true"
    r")",
    re.IGNORECASE,
)
RETIRED_SOURCE_RE = re.compile(r"(?:vivo|is_vki|vki|vivo_tarball_url)", re.IGNORECASE)
EXCLUDED_ROOT_RE = re.compile(r"(?:kernel.?su.?next|\bksun\b|\bksu.?next\b)", re.IGNORECASE)
VERSION_WRITE_RE = re.compile(r"(?:scripts/setlocalversion|CONFIG_LOCALVERSION)")
SAFE_SUFFIX_RE = re.compile(r"^-[A-Za-z0-9._-]+$")
OPTION_LINE_RE = re.compile(r"^\s*-\s+([a-z0-9][a-z0-9._-]*)\s*$")
REQUIRED_FAMILY_ADAPTERS = {
    "android12-5.10": "legacy-build-sh-arm64-v1",
    "android13-5.10": "legacy-build-sh-arm64-v1",
    "android13-5.15": "legacy-build-sh-arm64-v1",
    "android14-5.15": "kleaf-defconfig-fragment-arm64-v1",
    "android14-6.1": "kleaf-defconfig-fragment-arm64-v1",
    "android15-6.6": "kleaf-defconfig-fragment-arm64-v1",
    "android16-6.12": "kleaf-defconfig-fragment-arm64-v1",
    "android17-6.18": "kleaf-defconfig-fragment-arm64-v1",
}
RESUKISU_COMBINATIONS = (
    ("resukisu", "lkm", "tracepoint", "release"),
    ("resukisu", "lkm", "tracepoint", "debug"),
    ("resukisu", "builtin", "tracepoint", "release"),
    ("resukisu", "builtin", "tracepoint", "debug"),
)
REQUIRED_FAMILY_SELECTOR_COMBINATIONS = {
    family_id: (("none", "none", "none", "release"),) + RESUKISU_COMBINATIONS
    for family_id in REQUIRED_FAMILY_ADAPTERS
    if family_id != "android17-6.18"
}
REQUIRED_FAMILY_SELECTOR_COMBINATIONS["android17-6.18"] = (
    ("none", "none", "none", "release"),
)


class RepositoryError(ValueError):
    """Raised for a repository-contract violation."""


def collect_files(directory: Path, suffixes: Tuple[str, ...]) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix in suffixes)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise RepositoryError(f"cannot read {path}: {error}") from error


def expected_profile_files(root: Path, entries: List[Dict[str, Any]], key: str) -> set[str]:
    return {entry[key] for entry in entries}


def ensure_profile_files_match_registry(
    root: Path, directory: str, entries: List[Dict[str, Any]]
) -> None:
    actual = {
        path.relative_to(root).as_posix()
        for path in collect_files(root / "profiles" / directory, (".json",))
    }
    expected = expected_profile_files(root, entries, "profile")
    if actual != expected:
        raise RepositoryError(
            f"profiles/{directory} must exactly match the selector registry; "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


def ensure_registry_graph(root: Path) -> Dict[str, Any]:
    registry, registry_path = resolve_plan.load_registry(root)
    entries = registry["releases"]
    root_entries = registry["root_providers"]
    root_linkages = registry["root_linkages"]
    hook_modes = registry["hook_modes"]
    config_entries = registry["config_profiles"]
    entry_ids = {entry["id"] for entry in entries}
    entry_families = {entry["family_id"] for entry in entries}
    if set(REQUIRED_FAMILY_ADAPTERS) != entry_families:
        missing = sorted(set(REQUIRED_FAMILY_ADAPTERS) - entry_families)
        unexpected = sorted(entry_families - set(REQUIRED_FAMILY_ADAPTERS))
        raise RepositoryError(
            "registry must cover every supported complete KMI chain; "
            f"missing={missing}, unexpected={unexpected}"
        )
    if len(entries) != len(entry_families):
        raise RepositoryError("registry must contain exactly one current release per KMI family")
    if [entry["id"] for entry in root_entries] != ["none", "resukisu"]:
        raise RepositoryError("selector registry must expose only none and resukisu root providers")
    if root_linkages != ["none", "lkm", "builtin"]:
        raise RepositoryError("selector registry exposes an unexpected root linkage")
    if hook_modes != ["none", "tracepoint"]:
        raise RepositoryError("selector registry exposes an unexpected hook mode")
    if [entry["id"] for entry in config_entries] != ["release", "debug"]:
        raise RepositoryError("selector registry exposes an unexpected config profile")

    ensure_profile_files_match_registry(root, "releases", entries)
    ensure_profile_files_match_registry(root, "root-providers", root_entries)
    ensure_profile_files_match_registry(root, "config-profiles", config_entries)
    family_files = {
        path.stem for path in collect_files(root / "profiles" / "families", (".json",))
    }
    if family_files != entry_families:
        raise RepositoryError(
            "family profile files must exactly match the registry; "
            f"missing={sorted(entry_families - family_files)}, "
            f"unexpected={sorted(family_files - entry_families)}"
        )
    if collect_files(root / "profiles" / "targets", (".json",)):
        raise RepositoryError("legacy profiles/targets must not coexist with release profiles")

    lock_document = load_json(root / "locks" / "sources.lock.json")
    if not isinstance(lock_document, dict) or lock_document.get("schema") != 2:
        raise RepositoryError("sources lock must use schema 2")
    locks = lock_document.get("locks")
    if not isinstance(locks, dict):
        raise RepositoryError("sources lock must contain locks")

    expected_lock_ids = set()
    plans: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    seen_root_providers = set()
    seen_root_linkages = set()
    seen_hook_modes = set()
    seen_config_profiles = set()
    for entry in entries:
        release_id = entry["id"]
        _, release, _ = resolve_plan.load_release(root, release_id, registry)
        family, _ = resolve_plan.load_family(root, entry["family_id"])
        adapter = family["build"]["adapter"]
        if adapter != REQUIRED_FAMILY_ADAPTERS[entry["family_id"]]:
            raise RepositoryError(f"{entry['family_id']} has the wrong build adapter")
        allowed_combinations = tuple(resolve_plan.allowed_selector_combinations(family))
        if allowed_combinations != REQUIRED_FAMILY_SELECTOR_COMBINATIONS[entry["family_id"]]:
            raise RepositoryError(
                f"{entry['family_id']} selector combinations diverge from the audited support matrix"
            )
        lock_id = release["source_lock"]
        expected_lock_ids.add(lock_id)
        lock = load_lock(root / "locks" / "sources.lock.json", lock_id)
        if lock["release_id"] != release_id or lock["family_id"] != entry["family_id"]:
            raise RepositoryError(f"{release_id} does not agree with its source lock")
        if lock["version"]["expected_base_release"] != release["version"]["expected_base_release"]:
            raise RepositoryError(f"{release_id} has a source-lock version mismatch")
        for root_provider, root_linkage, hook_mode, config_profile in allowed_combinations:
            plan = resolve_plan.resolve_plan(
                root, release_id, root_provider, root_linkage, hook_mode, config_profile
            )
            key = (release_id, root_provider, root_linkage, hook_mode, config_profile)
            if key in plans:
                raise RepositoryError(f"selector tuple is repeated: {key}")
            plans[key] = plan
            seen_root_providers.add(root_provider)
            seen_root_linkages.add(root_linkage)
            seen_hook_modes.add(hook_mode)
            seen_config_profiles.add(config_profile)
    if set(locks) != expected_lock_ids:
        raise RepositoryError(
            "sources lock entries must exactly match registered releases; "
            f"missing={sorted(expected_lock_ids - set(locks))}, "
            f"unexpected={sorted(set(locks) - expected_lock_ids)}"
        )
    if len(expected_lock_ids) != len(entry_ids):
        raise RepositoryError("each registered release must have its own immutable source lock")
    if seen_root_providers != {entry["id"] for entry in root_entries}:
        raise RepositoryError("every UI root provider must appear in an enabled selector tuple")
    if seen_root_linkages != set(root_linkages):
        raise RepositoryError("every UI root linkage must appear in an enabled selector tuple")
    if seen_hook_modes != set(hook_modes):
        raise RepositoryError("every UI hook mode must appear in an enabled selector tuple")
    if seen_config_profiles != {entry["id"] for entry in config_entries}:
        raise RepositoryError("every UI config profile must appear in an enabled selector tuple")
    return {
        "registry": registry,
        "registry_path": registry_path,
        "plans": plans,
        "release_ids": [entry["id"] for entry in entries],
        "root_provider_ids": [entry["id"] for entry in root_entries],
        "root_linkages": root_linkages,
        "hook_modes": hook_modes,
        "config_profile_ids": [entry["id"] for entry in config_entries],
        "selector_tuples": list(plans),
    }


def ensure_workflow_supply_chain(root: Path) -> None:
    workflow_files = collect_files(root / ".github", (".yml", ".yaml"))
    if not workflow_files:
        raise RepositoryError("no GitHub workflow definitions found")
    for path in workflow_files:
        content = read_text(path)
        if FORBIDDEN_WORKFLOW_RE.search(content):
            raise RepositoryError(f"forbidden dynamic or failure-masking operation in {path}")
        for reference in USES_RE.findall(content):
            if reference.startswith("./"):
                continue
            if not PINNED_ACTION_RE.fullmatch(reference):
                raise RepositoryError(f"unpinned action reference in {path}: {reference}")


def workflow_selector_options(path: Path, label: str) -> List[str]:
    content = read_text(path)
    escaped_label = re.escape(label)
    block_re = re.compile(
        rf"^\s*# BEGIN GENERATED {escaped_label} OPTIONS\s*$"
        rf"(?P<options>.*?)"
        rf"^\s*# END GENERATED {escaped_label} OPTIONS\s*$",
        re.MULTILINE | re.DOTALL,
    )
    match = block_re.search(content)
    if not match:
        raise RepositoryError(f"dispatch workflow lacks the generated {label.lower()} options block")
    options: List[str] = []
    for line in match.group("options").splitlines():
        if not line.strip():
            continue
        option_match = OPTION_LINE_RE.fullmatch(line)
        if not option_match:
            raise RepositoryError(f"invalid generated {label.lower()} option line: {line!r}")
        options.append(option_match.group(1))
    if not options:
        raise RepositoryError(f"dispatch workflow does not expose any {label.lower()} option")
    return options


def ensure_workflow_matches_registry(
    root: Path,
    release_ids: List[str],
    root_provider_ids: List[str],
    root_linkages: List[str],
    hook_modes: List[str],
    config_profile_ids: List[str],
) -> None:
    dispatch_path = root / ".github" / "workflows" / "dispatch.yml"
    expected_options = {
        "RELEASE": release_ids,
        "ROOT PROVIDER": root_provider_ids,
        "ROOT LINKAGE": root_linkages,
        "HOOK MODE": hook_modes,
        "CONFIG PROFILE": config_profile_ids,
    }
    for label, expected in expected_options.items():
        options = workflow_selector_options(dispatch_path, label)
        if options != expected:
            raise RepositoryError(
                f"dispatch {label.lower()} choices diverge from profiles/registry.json; "
                f"workflow={options}, registry={expected}"
            )
    dispatch = read_text(dispatch_path)
    required_inputs = {
        "release_id": "__select_release_id__",
        "root_provider": "__select_root_provider__",
        "root_linkage": "__select_root_linkage__",
        "hook_mode": "__select_hook_mode__",
        "config_profile": "__select_config_profile__",
    }
    for input_id, sentinel in required_inputs.items():
        if not re.search(rf"(?m)^\s+{re.escape(input_id)}:\s*$", dispatch):
            raise RepositoryError(f"dispatch workflow must accept {input_id}")
        if f"default: {sentinel}" not in dispatch:
            raise RepositoryError(f"dispatch {input_id} must require an explicit selection sentinel")
    if not re.search(r"(?m)^\s+uname_suffix:\s*$", dispatch):
        raise RepositoryError("dispatch workflow must accept optional uname_suffix")
    if "default: \"\"" not in dispatch:
        raise RepositoryError("dispatch uname_suffix must default to the empty append-only suffix")
    if "target:" in dispatch or "features:" in dispatch:
        raise RepositoryError("dispatch workflow must not accept legacy target or feature inputs")
    if not re.search(r"(?m)^\s*needs:\s*\[resolve, verify\]\s*$", dispatch):
        raise RepositoryError("build must wait for both resolve and static verification")
    resolve_workflow = read_text(root / ".github" / "workflows" / "resolve-plan.yml")
    for argument in (
        "--release-id",
        "--root-provider",
        "--root-linkage",
        "--hook-mode",
        "--config-profile",
        "--uname-suffix",
    ):
        if argument not in resolve_workflow:
            raise RepositoryError(f"resolve workflow must pass {argument} to the resolver")
    if "--target" in resolve_workflow:
        raise RepositoryError("resolve workflow must not pass a legacy target")


def ensure_retired_sources_absent(root: Path) -> None:
    definition_roots = [root / ".github", root / "locks", root / "profiles", root / "scripts"]
    for directory in definition_roots:
        for path in collect_files(directory, (".py", ".json", ".yml", ".yaml")):
            if path.resolve() == Path(__file__).resolve():
                continue
            if RETIRED_SOURCE_RE.search(read_text(path)):
                raise RepositoryError(f"retired source-family term in {path}")


def ensure_excluded_root_absent(root: Path) -> None:
    definition_roots = [root / ".github", root / "locks", root / "profiles", root / "scripts"]
    for directory in definition_roots:
        for path in collect_files(directory, (".py", ".json", ".yml", ".yaml")):
            if path.resolve() == Path(__file__).resolve():
                continue
            if EXCLUDED_ROOT_RE.search(read_text(path)):
                raise RepositoryError(f"KernelSU-Next is out of scope but appears in {path}")


def ensure_single_version_writer(root: Path) -> None:
    occurrences: List[Path] = []
    for directory in (root / ".github", root / "profiles", root / "locks", root / "scripts"):
        for path in collect_files(directory, (".py", ".json", ".yml", ".yaml")):
            if path.resolve() == Path(__file__).resolve():
                continue
            if VERSION_WRITE_RE.search(read_text(path)):
                occurrences.append(path.relative_to(root))
    allowed = {Path("scripts/verify_release.py")}
    unexpected = sorted(str(path) for path in set(occurrences) - allowed)
    if unexpected:
        raise RepositoryError(
            "only scripts/verify_release.py may write or name kernel local-version machinery; "
            f"unexpected: {', '.join(unexpected)}"
        )
    if Path("scripts/verify_release.py") not in occurrences:
        raise RepositoryError("the required version-contract writer is missing")


def ensure_plan(
    plan_path: Path, root: Path, plans: Dict[Tuple[str, str, str, str, str], Dict[str, Any]]
) -> None:
    plan = load_json(plan_path)
    if not isinstance(plan, dict) or plan.get("schema") != 4:
        raise RepositoryError("build plan must be a schema-4 object")
    selection = plan.get("selection")
    if not isinstance(selection, dict):
        raise RepositoryError("build plan selection is missing")
    release_id = selection.get("release_id")
    root_provider = selection.get("root_provider")
    root_linkage = selection.get("root_linkage")
    hook_mode = selection.get("hook_mode")
    config_profile = selection.get("config_profile")
    uname_suffix = selection.get("uname_suffix")
    key = (release_id, root_provider, root_linkage, hook_mode, config_profile)
    if not all(isinstance(value, str) for value in key) or key not in plans:
        raise RepositoryError("build plan selects an unregistered selector tuple")
    if not isinstance(uname_suffix, str):
        raise RepositoryError("build plan uname_suffix must be a string")
    root_plan = plan.get("root")
    configuration = plan.get("configuration")
    if not isinstance(root_plan, dict) or root_plan.get("id") != root_provider:
        raise RepositoryError("build plan root does not match its selected root provider")
    if not isinstance(configuration, dict) or configuration.get("id") != config_profile:
        raise RepositoryError("build plan configuration does not match its selected config profile")
    version = plan.get("version")
    if not isinstance(version, dict):
        raise RepositoryError("build plan version contract is missing")
    suffix = version.get("local_suffix")
    expected_base_release = version.get("expected_base_release")
    if not isinstance(suffix, str) or not SAFE_SUFFIX_RE.fullmatch(suffix):
        raise RepositoryError("build plan local suffix is invalid")
    release_contract = version.get("release_contract")
    if not isinstance(release_contract, dict):
        raise RepositoryError("build plan uname contract is missing")
    if release_contract.get("mode") == "exact":
        if release_contract.get("expected_uname_release") != expected_base_release + suffix:
            raise RepositoryError("exact build plan uname is not base release plus suffix")
    elif release_contract.get("mode") == "base-prefix-and-suffix":
        if release_contract.get("prefix") != expected_base_release:
            raise RepositoryError("boundary uname contract does not start with the base release")
        if release_contract.get("suffix") != suffix:
            raise RepositoryError("boundary uname contract does not end with the ReNebula suffix")
    else:
        raise RepositoryError("build plan has an unsupported uname contract")
    expected_plan = (
        plans[key]
        if uname_suffix == ""
        else resolve_plan.resolve_plan(
            root,
            release_id,
            root_provider,
            root_linkage,
            hook_mode,
            config_profile,
            uname_suffix,
        )
    )
    if canonical_json(plan) != canonical_json(expected_plan):
        raise RepositoryError("build plan is not the exact deterministic result for its selector tuple")
    try:
        raw = plan_path.read_bytes()
    except OSError as error:
        raise RepositoryError(f"cannot read build plan: {error}") from error
    if raw != canonical_json(expected_plan) + b"\n":
        raise RepositoryError("build plan must use canonical resolver serialization")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="ReNebula repository root",
    )
    parser.add_argument("--plan", type=Path, help="optional resolved plan to verify")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        root = args.repo_root.resolve()
        context = ensure_registry_graph(root)
        ensure_workflow_supply_chain(root)
        ensure_workflow_matches_registry(
            root,
            context["release_ids"],
            context["root_provider_ids"],
            context["root_linkages"],
            context["hook_modes"],
            context["config_profile_ids"],
        )
        ensure_retired_sources_absent(root)
        ensure_excluded_root_absent(root)
        ensure_single_version_writer(root)
        if args.plan:
            ensure_plan(args.plan.resolve(), root, context["plans"])
    except (LockError, resolve_plan.PlanError, RepositoryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
