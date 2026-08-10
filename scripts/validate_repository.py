#!/usr/bin/env python3
"""Static gate for ReNebula's schema-5 catalog, workflow, and plan envelope."""

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
    r"(?:curl\s*\||wget\s*\||git\s+(?:clone|pull)\b|repo\s+(?:init|sync)\b|"
    r"continue-on-error\s*:\s*true|\|\|\s*true)",
    re.IGNORECASE,
)
RETIRED_SOURCE_RE = re.compile(r"(?:is_vki|vki_tarball_url|vivo_tarball_url)", re.IGNORECASE)
EXCLUDED_ROOT_RE = re.compile(r"(?:kernel.?su.?next|\bksun\b|\bksu.?next\b)", re.IGNORECASE)
VERSION_WRITE_RE = re.compile(r"(?:scripts/setlocalversion|CONFIG_LOCALVERSION)")
SAFE_SUFFIX_RE = re.compile(r"^-[A-Za-z0-9._-]+$")
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
ROOT_SOURCES = ["none", "kernelsu", "sukisu", "resukisu"]
PUBLIC_INPUTS = ["release_id", "root_source", "susfs", "kpm", "vivo_vermagic", "uname_tag"]


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


def ensure_profile_files(root: Path, directory: str, expected: set[str]) -> None:
    actual = {
        path.relative_to(root).as_posix()
        for path in collect_files(root / "profiles" / directory, (".json",))
    }
    if actual != expected:
        raise RepositoryError(
            f"profiles/{directory} diverges from registry; "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


def ensure_registry_graph(root: Path) -> Dict[str, Any]:
    registry, _ = resolve_plan.load_registry(root)
    releases = registry["releases"]
    root_entries = registry["root_sources"]
    feature_entries = registry["features"]
    families = {entry["family_id"] for entry in releases}
    if families != set(REQUIRED_FAMILY_ADAPTERS) or len(releases) != len(families):
        raise RepositoryError("registry must contain one release for every complete GKI KMI chain")
    if [entry["id"] for entry in root_entries] != ROOT_SOURCES:
        raise RepositoryError("registry root source order is invalid")
    if [entry["id"] for entry in feature_entries] != ["susfs", "kpm", "vivo-vermagic"]:
        raise RepositoryError("registry feature set is invalid")
    expected_feature_roots = {
        "susfs": ["kernelsu", "sukisu", "resukisu"],
        "kpm": ["sukisu"],
        "vivo-vermagic": ["kernelsu", "sukisu", "resukisu"],
    }
    for entry in feature_entries:
        profile, _ = resolve_plan.load_feature_profile(root, entry["id"], registry)
        if profile["root_sources"] != expected_feature_roots[entry["id"]]:
            raise RepositoryError(f"feature {entry['id']} has an invalid root provider boundary")
    ensure_profile_files(root, "releases", {entry["profile"] for entry in releases})
    ensure_profile_files(root, "root-providers", {entry["profile"] for entry in root_entries})
    ensure_profile_files(root, "features", {entry["profile"] for entry in feature_entries})
    ensure_profile_files(root, "families", {f"profiles/families/{family}.json" for family in families})
    if (root / "profiles" / "config-profiles").exists() and collect_files(
        root / "profiles" / "config-profiles", (".json",)
    ):
        raise RepositoryError("public config profiles must not return")

    google_document = load_json(root / "locks" / "sources.lock.json")
    if not isinstance(google_document, dict) or google_document.get("schema") != 2:
        raise RepositoryError("Google source lock must use schema 2")
    google_locks = google_document.get("locks")
    if not isinstance(google_locks, dict):
        raise RepositoryError("Google source lock lacks locks")
    expected_google_locks = set()
    plain_plans: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in releases:
        _, release, _ = resolve_plan.load_release(root, entry["id"], registry)
        family, _ = resolve_plan.load_family(root, entry["family_id"])
        if family["build"]["adapter"] != REQUIRED_FAMILY_ADAPTERS[entry["family_id"]]:
            raise RepositoryError(f"{entry['family_id']} has the wrong build adapter")
        expected_google_locks.add(release["source_lock"])
        lock = load_lock(root / "locks" / "sources.lock.json", release["source_lock"])
        if lock["release_id"] != entry["id"] or lock["family_id"] != entry["family_id"]:
            raise RepositoryError(f"{entry['id']} does not match its Google source lock")
        for root_source in ROOT_SOURCES:
            first = resolve_plan.resolve_plan(root, entry["id"], root_source)
            second = resolve_plan.resolve_plan(root, entry["id"], root_source)
            if canonical_json(first) != canonical_json(second):
                raise RepositoryError(f"request is not deterministic: {entry['id']}/{root_source}")
            plain_plans[(entry["id"], root_source)] = first
        if family["features"]["susfs"]:
            for root_source in ROOT_SOURCES[1:]:
                resolve_plan.resolve_plan(
                    root, entry["id"], root_source, susfs=True
                )
        if family["features"]["kpm"]:
            resolve_plan.resolve_plan(
                root, entry["id"], "sukisu", kpm=True
            )
        if family["features"]["vivo_vermagic"]:
            for root_source in ROOT_SOURCES[1:]:
                resolve_plan.resolve_plan(
                    root, entry["id"], root_source, vivo_vermagic=True
                )
    if set(google_locks) != expected_google_locks:
        raise RepositoryError("Google source locks must exactly match registered releases")

    root_lock_document = load_json(root / "locks" / "root-sources.lock.json")
    if not isinstance(root_lock_document, dict) or root_lock_document.get("schema") != 2:
        raise RepositoryError("root source locks must use schema 2")
    root_locks = root_lock_document.get("locks")
    if not isinstance(root_locks, dict) or {item.get("provider") for item in root_locks.values()} != set(ROOT_SOURCES[1:]):
        raise RepositoryError("root source locks must contain exactly the three public providers")
    if len(root_locks) != 3:
        raise RepositoryError("each root provider must have exactly one current source lock")

    feature_locks = load_json(root / "locks" / "feature-sources.lock.json")
    if not isinstance(feature_locks, dict) or feature_locks.get("kind") != "static-feature-source-locks":
        raise RepositoryError("feature source lock document is invalid")
    if not isinstance(feature_locks.get("locks"), dict) or len(feature_locks["locks"]) != 8:
        raise RepositoryError("feature locks must contain seven SUSFS branches and one KPM source")
    return {
        "registry": registry,
        "release_ids": [entry["id"] for entry in releases],
        "plain_plans": plain_plans,
    }


def ensure_workflow(root: Path, release_ids: List[str]) -> None:
    workflow_files = collect_files(root / ".github" / "workflows", (".yml", ".yaml"))
    if [path.name for path in workflow_files] != ["build.yml"]:
        raise RepositoryError("repository must contain exactly one build.yml workflow")
    path = workflow_files[0]
    content = read_text(path)
    if FORBIDDEN_WORKFLOW_RE.search(content):
        raise RepositoryError("workflow contains a dynamic or failure-masking source operation")
    for reference in USES_RE.findall(content):
        if not PINNED_ACTION_RE.fullmatch(reference):
            raise RepositoryError(f"unpinned action reference: {reference}")
    if any(trigger in content for trigger in ("workflow_call:", "pull_request:", "push:", "schedule:")):
        raise RepositoryError("build.yml must be manual workflow_dispatch only")
    dispatch = re.search(
        r"(?ms)^  workflow_dispatch:\s*\n    inputs:\s*\n(?P<body>.*?)(?=^permissions:)",
        content,
    )
    if dispatch is None:
        raise RepositoryError("workflow_dispatch inputs block is missing")
    input_ids = re.findall(r"(?m)^      ([a-z][a-z0-9_]*):\s*$", dispatch.group("body"))
    if input_ids != PUBLIC_INPUTS:
        raise RepositoryError(f"workflow public inputs are invalid: {input_ids}")
    release_block = re.search(
        r"(?ms)^      release_id:\s*\n.*?^        options:\s*\n(?P<options>(?:^          - [^\n]+\n)+)",
        content,
    )
    root_block = re.search(
        r"(?ms)^      root_source:\s*\n.*?^        options:\s*\n(?P<options>(?:^          - [^\n]+\n)+)",
        content,
    )
    if release_block is None or root_block is None:
        raise RepositoryError("workflow choice options are missing")
    parse_options = lambda block: [line.strip()[2:] for line in block.group("options").splitlines()]
    if parse_options(release_block) != release_ids:
        raise RepositoryError("workflow release choices diverge from registry")
    if parse_options(root_block) != ROOT_SOURCES:
        raise RepositoryError("workflow root choices diverge from registry")
    if "__select_" in content:
        raise RepositoryError("workflow must not publish invalid sentinel defaults")
    for name in ("susfs", "kpm", "vivo_vermagic"):
        block = re.search(rf"(?ms)^      {name}:\s*\n(?P<body>.*?)(?=^      [a-z]|^permissions:)", content)
        if block is None or "type: boolean" not in block.group("body") or "default: false" not in block.group("body"):
            raise RepositoryError(f"workflow {name} must be a default-false boolean")
    for required in (
        "fromJSON(needs.prepare.outputs.variants)",
        "matrix.variant.id",
        "scripts/apply_root_adapter.py",
        "scripts/apply_feature_adapter.py",
        "scripts/configure_variant.py",
        "scripts/verify_release.py",
    ):
        if required not in content:
            raise RepositoryError(f"workflow is missing matrix contract: {required}")
    if "6.6 及以上会拒绝" not in content:
        raise RepositoryError("Vivo workflow help must describe the 6.6+ rejection")


def ensure_retired_sources_absent(root: Path) -> None:
    for directory in (root / ".github", root / "locks", root / "profiles", root / "scripts"):
        for path in collect_files(directory, (".py", ".json", ".yml", ".yaml")):
            if path.resolve() != Path(__file__).resolve() and RETIRED_SOURCE_RE.search(read_text(path)):
                raise RepositoryError(f"retired source-family term in {path}")


def ensure_kernel_su_next_absent(root: Path) -> None:
    for directory in (root / ".github", root / "locks", root / "profiles"):
        for path in collect_files(directory, (".json", ".yml", ".yaml")):
            if EXCLUDED_ROOT_RE.search(read_text(path)):
                raise RepositoryError(f"KernelSU-Next is excluded but appears in {path}")


def ensure_single_version_writer(root: Path) -> None:
    occurrences: List[Path] = []
    for directory in (root / ".github", root / "profiles", root / "locks", root / "scripts"):
        for path in collect_files(directory, (".py", ".json", ".yml", ".yaml")):
            if path.resolve() == Path(__file__).resolve():
                continue
            if VERSION_WRITE_RE.search(read_text(path)):
                occurrences.append(path.relative_to(root))
    allowed = {Path("scripts/configure_variant.py")}
    if set(occurrences) != allowed:
        raise RepositoryError(
            "scripts/configure_variant.py must be the only LOCALVERSION writer; "
            f"found={sorted(str(path) for path in set(occurrences))}"
        )


def ensure_plan(plan_path: Path, root: Path) -> None:
    plan = load_json(plan_path)
    if not isinstance(plan, dict) or plan.get("schema") != 5:
        raise RepositoryError("build plan must be a schema-5 object")
    selection = plan.get("selection")
    if not isinstance(selection, dict):
        raise RepositoryError("build plan selection is missing")
    required = {
        "release_id": str,
        "root_source": str,
        "susfs": bool,
        "kpm": bool,
        "vivo_vermagic": bool,
        "uname_tag": str,
    }
    for key, expected_type in required.items():
        if not isinstance(selection.get(key), expected_type):
            raise RepositoryError(f"build plan selection.{key} has the wrong type")
    expected = resolve_plan.resolve_plan(
        root,
        selection["release_id"],
        selection["root_source"],
        selection["susfs"],
        selection["kpm"],
        selection["vivo_vermagic"],
        selection["uname_tag"],
    )
    if canonical_json(plan) != canonical_json(expected):
        raise RepositoryError("build plan is not the exact deterministic resolver result")
    for variant in plan["variants"]:
        suffix = variant.get("version", {}).get("local_suffix")
        if not isinstance(suffix, str) or not SAFE_SUFFIX_RE.fullmatch(suffix):
            raise RepositoryError("build plan variant local suffix is invalid")
    try:
        raw = plan_path.read_bytes()
    except OSError as error:
        raise RepositoryError(f"cannot read build plan: {error}") from error
    if raw != canonical_json(expected) + b"\n":
        raise RepositoryError("build plan must use canonical resolver serialization")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--plan", type=Path)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        root = args.repo_root.resolve()
        context = ensure_registry_graph(root)
        ensure_workflow(root, context["release_ids"])
        ensure_retired_sources_absent(root)
        ensure_kernel_su_next_absent(root)
        ensure_single_version_writer(root)
        if args.plan:
            ensure_plan(args.plan.resolve(), root)
    except (LockError, resolve_plan.PlanError, RepositoryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
