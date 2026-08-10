#!/usr/bin/env python3
"""Materialize a SHA-pinned Google GKI checkout from a manifest/superproject pair.

The lock deliberately pins the two roots of a Google GKI release rather than
copying a brittle, manually maintained list of every repository.  At run time
we parse the digest-locked manifest and derive every project revision from the
SHA-pinned superproject.  No manifest branch, ``repo init``, or ``repo sync``
is ever followed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
GOOGLESOURCE_HOST = "android.googlesource.com"
MANIFEST_URL = f"https://{GOOGLESOURCE_HOST}/kernel/manifest"
SUPERPROJECT_URL = f"https://{GOOGLESOURCE_HOST}/kernel/superproject"
SOURCE_MODE = "manifest-superproject-v1"


class LockError(ValueError):
    """Raised when the committed source lock is malformed or unsafe."""


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LockError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except OSError as error:
        raise LockError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise LockError(f"invalid JSON in {path}: {error}") from error


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def require_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise LockError(f"{context}.{key} must be a non-empty string")
    return value


def require_integer(container: Dict[str, Any], key: str, context: str) -> int:
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise LockError(f"{context}.{key} must be an integer")
    return value


def require_identifier(value: str, context: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise LockError(f"{context} must be a lowercase identifier")


def require_sha1(value: str, context: str) -> None:
    if not SHA1_RE.fullmatch(value):
        raise LockError(f"{context} must be a lowercase 40-character SHA-1")


def require_sha256(value: str, context: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise LockError(f"{context} must be a lowercase 64-character SHA-256")


def validate_google_url(url: str, context: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != GOOGLESOURCE_HOST:
        raise LockError(f"{context} must use https://{GOOGLESOURCE_HOST}/")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LockError(f"{context} must not contain credentials, query, or fragment")


def validate_relative_path(value: str, context: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise LockError(f"{context} must be a non-empty safe relative path")


def load_lock(lock_path: Path, lock_id: str) -> Dict[str, Any]:
    document = load_json(lock_path)
    if not isinstance(document, dict) or document.get("schema") != 2:
        raise LockError("sources lock must be an object with schema 2")
    locks = document.get("locks")
    if not isinstance(locks, dict):
        raise LockError("sources lock must contain a locks object")
    lock = locks.get(lock_id)
    if not isinstance(lock, dict):
        raise LockError(f"unknown source lock: {lock_id}")
    validate_lock(lock, lock_id)
    return lock


def validate_lock(lock: Dict[str, Any], lock_id: str) -> None:
    require_identifier(lock_id, "source lock id")
    if require_string(lock, "id", lock_id) != lock_id:
        raise LockError(f"{lock_id}.id must equal its enclosing lock id")
    family_id = require_string(lock, "family_id", lock_id)
    release_id = require_string(lock, "release_id", lock_id)
    require_identifier(family_id, f"{lock_id}.family_id")
    require_identifier(release_id, f"{lock_id}.release_id")
    if require_string(lock, "source_mode", lock_id) != SOURCE_MODE:
        raise LockError(f"{lock_id}.source_mode must be {SOURCE_MODE}")

    manifest = lock.get("manifest")
    if not isinstance(manifest, dict):
        raise LockError(f"{lock_id}.manifest must be an object")
    manifest_url = require_string(manifest, "url", f"{lock_id}.manifest")
    validate_google_url(manifest_url, f"{lock_id}.manifest.url")
    if manifest_url != MANIFEST_URL:
        raise LockError(f"{lock_id}.manifest.url must be {MANIFEST_URL}")
    manifest_commit = require_string(manifest, "commit", f"{lock_id}.manifest")
    require_sha1(manifest_commit, f"{lock_id}.manifest.commit")
    manifest_file = require_string(manifest, "file", f"{lock_id}.manifest")
    validate_relative_path(manifest_file, f"{lock_id}.manifest.file")
    if manifest_file != "default.xml":
        raise LockError(f"{lock_id}.manifest.file must be default.xml")
    manifest_digest = require_string(manifest, "sha256", f"{lock_id}.manifest")
    require_sha256(manifest_digest, f"{lock_id}.manifest.sha256")

    superproject = lock.get("superproject")
    if not isinstance(superproject, dict):
        raise LockError(f"{lock_id}.superproject must be an object")
    superproject_url = require_string(superproject, "url", f"{lock_id}.superproject")
    validate_google_url(superproject_url, f"{lock_id}.superproject.url")
    if superproject_url != SUPERPROJECT_URL:
        raise LockError(f"{lock_id}.superproject.url must be {SUPERPROJECT_URL}")
    superproject_commit = require_string(superproject, "commit", f"{lock_id}.superproject")
    require_sha1(superproject_commit, f"{lock_id}.superproject.commit")
    manifest_ref = require_string(superproject, "manifest_ref", f"{lock_id}.superproject")
    if not manifest_ref.startswith("refs/heads/") or any(char.isspace() for char in manifest_ref):
        raise LockError(f"{lock_id}.superproject.manifest_ref must be a refs/heads/ reference")

    materialization = lock.get("materialization")
    if not isinstance(materialization, dict):
        raise LockError(f"{lock_id}.materialization must be an object")
    expected_count = require_integer(materialization, "expected_project_count", f"{lock_id}.materialization")
    if expected_count < 1 or expected_count > 256:
        raise LockError(f"{lock_id}.materialization.expected_project_count is out of range")
    required_paths = materialization.get("required_paths")
    if not isinstance(required_paths, list) or not required_paths:
        raise LockError(f"{lock_id}.materialization.required_paths must be a non-empty array")
    seen_paths = set()
    for index, path in enumerate(required_paths):
        if not isinstance(path, str):
            raise LockError(f"{lock_id}.materialization.required_paths[{index}] must be a string")
        validate_relative_path(path, f"{lock_id}.materialization.required_paths[{index}]")
        if path in seen_paths:
            raise LockError(f"duplicate required project path: {path}")
        seen_paths.add(path)

    known_missing_linkfiles = materialization.get("known_missing_linkfiles", [])
    if not isinstance(known_missing_linkfiles, list):
        raise LockError(
            f"{lock_id}.materialization.known_missing_linkfiles must be an array"
        )
    seen_missing_linkfiles = set()
    seen_missing_destinations = set()
    for index, linkfile in enumerate(known_missing_linkfiles):
        context = f"{lock_id}.materialization.known_missing_linkfiles[{index}]"
        if not isinstance(linkfile, dict) or set(linkfile) != {"source", "dest"}:
            raise LockError(f"{context} must contain exactly source and dest")
        source = require_string(linkfile, "source", context)
        destination = require_string(linkfile, "dest", context)
        validate_relative_path(source, f"{context}.source")
        validate_relative_path(destination, f"{context}.dest")
        pair = (source, destination)
        if pair in seen_missing_linkfiles:
            raise LockError(f"duplicate known missing linkfile: {source} -> {destination}")
        if destination in seen_missing_destinations:
            raise LockError(f"duplicate known missing linkfile destination: {destination}")
        seen_missing_linkfiles.add(pair)
        seen_missing_destinations.add(destination)

    common = lock.get("common")
    if not isinstance(common, dict):
        raise LockError(f"{lock_id}.common must be an object")
    common_path = require_string(common, "path", f"{lock_id}.common")
    validate_relative_path(common_path, f"{lock_id}.common.path")
    if common_path != "common":
        raise LockError(f"{lock_id}.common.path must be common")
    common_commit = require_string(common, "commit", f"{lock_id}.common")
    require_sha1(common_commit, f"{lock_id}.common.commit")
    if common_path not in seen_paths:
        raise LockError(f"{lock_id}.materialization.required_paths must include common")

    version = lock.get("version")
    if not isinstance(version, dict):
        raise LockError(f"{lock_id}.version must be an object")
    expected_base = require_string(version, "expected_base_release", f"{lock_id}.version")
    if not RELEASE_RE.fullmatch(expected_base):
        raise LockError(f"{lock_id}.version.expected_base_release must be X.Y.Z")


def run(command: List[str], *, cwd: Optional[Path] = None) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}")
    try:
        return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as error:
        output = error.output.strip()
        raise RuntimeError(f"command failed ({rendered})\n{output}") from error


def run_bytes(command: List[str], *, cwd: Optional[Path] = None) -> bytes:
    rendered = " ".join(command)
    print(f"+ {rendered}")
    try:
        return subprocess.check_output(command, cwd=cwd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as error:
        output = error.output.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"command failed ({rendered})\n{output}") from error


def ensure_empty_workspace(workspace: Path) -> None:
    if workspace.exists():
        if not workspace.is_dir():
            raise RuntimeError(f"workspace is not a directory: {workspace}")
        if any(workspace.iterdir()):
            raise RuntimeError(
                f"refusing to write into non-empty workspace: {workspace}; use a new directory"
            )
    else:
        workspace.mkdir(parents=True)


def checkout_project(project: Dict[str, str], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", str(destination)])
    run(["git", "-C", str(destination), "remote", "add", "origin", project["url"]])
    fetch_command = [
        "git",
        "-C",
        str(destination),
        "-c",
        "protocol.version=2",
        "fetch",
        "--no-tags",
        "--depth=1",
        "origin",
        project["commit"],
    ]
    for attempt in range(1, 4):
        try:
            run(fetch_command)
            break
        except RuntimeError:
            if attempt == 3:
                raise
            delay = 2**attempt
            print(
                f"exact fetch failed for {project['commit']}; "
                f"retrying attempt {attempt + 1}/3 in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    run(["git", "-C", str(destination), "checkout", "--detach", "--quiet", "FETCH_HEAD"])


def observed_commit(directory: Path) -> str:
    return run(["git", "-C", str(directory), "rev-parse", "HEAD"]).strip()


def parse_manifest(content: bytes) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Return the only source inventory we accept from a digest-locked manifest."""

    try:
        manifest = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise RuntimeError(f"locked manifest XML is invalid: {error}") from error
    if manifest.tag != "manifest":
        raise RuntimeError("locked manifest XML has an unexpected root element")

    projects: List[Dict[str, str]] = []
    linkfiles: List[Dict[str, str]] = []
    project_paths = set()
    destinations = set()
    superproject_seen = False
    for child in manifest:
        if child.tag in {"remote", "default", "notice"}:
            continue
        if child.tag == "superproject":
            if superproject_seen or child.get("name") != "kernel/superproject":
                raise RuntimeError("locked manifest has an unexpected superproject declaration")
            superproject_seen = True
            continue
        if child.tag in {"include", "extend-project", "remove-project"}:
            raise RuntimeError(f"locked manifest uses unsupported dynamic element: {child.tag}")
        if child.tag != "project":
            raise RuntimeError(f"locked manifest has unsupported element: {child.tag}")
        name = child.get("name")
        if not name:
            raise RuntimeError("locked manifest contains a project without a name")
        path = child.get("path") or name
        try:
            validate_relative_path(name, "manifest project name")
            validate_relative_path(path, "manifest project path")
        except LockError as error:
            raise RuntimeError(str(error)) from error
        if path in project_paths:
            raise RuntimeError(f"locked manifest repeats project path: {path}")
        project_paths.add(path)
        projects.append(
            {
                "path": path,
                "name": name,
                "url": f"https://{GOOGLESOURCE_HOST}/{name}",
            }
        )
        for nested in child:
            if nested.tag != "linkfile":
                raise RuntimeError(
                    f"locked manifest project {path} has unsupported element: {nested.tag}"
                )
            source = nested.get("src")
            destination = nested.get("dest")
            if not source or not destination:
                raise RuntimeError("locked manifest contains an incomplete linkfile")
            try:
                if source != ".":
                    validate_relative_path(source, "manifest linkfile source")
                validate_relative_path(destination, "manifest linkfile destination")
            except LockError as error:
                raise RuntimeError(str(error)) from error
            if destination in destinations:
                raise RuntimeError(f"locked manifest repeats linkfile destination: {destination}")
            destinations.add(destination)
            linkfiles.append(
                {"source": path if source == "." else f"{path}/{source}", "dest": destination}
            )
    if not projects:
        raise RuntimeError("locked manifest does not declare any projects")
    return sorted(projects, key=lambda project: project["path"]), sorted(
        linkfiles, key=lambda linkfile: linkfile["dest"]
    )


