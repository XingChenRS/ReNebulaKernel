# ReNebula Provider Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four-axis root selector with one provider request that automatically builds built-in and LKM variants, supports locked SUSFS/KPM features, and offers a boolean Vivo vermagic adaptation for 5.10 through 6.1.

**Architecture:** A schema-5 planner compiles six public workflow inputs into immutable variant records. Root profiles own provider-specific configuration, feature profiles own compatibility and source locks, and build jobs consume variants without reconstructing user intent. Source mutation, configuration generation, and artifact verification remain separate scripts.

**Tech Stack:** Python 3 standard library, JSON profiles and locks, GitHub Actions YAML, Google Kleaf/legacy GKI build backends, git, Kconfig fragments.

## Global Constraints

- Keep one `workflow_dispatch` entrypoint.
- `root_source` values are exactly `none`, `kernelsu`, `sukisu`, and `resukisu`; KernelSU-Next is forbidden.
- A non-`none` root request emits both `builtin-image` and `lkm-module` variants.
- `vivo_vermagic` is boolean, stays visible for every release, and is rejected when the selected series is not 5.10, 5.15, or 6.1.
- Vivo adaptation inserts only `vivo ` before the architecture vermagic token and never rewrites UTS_RELEASE or CRC data.
- SUSFS and KPM apply only to the built-in variant; Vivo applies only to the LKM variant.
- KSU debug is fixed off; linkage, hook mode, config profile, and multi-manager are not public inputs.
- Do not trigger a real kernel compilation or GitHub Actions run in this implementation turn.
- Push the verified source changes to `origin` only after local verification.

---

### Task 1: Schema-5 request planner and catalog

**Files:**
- Modify: `profiles/registry.json`
- Modify: `profiles/families/*.json`
- Create: `profiles/root-providers/kernelsu.json`
- Create: `profiles/root-providers/sukisu.json`
- Modify: `profiles/root-providers/resukisu.json`
- Modify: `profiles/root-providers/none.json`
- Create: `profiles/features/susfs.json`
- Create: `profiles/features/kpm.json`
- Create: `profiles/features/vivo-vermagic.json`
- Delete: `profiles/config-profiles/release.json`
- Delete: `profiles/config-profiles/debug.json`
- Modify: `locks/root-sources.lock.json`
- Create: `locks/feature-sources.lock.json`
- Modify: `scripts/resolve_plan.py`
- Modify: `tests/test_resolve_plan.py`

**Interfaces:**
- Consumes: `resolve_plan(repo_root, release_id, root_source, susfs, kpm, vivo_vermagic, uname_tag)`.
- Produces: canonical schema-5 plan with `selection`, `source`, `root`, `features`, `variants`, `version`, and `definition` objects.

- [ ] **Step 1: Write failing planner tests**

```python
plan = resolve_plan.resolve_plan(
    REPO_ROOT,
    "android14-6.1-lts-2026-08-03",
    "sukisu",
    True,
    True,
    True,
    "MLXC_RENB",
)
self.assertEqual([v["id"] for v in plan["variants"]], ["builtin-image", "lkm-module"])
self.assertEqual(plan["selection"]["uname_tag"], "MLXC_RENB")
self.assertTrue(plan["features"]["vivo_vermagic"]["enabled"])
```

- [ ] **Step 2: Run planner tests and confirm RED**

Run: `python -m unittest tests.test_resolve_plan -v`

Expected: failures because the schema-5 signature, providers, features, and variants do not exist.

- [ ] **Step 3: Implement the schema-5 catalog and resolver**

```python
def resolve_plan(
    repo_root: Path,
    release_id: str,
    root_source: str,
    susfs: bool = False,
    kpm: bool = False,
    vivo_vermagic: bool = False,
    uname_tag: str = "",
) -> Dict[str, Any]:
    """Compile one public request into immutable build variants."""
```

The resolver must normalize `uname_tag` to a managed suffix, resolve all source
locks to fixed SHAs, reject features with `root_source=none`, reject Vivo on
6.6+, and emit one or two literal variant records.

- [ ] **Step 4: Run planner tests and confirm GREEN**

Run: `python -m unittest tests.test_resolve_plan -v`

Expected: all planner tests pass.

- [ ] **Step 5: Commit the planner boundary**

```text
git add profiles locks/root-sources.lock.json scripts/resolve_plan.py tests/test_resolve_plan.py
git commit -m "refactor: compile root requests into build variants"
```

### Task 2: Generic immutable root adapter

**Files:**
- Modify: `scripts/apply_root_adapter.py`
- Modify: `tests/test_apply_root_adapter.py`

**Interfaces:**
- Consumes: schema-5 plan and one `--variant-id`.
- Produces: `renebula-root-record.json` with provider, locked SHA, checkout mode, registration paths, and optional Kleaf module declaration.

- [ ] **Step 1: Write failing adapter tests for all providers**

