# GKI Release Snapshots and `SET_INIT_PGRP` Repair Implementation Plan

> **Superseded:** The PID1 portion of this historical plan was replaced after
> later pstore and Image-symbol evidence. See
> `2026-08-11-android16-6.12-init-task-pid-guard.md` for the active PID0 design
> and implementation.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible Android 16 / 6.12.58 release choice, repair the shared KernelSU-family `SET_INIT_PGRP` implementation without removing it, and run one ReSukiSU + SUSFS verification build.

**Architecture:** `release_id` remains the only kernel snapshot selector; each choice resolves to one complete immutable manifest/superproject/common tuple. The root adapter performs one strict, recorded source transformation on the exact KernelSU, SukiSU, or ReSukiSU checkout so built-in and LKM variants share the same PID-1 process-group implementation.

**Tech Stack:** Python 3 standard library, JSON source locks, GitHub Actions YAML, Linux kernel C, Python `unittest`, Git/GitHub CLI.

## Global Constraints

- Keep exactly one manually dispatched workflow and the existing six public inputs.
- Do not add a free-form sublevel input; release choices are complete immutable snapshots.
- Keep KernelSU-Next excluded.
- Preserve both built-in Image and LKM outputs for every non-`none` root source.
- Preserve the `SET_INIT_PGRP` ioctl and userspace fallback behavior.
- Apply no fuzzy provider patch: the adapter must reject zero or multiple source matches.
- Do not stage `.artifact-audit-20260811/` or `temp.gz`.
- The verification build is `android16-6.12-2025-12-r1`, ReSukiSU, SUSFS on, KPM off, Vivo vermagic off, empty uname tag.

---

### Task 1: Lock and expose the official 6.12.58 snapshot

**Files:**
- Create: `profiles/releases/android16-6.12-2025-12-r1.json`
- Modify: `profiles/registry.json`
- Modify: `locks/sources.lock.json`
- Modify: `.github/workflows/build.yml`
- Modify: `scripts/validate_repository.py`
- Modify: `tests/test_resolve_plan.py`
- Modify: `tests/test_repository_contract.py`

**Interfaces:**
- Consumes: the existing schema-5 release registry and schema-2 `manifest-superproject-v1` source lock.
- Produces: selectable release ID `android16-6.12-2025-12-r1` with `expected_base_release=6.12.58`.

- [ ] **Step 1: Write failing catalog tests**

Add a resolver test that loads the new ID directly and asserts:

```python
plan = resolve_plan.resolve_plan(
    self.REPO_ROOT,
    "android16-6.12-2025-12-r1",
    "resukisu",
    susfs=True,
)
self.assertEqual(plan["selection"]["family_id"], "android16-6.12")
self.assertEqual(plan["version"]["expected_base_release"], "6.12.58")
self.assertEqual(
    plan["source"]["common_commit"],
    "67fe3c9df146f5752b3cd5c69c8e0460221a8018",
)
```

Extend the workflow contract test to require the new release option and readable UTF-8 Chinese help that mentions both `6.12.58` and `6.12.92`.

- [ ] **Step 2: Run tests and verify the expected RED state**

Run:

```powershell
python -m unittest tests.test_resolve_plan tests.test_repository_contract -v
```

Expected: failure because the new release is not registered and the workflow lacks its help text.

- [ ] **Step 3: Add the immutable release definition**

Create the release profile with source lock `gki.android16-6.12.2025-12-r1`, state `source-locked`, base `6.12.58`, and suffix prefix `-RN4`.

Add this exact source lock:

```json
{
  "id": "gki.android16-6.12.2025-12-r1",
  "family_id": "android16-6.12",
  "release_id": "android16-6.12-2025-12-r1",
  "source_mode": "manifest-superproject-v1",
  "manifest": {
    "url": "https://android.googlesource.com/kernel/manifest",
    "commit": "cedd514499f1d411a1b42326dbffdb6b164f0f01",
    "file": "default.xml",
    "sha256": "826ce00239dfc3e049ab53c42fcec79c6c3127e7b3a49807c0fe00a226a6ae41"
  },
  "superproject": {
    "url": "https://android.googlesource.com/kernel/superproject",
    "commit": "c516b6c3e5b60a0c6f54715cf229a34d64682894",
    "manifest_ref": "refs/heads/common-android16-6.12-2025-12"
  },
  "materialization": {
    "expected_project_count": 53,
    "required_paths": ["build/kernel", "common"]
  },
  "common": {
    "path": "common",
    "commit": "67fe3c9df146f5752b3cd5c69c8e0460221a8018"
  },
  "version": {"expected_base_release": "6.12.58"}
}
```

Insert the release beside the current Android 16 / 6.12 snapshot in both registry and workflow. Replace mojibake workflow descriptions with readable Chinese while preserving the input semantics.

- [ ] **Step 4: Permit multiple locked releases per complete family**

Change `ensure_registry_graph()` so it still requires the exact complete family set but no longer requires `len(releases) == len(families)`. Release ID uniqueness, profile uniqueness, source-lock equality, and per-release deterministic-plan checks remain enforced by existing code.

- [ ] **Step 5: Run focused tests and static validation**

Run:

```powershell
python -m unittest tests.test_resolve_plan tests.test_repository_contract -v
python scripts/validate_repository.py
```

Expected: PASS.

- [ ] **Step 6: Commit the release snapshot**

```powershell
git add -- profiles/registry.json profiles/releases/android16-6.12-2025-12-r1.json locks/sources.lock.json .github/workflows/build.yml scripts/validate_repository.py tests/test_resolve_plan.py tests/test_repository_contract.py
git commit -m "feat: add locked Android 16 6.12.58 release"
```

---

### Task 2: Repair `SET_INIT_PGRP` in every provider checkout