def get_manifest_content(lock: Dict[str, Any], manifest_dir: Path) -> bytes:
    manifest = lock["manifest"]
    content = run_bytes(
        ["git", "-C", str(manifest_dir), "show", f"{manifest['commit']}:{manifest['file']}"]
    )
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != manifest["sha256"]:
        raise RuntimeError(
            f"manifest digest mismatch: {actual_digest}, expected {manifest['sha256']}"
        )
    return content


def validate_manifest_layout(lock: Dict[str, Any], content: bytes) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    projects, linkfiles = parse_manifest(content)
    expected_count = lock["materialization"]["expected_project_count"]
    if len(projects) != expected_count:
        raise RuntimeError(
            f"manifest declares {len(projects)} projects, lock requires {expected_count}"
        )
    paths = {project["path"] for project in projects}
    required_paths = set(lock["materialization"]["required_paths"])
    missing = sorted(required_paths - paths)
    if missing:
        raise RuntimeError(f"manifest is missing required project path(s): {', '.join(missing)}")
    declared_linkfiles = {
        (linkfile["source"], linkfile["dest"]) for linkfile in linkfiles
    }
    for linkfile in lock["materialization"].get("known_missing_linkfiles", []):
        pair = (linkfile["source"], linkfile["dest"])
        if pair not in declared_linkfiles:
            raise RuntimeError(
                "known missing linkfile is not declared by the locked manifest: "
                f"{linkfile['source']} -> {linkfile['dest']}"
            )
    return projects, linkfiles