```python
for provider in ("kernelsu", "sukisu", "resukisu"):
    record = apply_root_adapter.apply_provider(lock, workspace, "lkm-module", build_adapter)
    self.assertEqual(record["provider"], provider)
    self.assertEqual(record["commit"], lock["commit"])
    self.assertEqual(record["module_out"], "drivers/kernelsu/kernelsu.ko")
```

- [ ] **Step 2: Run adapter tests and confirm RED**

Run: `python -m unittest tests.test_apply_root_adapter -v`

Expected: failures because the adapter is ReSuki-specific and reads schema 4.

- [ ] **Step 3: Implement generic checkout and registration**

```python
def checkout_provider(lock: Dict[str, Any], kernel_workspace: Path) -> Path:
    """Fetch the exact commit, detach HEAD, and verify repository invariants."""

def apply_provider(
    lock: Dict[str, Any],
    kernel_workspace: Path,
    variant_id: str,
    build_adapter: str,
) -> Dict[str, Any]:
    """Register the selected provider without owning Kconfig policy."""
```

The implementation must preserve full `.git` history required by upstream
Kbuild files, validate exact repository URLs from the lock, and declare the
single Kleaf LKM output only for `lkm-module`.

- [ ] **Step 4: Run adapter tests and confirm GREEN**

Run: `python -m unittest tests.test_apply_root_adapter -v`

Expected: all root adapter tests pass.

- [ ] **Step 5: Commit the provider adapter**

```text
git add scripts/apply_root_adapter.py tests/test_apply_root_adapter.py
git commit -m "feat: add immutable KernelSU provider adapters"
```

### Task 3: Single-owner variant configuration compiler

**Files:**
- Create: `scripts/configure_variant.py`
- Create: `tests/test_configure_variant.py`
- Modify: `scripts/verify_release.py`
- Modify: `tests/test_verify_release.py`

**Interfaces:**
- Consumes: schema-5 plan, `variant_id`, Google `common/Makefile`, and selected backend output paths.
- Produces: exact Kconfig lines, localversion fragment or legacy post-defconfig script, and `renebula-config-record.json`.

- [ ] **Step 1: Write failing configuration tests**

```python
self.assertEqual(
    compile_lines(resukisu_plan, "lkm-module"),
    [
        "CONFIG_KSU=m",
        "CONFIG_KSU_TRACEPOINT_HOOK=y",
        "CONFIG_KSU_MULTI_MANAGER_SUPPORT=y",
        "# CONFIG_KSU_MANUAL_HOOK is not set",
        "# CONFIG_KSU_SUSFS is not set",
        "# CONFIG_KSU_DEBUG is not set",
    ],
)
self.assertIn("CONFIG_KPM=y", compile_lines(sukisu_kpm_plan, "builtin-image"))
```

- [ ] **Step 2: Run configuration tests and confirm RED**

Run: `python -m unittest tests.test_configure_variant -v`

Expected: import failure because `configure_variant.py` does not exist.

- [ ] **Step 3: Implement configuration compilation and narrow release verification**

```python
def compile_kconfig(plan: Dict[str, Any], variant_id: str) -> List[str]:
    """Return the exact provider/feature configuration for one variant."""

def write_backend_contract(
    plan: Dict[str, Any], variant_id: str, makefile: Path, output_root: Path
) -> Dict[str, str]:
    """Write localversion and Kconfig through the selected backend."""
```

`verify_release.py` must stop owning provider policy and retain only base
release parsing, final uname validation, artifact hashing, and evidence output.

- [ ] **Step 4: Run configuration and release tests and confirm GREEN**

Run: `python -m unittest tests.test_configure_variant tests.test_verify_release -v`

Expected: all configuration and release tests pass.

- [ ] **Step 5: Commit the configuration boundary**

```text
git add scripts/configure_variant.py scripts/verify_release.py tests/test_configure_variant.py tests/test_verify_release.py
git commit -m "refactor: centralize variant Kconfig compilation"
```

### Task 4: Locked SUSFS, KPM, and Vivo feature adapters

**Files:**
- Modify: `locks/feature-sources.lock.json`
- Modify: `profiles/features/susfs.json`
- Modify: `profiles/features/kpm.json`
- Modify: `profiles/features/vivo-vermagic.json`
- Create: `scripts/apply_feature_adapter.py`
- Create: `tests/test_apply_feature_adapter.py`

**Interfaces:**
- Consumes: schema-5 plan, variant id, kernel workspace, and optional built Image path.
- Produces: `renebula-feature-record.json`, deterministic source mutations, and optional KPM Image.

- [ ] **Step 1: Write failing feature tests**

```python
changed = apply_vivo_vermagic(vermagic_header)
self.assertIn('"vivo " MODULE_ARCH_VERMAGIC', changed)
self.assertEqual(changed.count('"vivo "'), 1)

with self.assertRaises(FeatureError):
    validate_feature_scope(plan_for_6_6_with_vivo, "lkm-module")
```

