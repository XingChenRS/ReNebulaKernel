# ReNebulaKernel

ReNebulaKernel v3 is a clean, reproducible Google GKI build system. It
materializes a locked Google source snapshot and never downloads, replaces, or
patches against Vivo kernel sources.

## KMI coverage

P0 registers every Google GKI KMI family for which the project has a complete
public source-and-build chain. A release selection is a locked snapshot inside
one of these families; it is not a floating branch name.

| Android generation | KMI family | Build backend |
|---|---|---|
| Android 12 | `android12-5.10` | legacy `build/build.sh` |
| Android 13 | `android13-5.10`, `android13-5.15` | legacy `build/build.sh` |
| Android 14 | `android14-5.15`, `android14-6.1` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 15 | `android15-6.6` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 16 | `android16-6.12` | `kleaf-defconfig-fragment-arm64-v1` |
| Android 17 | `android17-6.18` | `kleaf-defconfig-fragment-arm64-v1` |

No KMI family is a default or priority sample. The registry is intentionally
data-driven so every listed family follows the same planning, locking,
provenance, and version rules.

“Source-locked” means the manifest, superproject, source inventory, and build
adapter are pinned and independently checkable. It does **not** claim that
every release has already completed an image-verification run. Source
verification and image verification are separate recorded states.

## Selector matrix

Every registered release retains the pure-GKI baseline tuple:

- root provider: `none`
- root linkage: `none`
- hook mode: `none`
- configuration profile: `release`
- output: a normal arm64 GKI `Image`, provenance, and version diagnostics

The current admitted root tuples are deliberately narrow:

| KMI families | Root provider | Root linkage | Hook mode | Configuration profile |
|---|---|---|---|---|
| Android 12 through Android 16 / 5.10–6.12 | `resukisu` | `lkm`, `builtin` | `tracepoint` | `release`, `debug` |
| Android 17 / 6.18 | `none` | `none` | `none` | `release` only |

`resukisu` is locked to one ReSukiSU commit. Its `main` ref is recorded solely
as provenance; the adapter fetches and checks out the fixed SHA detached and
non-shallow, never follows `main` at build time. LKM builds require exactly one
`kernelsu.ko` in the resulting dist tree; built-in builds deliberately do not
perform that module check. The selector does not expose manual hooks, SUSFS,
KPM, arbitrary Kconfig switches, SakiSU, or KernelSU-Next as if they were
verified combinations; each needs an independent locked source/patch/trust
contract and image-tested compatibility matrix.

The user-facing workflow requires five static selections: `release_id`,
`root_provider`, `root_linkage`, `hook_mode`, and `config_profile`. It also offers an optional
`uname_suffix`: it must be empty or start with `-`, use only ASCII letters,
digits, `.`, `_`, and `-`, and is appended after ReNebula's managed suffix.
It is never a replacement uname or a way to provide a base kernel version.
The resolver rejects a suffix that repeats the selected Google base release,
contains unsafe characters, or would exceed the kernel UTS release limit for
the chosen tuple.

The resolver admits only an audited tuple for the selected family, then maps
it to one immutable Google source lock, root-source lock, build backend,
configuration fragment, and version contract. Later stages consume that
canonical build plan rather than reinterpreting workflow input. URLs, branch
names, shell snippets, and raw Kconfig switches are never accepted as dispatch
input.

The lock root is the Google manifest/superproject pair. Materialization derives
and verifies every manifest project against the locked superproject, then
records the exact detached commits that were actually checked out. No runtime
fallback to a branch head is allowed.

## Local checks

```text
python scripts/validate_repository.py
python -m unittest discover -s tests -v
python scripts/resolve_plan.py --release-id <registered-release-id> --root-provider none --root-linkage none --hook-mode none --config-profile release --uname-suffix=-lab1 --output build-plan.json
```

`build-plan.json` is disposable output. It records one fully resolved release
selection and checksums of the committed registry, family, release, and
source-lock inputs.

## Build and version contract

Android 12 and Android 13 5.10/5.15 use the dedicated legacy
`legacy-build-sh-arm64-v1` adapter with `common/build.config.gki.aarch64`. It
exports `LOCALVERSION=` and applies the selected version through a
post-defconfig configuration hook, so those releases use the exact
`base + ReNebula suffix` contract. Android 14 and newer use
`kleaf-defconfig-fragment-arm64-v1`. The plan selects only one allowlisted
adapter; no workflow constructs a command from user input.

`common/Makefile` remains the only source of a base release. The single
version writer adds only a suffix, for example
`-ReNebula-v3-a<generation>-<kmi>-<config-token>[-<uname-suffix>]`, through
the selected backend's controlled configuration mechanism. The optional final
part is validated by the resolver and preserved verbatim in the build plan:

```text
legacy Android 12/13: <base-release>-ReNebula-v3-a13-5.15-<config-token>[-<uname-suffix>]
Android 14+ Kleaf:    <base-release><Google-localversion>-ReNebula-v3-a14-6.1-<config-token>[-<uname-suffix>]
```

The legacy contract asserts the exact `<base-release><ReNebula-suffix>` value.
Modern Kleaf branches may produce a Google localversion segment, so Android 14+
uses `base-prefix-and-suffix` rather than making an exact final-uname claim
before image evidence. It records the complete observed release from generated
metadata and the built image's `Linux version` string. The build never edits
`scripts/setlocalversion`; this prevents a base release from being prepended
twice.

## Upstream boundary

WildKernels is tracked as an architectural and behavioral reference, not as a
vendored code source. Its root repository does not declare a license, so the
workflows and scripts here are independently written. The same rule applies to
every external adapter and patch: it needs a pinned revision and a license
review before it can be distributed.

The roadmap is described in [the V3 architecture](docs/ARCHITECTURE-V3.md).
Upstream pins, adopted design ideas, and rejected behavior are recorded in
[the upstream adoption record](docs/UPSTREAM-ADOPTION.md).

ReSukiSU is the first pure-GKI root provider. It is not an implicit SUSFS,
KPM, or manual-hook switch. KernelSU-Next is deliberately out of scope:
ReNebula will not add its adapter, profile, build switch, or compatibility
promise.