def checkout_manifest(lock: Dict[str, Any], workspace: Path) -> Path:
    manifest = lock["manifest"]
    manifest_dir = workspace / ".repo" / "manifests"
    checkout_project({"url": manifest["url"], "commit": manifest["commit"]}, manifest_dir)
    get_manifest_content(lock, manifest_dir)
    return manifest_dir


def checkout_superproject(lock: Dict[str, Any], workspace: Path) -> Path:
    superproject = lock["superproject"]
    superproject_dir = workspace / ".repo" / "superproject"
    checkout_project(
        {"url": superproject["url"], "commit": superproject["commit"]}, superproject_dir
    )
    return superproject_dir


def verify_supermanifest(lock: Dict[str, Any], superproject_dir: Path) -> None:
    superproject = lock["superproject"]
    content = run_bytes(
        ["git", "-C", str(superproject_dir), "show", f"{superproject['commit']}:.supermanifest"]
    ).decode("utf-8", errors="strict")
    entries = set()
    for line in content.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) != 3:
            raise RuntimeError(f"invalid .supermanifest entry: {line!r}")
        entries.add(tuple(fields))
    expected = ("kernel/manifest", superproject["manifest_ref"], lock["manifest"]["commit"])
    if expected not in entries:
        raise RuntimeError("superproject does not map to the locked manifest commit")