Tests also require SUSFS to mutate only `builtin-image`, KPM to reject 6.18,
and repeated feature application to fail or remain byte-identical as defined by
the feature profile.

- [ ] **Step 2: Run feature tests and confirm RED**

Run: `python -m unittest tests.test_apply_feature_adapter -v`

Expected: import failure because the feature adapter does not exist.

- [ ] **Step 3: Implement feature locks and adapters**

```python
def apply_vivo_vermagic(path: Path) -> Dict[str, str]:
    """Insert one build-derived Vivo token before MODULE_ARCH_VERMAGIC."""

def apply_susfs(plan: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    """Fetch exact SUSFS commit and apply the family/provider patch set."""

def build_kpm_image(
    plan: Dict[str, Any], image: Path, output: Path, work_root: Path
) -> Dict[str, Any]:
    """Build locked kpimg/kptools and patch a copy of the built Image."""
```

All git fetches use lock SHAs; patch application uses `git apply --check`
before mutation; Vivo never hardcodes a release string.

- [ ] **Step 4: Run feature tests and confirm GREEN**

Run: `python -m unittest tests.test_apply_feature_adapter -v`

Expected: all feature adapter tests pass.

- [ ] **Step 5: Commit feature supply chains**

```text
git add locks/feature-sources.lock.json profiles/features scripts/apply_feature_adapter.py tests/test_apply_feature_adapter.py
git commit -m "feat: add locked SUSFS KPM and Vivo adapters"
```

### Task 5: One workflow with automatic variant matrix

**Files:**
- Modify: `.github/workflows/dispatch.yml`
- Modify: `.github/workflows/resolve-plan.yml`
- Modify: `.github/workflows/verify.yml`
- Modify: `.github/workflows/build.yml`
- Modify: `tests/test_repository_contract.py`
- Modify: `scripts/validate_repository.py`

**Interfaces:**
- Consumes: the six workflow inputs and verified schema-5 plan envelope.
- Produces: one baseline job or parallel `builtin-image` and `lkm-module` jobs with separate artifacts.

- [ ] **Step 1: Write failing workflow contract tests**

```python
self.assertEqual(
    workflow_inputs,
    ["release_id", "root_source", "susfs", "kpm", "vivo_vermagic", "uname_tag"],
)
self.assertEqual(root_options, ["none", "kernelsu", "sukisu", "resukisu"])
self.assertNotIn("root_linkage", workflow_inputs)
self.assertNotIn("hook_mode", workflow_inputs)
```

The test must parse YAML and exercise a real resolved plan rather than grep
source lines.

- [ ] **Step 2: Run repository contract tests and confirm RED**

Run: `python -m unittest tests.test_repository_contract -v`

Expected: failures because the old workflow still exposes five selector axes.

- [ ] **Step 3: Implement the dispatch and build matrix**

```yaml
strategy:
  fail-fast: false
  matrix:
    variant: ${{ fromJSON(needs.prepare.outputs.variants) }}
```

Use valid defaults rather than sentinel defaults. Every description must be
plain Chinese and state scope, outputs, and rejection conditions. The build
workflow must consume the verified plan output from the verify job, not the
unverified resolve output.

- [ ] **Step 4: Run workflow contracts and repository validator**

Run: `python -m unittest tests.test_repository_contract -v`

Run: `python scripts/validate_repository.py`

Expected: both commands exit 0.

- [ ] **Step 5: Commit the workflow contract**

```text
git add .github/workflows scripts/validate_repository.py tests/test_repository_contract.py
git commit -m "feat: build provider variants from one workflow request"
```

### Task 6: Documentation, full verification, and push

**Files:**
- Modify: `README.md`
- Replace: `docs/ARCHITECTURE-V3.md`
- Modify: `docs/UPSTREAM-ADOPTION.md`

**Interfaces:**
- Consumes: final schema, workflow, locks, and verification commands.
- Produces: accurate user-facing option documentation and a pushed branch.

- [ ] **Step 1: Update documentation to match the implemented contract**

Document every public input, automatic variant mapping, provider/feature
matrix, Vivo sample evidence, immutable upstream pins, unsupported 6.18
feature boundaries, and the fact that local verification is not Image evidence.

- [ ] **Step 2: Run the complete local verification suite**

Run: `python -m unittest discover -s tests -v`

Run: `python scripts/validate_repository.py`

Run: `python -c "import json,pathlib; [json.loads(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').glob('**/*.json')]"`

Run: parse all four workflow YAML files with the available YAML parser.

Run: `git diff --check`

Expected: every command exits 0; no real kernel build or workflow dispatch is run.

- [ ] **Step 3: Review the final diff and commit documentation**

```text
git add README.md docs
git commit -m "docs: define the provider and feature build contract"
```

- [ ] **Step 4: Verify branch state and push**

Run: `git status --short --branch`

Run: `git log --oneline --decorate -8`

Run: `git push origin HEAD:rework/gki-v2-clean`

Expected: push succeeds without triggering `workflow_dispatch`; no workflow is manually started.
