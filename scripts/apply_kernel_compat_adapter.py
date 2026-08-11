#!/usr/bin/env python3
"""Apply source-family kernel guards selected by one immutable build plan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sync_google_gki import LockError, canonical_json, load_json


ANDROID16_612_FAMILY = "android16-6.12"
INIT_TASK_PID_READ_ADAPTER = "android16-6.12-init-task-pid-read-v1"
TASK_PID_PTR_ANCHOR = """static struct pid **task_pid_ptr(struct task_struct *task, enum pid_type type)
{
\treturn (type == PIDTYPE_PID) ?
\t\t&task->thread_pid :
\t\t&task->signal->pids[type];
}
"""
TASK_PID_READ_HELPER = """
static struct pid *renebula_task_pid_read(struct task_struct *task,
\t\t\t\t\t enum pid_type type)
{
\tstruct pid *pid = rcu_dereference(*task_pid_ptr(task, type));

\tif (unlikely(task == &init_task && pid != &init_struct_pid)) {
\t\tpr_warn_once("ReNebula: repaired corrupted init_task PID reference\\n");
\t\treturn &init_struct_pid;
\t}
\treturn pid;
}
"""
GET_TASK_PID_OLD = "pid = get_pid(rcu_dereference(*task_pid_ptr(task, type)));"
GET_TASK_PID_NEW = "pid = get_pid(renebula_task_pid_read(task, type));"
TASK_PID_NR_OLD = "nr = pid_nr_ns(rcu_dereference(*task_pid_ptr(task, type)), ns);"
TASK_PID_NR_NEW = "nr = pid_nr_ns(renebula_task_pid_read(task, type), ns);"


class CompatError(ValueError):
    """Raised when a compatibility adapter cannot be applied safely."""


def require_string(container: Dict[str, Any], key: str, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise CompatError(f"{context}.{key} must be a non-empty string")
    return value


def validate_scope(plan: Dict[str, Any], variant_id: str) -> Tuple[str, str]:
    if plan.get("schema") != 5:
        raise CompatError("plan.schema must be 5")
    selection = plan.get("selection")
    variants = plan.get("variants")
    if not isinstance(selection, dict) or not isinstance(variants, list):
        raise CompatError("plan.selection and plan.variants must be objects")
    family_id = require_string(selection, "family_id", "plan.selection")
    matches: List[Dict[str, Any]] = []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            raise CompatError(f"plan.variants[{index}] must be an object")
        candidate_id = require_string(variant, "id", f"plan.variants[{index}]")
        require_string(variant, "artifact", f"plan.variants[{index}]")
        if candidate_id == variant_id:
            matches.append(variant)
    if len(matches) != 1:
        raise CompatError(f"variant is not present exactly once in plan: {variant_id}")
    return family_id, require_string(matches[0], "artifact", f"plan variant {variant_id}")


def apply_init_task_pid_read_guard(pid_c: Path) -> None:
    try:
        original = pid_c.read_text(encoding="utf-8")
    except OSError as error:
        raise CompatError(f"cannot read locked kernel PID source {pid_c}: {error}") from error
    anchors = {
        "task_pid_ptr": original.count(TASK_PID_PTR_ANCHOR),
        "get_task_pid": original.count(GET_TASK_PID_OLD),
        "task_pid_nr": original.count(TASK_PID_NR_OLD),
        "managed_helper": original.count("renebula_task_pid_read"),
    }
    if anchors != {
        "task_pid_ptr": 1,
        "get_task_pid": 1,
        "task_pid_nr": 1,
        "managed_helper": 0,
    }:
        raise CompatError(f"Android 16 / 6.12 PID source anchors drifted: {anchors}")
    adapted = original.replace(
        TASK_PID_PTR_ANCHOR,
        TASK_PID_PTR_ANCHOR + TASK_PID_READ_HELPER,
        1,
    )
    adapted = adapted.replace(GET_TASK_PID_OLD, GET_TASK_PID_NEW, 1)
    adapted = adapted.replace(TASK_PID_NR_OLD, TASK_PID_NR_NEW, 1)
    if adapted.count("renebula_task_pid_read(task, type)") != 2:
        raise CompatError("Android 16 / 6.12 PID guard was not wired exactly twice")
    pid_c.write_text(adapted, encoding="utf-8", newline="\n")


def prepare_kernel_compat(
    plan: Dict[str, Any], variant_id: str, workspace: Path
) -> Dict[str, Any]:
    family_id, artifact = validate_scope(plan, variant_id)
    applied: List[str] = []
    if family_id == ANDROID16_612_FAMILY and artifact == "image":
        apply_init_task_pid_read_guard(workspace / "common" / "kernel" / "pid.c")
        applied.append(INIT_TASK_PID_READ_ADAPTER)
    return {
        "schema": 1,
        "family_id": family_id,
        "variant_id": variant_id,
        "applied": applied,
    }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--variant-id", required=True)
    parser.add_argument("--kernel-workspace", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        workspace = args.kernel_workspace.resolve()
        if not workspace.is_dir():
            raise CompatError(f"kernel workspace is not a directory: {workspace}")
        plan = load_json(args.plan)
        if not isinstance(plan, dict):
            raise CompatError("build plan must contain a JSON object")
        record = prepare_kernel_compat(plan, args.variant_id, workspace)
        (workspace / "renebula-compat-record.json").write_bytes(
            canonical_json(record) + b"\n"
        )
    except (CompatError, LockError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