def superproject_gitlinks(lock: Dict[str, Any], superproject_dir: Path) -> Dict[str, str]:
    superproject = lock["superproject"]
    if observed_commit(superproject_dir) != superproject["commit"]:
        raise RuntimeError("superproject checkout does not match lock")
    verify_supermanifest(lock, superproject_dir)
    output = run(
        ["git", "-C", str(superproject_dir), "ls-tree", "-r", superproject["commit"]]
    )
    gitlinks: Dict[str, str] = {}
    for line in output.splitlines():
        if "\t" not in line:
            raise RuntimeError(f"invalid superproject tree entry: {line!r}")
        metadata, path = line.split("\t", 1)
        fields = metadata.split()
        if len(fields) != 3:
            raise RuntimeError(f"invalid superproject tree metadata: {line!r}")
        mode, object_type, commit = fields
        if mode != "160000":
            continue
        if object_type != "commit" or not SHA1_RE.fullmatch(commit):
            raise RuntimeError(f"invalid superproject gitlink: {line!r}")
        try:
            validate_relative_path(path, "superproject gitlink path")
        except LockError as error:
            raise RuntimeError(str(error)) from error
        if path in gitlinks:
            raise RuntimeError(f"superproject repeats gitlink: {path}")
        gitlinks[path] = commit
    return gitlinks