**Files:**
- Modify: `scripts/apply_root_adapter.py`
- Modify: `tests/test_apply_root_adapter.py`

**Interfaces:**
- Consumes: exact provider checkout at `<kernel-workspace>/KernelSU/kernel/supercall/dispatch.c`.
- Produces: `repair_init_pgrp(dispatch: Path) -> str`, returning adapter ID `pid1-init-pgrp-v1`, plus the same ID in `renebula-root-record.json`.

- [ ] **Step 1: Write failing adapter tests**

Extend the fake provider checkout with the currently locked upstream function and test all three providers across built-in and LKM variants. Assert that the transformed source:

```python
self.assertNotIn("task_pgrp(&init_task)", dispatch)
self.assertIn("find_pid_ns(1, &init_pid_ns)", dispatch)
self.assertIn("get_pid_task(init_pid, PIDTYPE_PID)", dispatch)
self.assertIn("put_task_struct(init)", dispatch)
self.assertIn("put_pid(init_pid)", dispatch)
self.assertEqual(record["compatibility_adapters"], ["pid1-init-pgrp-v1"])
```

Add a drift test where one old anchor is changed and assert `AdapterError` without modifying the file.

- [ ] **Step 2: Run the focused test and verify the expected RED state**

Run:

```powershell
python -m unittest tests.test_apply_root_adapter -v
```

Expected: failure because `repair_init_pgrp()` and its provenance record do not exist.

- [ ] **Step 3: Implement one strict source transformation**

Add `#include <linux/pid.h>` once and replace exactly one complete legacy `do_set_init_pgrp()` body. The generated kernel implementation must follow this control flow:

```c
static int do_set_init_pgrp(void __user *arg)
{
    int err;
    struct pid *init_pid;
    struct pid *init_group;
    struct pid *init_session;
    struct task_struct *init;
    struct task_struct *p;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
    struct pid *pids[PIDTYPE_MAX] = { 0 };
#endif

    rcu_read_lock();
    init_pid = get_pid(find_pid_ns(1, &init_pid_ns));
    rcu_read_unlock();
    if (!init_pid)
        return -ESRCH;

    init = get_pid_task(init_pid, PIDTYPE_PID);
    if (!init) {
        put_pid(init_pid);
        return -ESRCH;
    }

    write_lock_irq(&tasklist_lock);
    p = current->group_leader;
    init_group = task_pgrp(init);
    init_session = task_session(init);
    err = -ESRCH;
    if (!init_group || !init_session)
        goto out;
    err = -EPERM;
    if (task_session(p) != init_session)
        goto out;
    err = 0;
    if (task_pgrp(p) != init_group) {
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
        change_pid(pids, p, PIDTYPE_PGID, init_group);
#else
        change_pid(p, PIDTYPE_PGID, init_group);
#endif
    }
out:
    write_unlock_irq(&tasklist_lock);
    put_task_struct(init);
    put_pid(init_pid);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
    free_pids(pids);
#endif
    return err;
}
```

The transformer must count the complete old function match before writing and reject anything other than one. Call it immediately after validating the provider layout and before the LKM early return, ensuring both linkage variants are repaired.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m unittest tests.test_apply_root_adapter -v
python -m unittest discover -s tests -v
python scripts/validate_repository.py
git diff --check
```

Expected: every command passes.

- [ ] **Step 5: Commit the provider repair**

```powershell
git add -- scripts/apply_root_adapter.py tests/test_apply_root_adapter.py
git commit -m "fix: resolve KernelSU init process group safely"
```

---

### Task 3: Document, push, dispatch, and observe the verification build

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE-V3.md`

**Interfaces:**
- Consumes: verified repository commits from Tasks 1 and 2.
- Produces: pushed `main`, one GitHub Actions run ID, and compile results for both variants.

- [ ] **Step 1: Document snapshot semantics and the compatibility adapter**

Explain that multiple `release_id` values may share one KMI family, that each represents a full Google source snapshot rather than a numeric sublevel override, and that `SET_INIT_PGRP` is preserved through the recorded PID-1 adapter.

- [ ] **Step 2: Run final verification from a clean index**

Run:

```powershell
python -m unittest discover -s tests -v
python scripts/validate_repository.py
python scripts/resolve_plan.py --release-id android16-6.12-2025-12-r1 --root-source resukisu --susfs true --kpm false --vivo-vermagic false --uname-tag "" --output build-plan.json
python scripts/validate_repository.py --plan build-plan.json
git diff --check
git status --short
```

Remove only the generated tracked-root `build-plan.json` after validation; do not touch pre-existing untracked diagnostics.

- [ ] **Step 3: Commit documentation and push the tested commit**

```powershell
git add -- README.md docs/ARCHITECTURE-V3.md docs/superpowers/plans/2026-08-11-gki-release-snapshots-and-init-pgrp.md
git commit -m "docs: explain reproducible GKI snapshot choices"
git push origin HEAD:rework/gki-v2-clean
git push origin HEAD:main
```

- [ ] **Step 4: Dispatch exactly one manual workflow**

```powershell
gh workflow run build.yml --repo XingChenRS/ReNebulaKernel --ref main --field release_id=android16-6.12-2025-12-r1 --field root_source=resukisu --field susfs=true --field kpm=false --field vivo_vermagic=false --field uname_tag=
```

Record the resulting run ID and do not dispatch a second run unless the first exposes a repository defect that is fixed and reverified.

- [ ] **Step 5: Monitor both compile variants**

Use `gh run watch <run-id> --repo XingChenRS/ReNebulaKernel --exit-status` and inspect failed logs if necessary. Completion requires successful `builtin-image` and `lkm-module` jobs plus release verification reporting base `6.12.58`.
