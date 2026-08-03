#!/usr/bin/env python3
"""Static gate for ReNebula's immutable multi-KMI definitions.

This runs before any GKI source is downloaded.  It validates the registry,
family/release/lock graph, the generated Actions selector, and boundaries that
prevent floating sources, a second kernel-version writer, retired Vivo inputs,
or KernelSU-Next support from returning.
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
OPTIONS_BLOCK_RE = re.compile(
    r"^\s*# BEGIN GENERATED RELEASE OPTIONS\s*$"
    r"(?P<options>.*?)"
    r"^\s*# END GENERATED RELEASE OPTIONS\s*$",
    re.MULTILINE | re.DOTALL,
)
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


def ensure_registry_graph(root: Path) -> Dict[str, Any]:
    registry, registry_path = resolve_plan.load_registry(root)
    entries = registry["releases"]
    entry_ids = {entry["id"] for entry in entries}
    entry_families = {entry["family_id"] for entry in entries}
    if set(REQUIRED_FAMILY_ADAPTERS) != entry_families:
        missing = sorted(set(REQUIRED_FAMILY_ADAPTERS) - entry_families)
        unexpected = sorted(entry_families - set(REQUIRED_FAMILY_ADAPTERS))
        raise RepositoryError(
            f"registry must cover every supported complete KMI chain; "
            f"missing={missing}, unexpected={unexpected}"
        )
    if len(entries) != len(entry_families):
        raise RepositoryError("registry must contain exactly one current release per KMI family")

    release_files = {
        path.relative_to(root).as_posix()
        for path in collect_files(root / "profiles" / "releases", (".json",))
    }
    expected_release_files = {entry["profile"] for entry in entries}
    if release_files != expected_release_files:
        raise RepositoryError(
            "release profile files must exactly match the registry; "
            f"missing={sorted(expected_release_files - release_files)}, "
            f"unexpected={sorted(release_files - expected_release_files)}"
        )
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
    plans: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        release_id = entry["id"]
        _, release, _ = resolve_plan.load_release(root, release_id, registry)
        family, _ = resolve_plan.load_family(root, entry["family_id"])
        adapter = family["build"]["adapter"]
        if adapter != REQUIRED_FAMILY_ADAPTERS[entry["family_id"]]:
            raise RepositoryError(f"{entry['family_id']} has the wrong build adapter")
        lock_id = release["source_lock"]
        expected_lock_ids.add(lock_id)
        lock = load_lock(root / "locks" / "sources.lock.json", lock_id)
        if lock["release_id"] != release_id or lock["family_id"] != entry["family_id"]:
            raise RepositoryError(f"{release_id} does not agree with its source lock")
        if lock["version"]["expected_base_release"] != release["version"]["expected_base_release"]:
            raise RepositoryError(f"{release_id} has a source-lock version mismatch")
        plans[release_id] = resolve_plan.resolve_plan(root, release_id)
    if set(locks) != expected_lock_ids:
        raise RepositoryError(
            "sources lock entries must exactly match registered releases; "
            f"missing={sorted(expected_lock_ids - set(locks))}, "
            f"unexpected={sorted(set(locks) - expected_lock_ids)}"
        )
    if len(expected_lock_ids) != len(entry_ids):
        raise RepositoryError("each registered release must have its own immutable source lock")
    return {
        "registry": registry,
        "registry_path": registry_path,
        "plans": plans,
        "release_ids": [entry["id"] for entry in entries],
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


def workflow_release_options(path: Path) -> List[str]:
    content = read_text(path)
    match = OPTIONS_BLOCK_RE.search(content)
    if not match:
        raise RepositoryError("dispatch workflow lacks the generated release options block")
    options: List[str] = []
    for line in match.group("options").splitlines():
        if not line.strip():
            continue
        option_match = OPTION_LINE_RE.fullmatch(line)
        if not option_match:
            raise RepositoryError(f"invalid generated release option line: {line!r}")
        options.append(option_match.group(1))
    if not options:
        raise RepositoryError("dispatch workflow does not expose any release option")
    return options


def ensure_workflow_matches_registry(root: Path, release_ids: List[str]) -> None:
    dispatch_path = root / ".github" / "workflows" / "dispatch.yml"
    options = workflow_release_options(dispatch_path)
    if options != release_ids:
        raise RepositoryError(
            "dispatch release choices diverge from profiles/registry.json; "
            f"workflow={options}, registry={release_ids}"
        )
    dispatch = read_text(dispatch_path)
    if "release_id:" not in dispatch or "target:" in dispatch:
        raise RepositoryError("dispatch workflow must accept release_id rather than a target")
    if not re.search(r"(?m)^\s*needs:\s*\[resolve, verify\]\s*$", dispatch):
        raise RepositoryError("build must wait for both resolve and static verification")
    resolve_workflow = read_text(root / ".github" / "workflows" / "resolve-plan.yml")
    if "--release-id" not in resolve_workflow or "--target" in resolve_workflow:
        raise RepositoryError("resolve workflow must pass a static release id to the resolver")


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


def ensure_plan(plan_path: Path, root: Path, plans: Dict[str, Dict[str, Any]]) -> None:
    plan = load_json(plan_path)
    if not isinstance(plan, dict) or plan.get("schema") != 2:
        raise RepositoryError("build plan must be a schema-2 object")
    selection = plan.get("selection")
    if not isinstance(selection, dict):
        raise RepositoryError("build plan selection is missing")
    release_id = selection.get("release_id")
    if not isinstance(release_id, str) or release_id not in plans:
        raise RepositoryError("build plan selects an unregistered release")
    if selection.get("root") != "none" or selection.get("features") != []:
        raise RepositoryError("P0 build plan must retain root=none and no optional features")
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
    expected_plan = plans[release_id]
    if canonical_json(plan) != canonical_json(expected_plan):
        raise RepositoryError("build plan is not the exact deterministic result for its release")
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
        ensure_workflow_matches_registry(root, context["release_ids"])
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