def derive_projects(
    lock: Dict[str, Any],
    manifest_projects: List[Dict[str, str]],
    superproject_dir: Path,
) -> List[Dict[str, str]]:
    gitlinks = superproject_gitlinks(lock, superproject_dir)
    manifest_paths = {project["path"] for project in manifest_projects}
    gitlink_paths = set(gitlinks)
    if manifest_paths != gitlink_paths:
        missing = sorted(manifest_paths - gitlink_paths)
        unexpected = sorted(gitlink_paths - manifest_paths)
        raise RuntimeError(
            "superproject gitlinks do not exactly match default.xml; "
            f"missing={missing}, unexpected={unexpected}"
        )
    projects: List[Dict[str, str]] = []
    for project in manifest_projects:
        projects.append({**project, "commit": gitlinks[project["path"]]})
    common = next(project for project in projects if project["path"] == lock["common"]["path"])
    if common["commit"] != lock["common"]["commit"]:
        raise RuntimeError(
            "superproject common gitlink does not match the source lock "
            f"({common['commit']} != {lock['common']['commit']})"
        )
    return projects


def verify_project(project: Dict[str, str], workspace: Path) -> Dict[str, str]:
    path = workspace / project["path"]
    if not path.is_dir():
        raise RuntimeError(f"locked project is missing: {project['path']}")
    actual = observed_commit(path)
    expected = project["commit"]
    if actual != expected:
        raise RuntimeError(f"{project['path']} is {actual}, expected {expected}")
    status = run(["git", "-C", str(path), "status", "--porcelain"])
    if status.strip():
        raise RuntimeError(f"{project['path']} has local modifications")
    return {key: project[key] for key in ("path", "name", "url", "commit")}


def _known_missing_linkfile_pairs(
    known_missing_linkfiles: List[Dict[str, str]],
) -> set[Tuple[str, str]]:
    return {
        (linkfile["source"], linkfile["dest"])
        for linkfile in known_missing_linkfiles
    }


def create_linkfiles(
    linkfiles: List[Dict[str, str]],
    workspace: Path,
    known_missing_linkfiles: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    known_missing_linkfiles = known_missing_linkfiles or []
    known_missing = _known_missing_linkfile_pairs(known_missing_linkfiles)
    omitted: List[Dict[str, str]] = []
    observed_known_missing = set()
    for linkfile in linkfiles:
        source = workspace / linkfile["source"]
        destination = workspace / linkfile["dest"]
        pair = (linkfile["source"], linkfile["dest"])
        if pair in known_missing:
            observed_known_missing.add(pair)
            if source.exists() or source.is_symlink():
                raise RuntimeError(
                    f"known missing linkfile source now exists: {linkfile['source']}"
                )
            if destination.exists() or destination.is_symlink():
                raise RuntimeError(
                    f"known missing linkfile destination must be absent: {linkfile['dest']}"
                )
            omitted.append(linkfile)
            continue
        if not source.exists():
            raise RuntimeError(f"linkfile source is missing: {linkfile['source']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        relative_source = os.path.relpath(source, destination.parent)
        if destination.exists() or destination.is_symlink():
            if not destination.is_symlink() or destination.resolve() != source.resolve():
                raise RuntimeError(f"refusing to replace existing linkfile: {linkfile['dest']}")
            continue
        destination.symlink_to(relative_source)
    if observed_known_missing != known_missing:
        raise RuntimeError("known missing linkfile is not present in the materialized manifest")
    return omitted


def verify_linkfiles(
    linkfiles: List[Dict[str, str]],
    workspace: Path,
    known_missing_linkfiles: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    known_missing_linkfiles = known_missing_linkfiles or []
    known_missing = _known_missing_linkfile_pairs(known_missing_linkfiles)
    omitted: List[Dict[str, str]] = []
    observed_known_missing = set()
    for linkfile in linkfiles:
        source = workspace / linkfile["source"]
        destination = workspace / linkfile["dest"]
        pair = (linkfile["source"], linkfile["dest"])
        if pair in known_missing:
            observed_known_missing.add(pair)
            if source.exists() or source.is_symlink():
                raise RuntimeError(
                    f"known missing linkfile source now exists: {linkfile['source']}"
                )
            if destination.exists() or destination.is_symlink():
                raise RuntimeError(
                    f"known missing linkfile destination must be absent: {linkfile['dest']}"
                )
            omitted.append(linkfile)
            continue
        if not destination.is_symlink() or destination.resolve() != source.resolve():
            raise RuntimeError(f"linkfile does not match manifest: {linkfile['dest']}")
    if observed_known_missing != known_missing:
        raise RuntimeError("known missing linkfile is not present in the materialized manifest")
    return omitted


def write_record(
    lock_id: str,
    lock: Dict[str, Any],
    workspace: Path,
    projects: List[Dict[str, str]],
    linkfiles: List[Dict[str, str]],
    omitted_linkfiles: List[Dict[str, str]],
) -> None:
    record = {
        "schema": 2,
        "source_lock": lock_id,
        "family_id": lock["family_id"],
        "release_id": lock["release_id"],
        "manifest_commit": lock["manifest"]["commit"],
        "manifest_sha256": lock["manifest"]["sha256"],
        "superproject_commit": lock["superproject"]["commit"],
        "manifest_ref": lock["superproject"]["manifest_ref"],
        "common_commit": lock["common"]["commit"],
        "projects": projects,
        "linkfiles": linkfiles,
        "omitted_linkfiles": omitted_linkfiles,
    }
    (workspace / "renebula-source-record.json").write_bytes(canonical_json(record) + b"\n")


def collect_inventory(lock: Dict[str, Any], workspace: Path) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    manifest_dir = workspace / ".repo" / "manifests"
    if observed_commit(manifest_dir) != lock["manifest"]["commit"]:
        raise RuntimeError("manifest checkout does not match lock")
    content = get_manifest_content(lock, manifest_dir)
    manifest_projects, linkfiles = validate_manifest_layout(lock, content)
    projects = derive_projects(lock, manifest_projects, workspace / ".repo" / "superproject")
    return projects, linkfiles


def synchronize(lock_id: str, lock: Dict[str, Any], workspace: Path) -> None:
    ensure_empty_workspace(workspace)
    checkout_manifest(lock, workspace)
    checkout_superproject(lock, workspace)
    projects, linkfiles = collect_inventory(lock, workspace)
    for project in projects:
        checkout_project(project, workspace / project["path"])
    create_linkfiles(
        linkfiles,
        workspace,
        lock["materialization"].get("known_missing_linkfiles", []),
    )
    verify(lock_id, lock, workspace)


def verify(lock_id: str, lock: Dict[str, Any], workspace: Path) -> None:
    projects, linkfiles = collect_inventory(lock, workspace)
    verified_projects = [verify_project(project, workspace) for project in projects]
    omitted_linkfiles = verify_linkfiles(
        linkfiles,
        workspace,
        lock["materialization"].get("known_missing_linkfiles", []),
    )
    write_record(
        lock_id,
        lock,
        workspace,
        verified_projects,
        linkfiles,
        omitted_linkfiles,
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "locks" / "sources.lock.json",
        help="path to the committed source lock",
    )
    parser.add_argument("--lock-id", required=True, help="source lock identifier")
    parser.add_argument("--workspace", required=True, type=Path, help="new or existing checkout directory")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an existing checkout without writing or fetching",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        lock = load_lock(args.lock.resolve(), args.lock_id)
        workspace = args.workspace.resolve()
        if args.verify_only:
            verify(args.lock_id, lock, workspace)
        else:
            synchronize(args.lock_id, lock, workspace)
    except (LockError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
