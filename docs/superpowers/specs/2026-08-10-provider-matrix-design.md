# ReNebula Provider Matrix Design

## Outcome

ReNebula exposes one manual GitHub Actions workflow with six user-facing inputs:

1. `release_id`
2. `root_source`
3. `susfs`
4. `kpm`
5. `vivo_vermagic`
6. `uname_tag`

The workflow does not expose linkage, hook selection, debug mode, multi-manager
support, raw Kconfig, URLs, refs, or patch names. Those are implementation
details resolved from committed profiles and immutable locks.

No KernelSU family is a default implementation. `root_source` accepts exactly
`none`, `kernelsu`, `sukisu`, and `resukisu`. KernelSU-Next remains excluded.

## Input contract

| Input | Type | Contract |
|---|---|---|
| `release_id` | choice | One registered immutable Google GKI release. The list is ordered by Android generation and does not express priority. |
| `root_source` | choice | `none` builds one clean Image. A KernelSU provider builds a built-in Image and an LKM variant in parallel. |
| `susfs` | boolean | Enables the locked SUSFS source/kernel integration for the built-in Image. It never claims that an external LKM can add SUSFS to an unpatched stock kernel. |
| `kpm` | boolean | Produces a KPM-enabled built-in Image variant through the locked SukiSU KernelPatch supply chain. Provider-specific Kconfig glue is resolved internally. |
| `vivo_vermagic` | boolean | Only valid for kernel series 5.10, 5.15, and 6.1. It inserts the token `vivo` immediately before the architecture token in LKM vermagic. It does not rewrite UTS_RELEASE or bypass symbol CRC/KMI checks. |
| `uname_tag` | string | Optional ASCII tag such as `MLXC_RENB`. ReNebula adds the leading `-`, rejects base-release repetition and unsafe characters, and enforces the 64-byte UTS limit. |

GitHub Actions cannot conditionally hide one dispatch input based on another.
Therefore `vivo_vermagic` stays visible in the single workflow. The planner
rejects `true` for 6.6, 6.12, and 6.18 before source synchronization.

## Plan and variant model

The canonical request plan is schema 5. It records the six normalized inputs,
the selected Google source lock, immutable root and feature locks, definition
digests, and a list of build variants.

`root_source=none` resolves to one variant:

```text
baseline-image: linkage=none, artifact=image
```

Any selected KernelSU provider resolves to two variants:

```text
builtin-image: linkage=builtin, artifact=image
lkm-module:    linkage=lkm,     artifact=module
```

SUSFS and KPM are scoped to `builtin-image`. Vivo vermagic is scoped to
`lkm-module`. The plan records this scope explicitly; a build stage never
reinterprets a global boolean.

## Provider policy

| Provider | Built-in | LKM | Hook policy | KPM glue |
|---|---|---|---|---|
| KernelSU | `CONFIG_KSU=y` | `CONFIG_KSU=m` | Current upstream hook manager; no user hook input | External locked KernelPatch image layer |
| SukiSU-Ultra | `CONFIG_KSU=y` | `CONFIG_KSU=m` | Current upstream hook manager; no user hook input | `CONFIG_KPM=y` for built-in plus locked KernelPatch image layer |
| ReSukiSU | `CONFIG_KSU=y` | `CONFIG_KSU=m` | Tracepoint for ordinary GKI; SUSFS inline hook when SUSFS is enabled | Locked compatibility layer plus KernelPatch image layer |

Every provider is checked out at a full 40-character SHA. A branch or tag is
provenance only. The adapter verifies `HEAD`, checkout cleanliness, source
directory, Kconfig, and Kbuild before registering `drivers/kernelsu`.

## Feature policy

SUSFS is a separately locked source supply chain. Each supported KMI family
maps to the exact upstream branch commit. The feature adapter applies the
kernel patch first and then the provider-specific integration patch. Missing
patches, rejected hunks, or an unsupported provider/KMI fail before config or
build. SUSFS is initially admissible through 6.12 because the upstream has no
Android 17/6.18 branch.

KPM uses the locked `SukiSU_KernelPatch_patch` source. The built-in job builds
`kpimg` and `kptools`, patches a copy of the verified Image, and retains both
the normal built-in Image and the KPM Image. KPM is initially admissible
through 6.12, matching the upstream stated range. SukiSU additionally enables
its native `CONFIG_KPM`; other providers keep KPM as an image-level feature.

Vivo vermagic is a source transformation owned by the LKM variant. It changes
the build-derived macro from:

```text
... modversions aarch64
```

to:

```text
... modversions vivo aarch64
```

The adapter requires exactly one supported vermagic macro anchor and records
the changed file hash. The two supplied Vivo `vr.ko` samples and same-firmware
ordinary modules demonstrate that the suffix differs by only this token.

## Workflow structure

`dispatch.yml` sends only the six public inputs to `resolve-plan.yml`.
`verify.yml` verifies the canonical plan and returns the same verified plan
digest. `build.yml` derives a matrix from `plan.variants`; each matrix job gets
an independent Google source tree and output directory.

Each variant executes the same stages:

```text
restore verified plan
  -> synchronize locked Google tree
  -> materialize locked root provider
  -> apply variant-scoped source features
  -> compile exact Kconfig/localversion contract
  -> run selected Google build backend
  -> verify final config and artifacts
  -> optionally derive KPM Image
  -> upload provenance and diagnostics
```

No step follows a floating branch, accepts arbitrary shell text, or silently
falls back to another provider or feature implementation.

## Artifact contract

`none` produces a baseline Image artifact. A root provider produces:

- one built-in Image;
- exactly one `kernelsu.ko` from the LKM variant;
- a KPM Image in addition to the built-in Image when `kpm=true`;
- a `kernelsu.ko` whose vermagic contains exactly one `vivo` token when
  `vivo_vermagic=true`.

Each artifact record binds the request-plan digest, variant id, Google source
record, root source record, feature records, final config digest, observed
uname/vermagic, and artifact SHA-256.

## Verification boundary

This implementation turn runs unit tests, repository validation, JSON/YAML
parsing, shell syntax checks where available, and source-level adapter tests.
It does not dispatch or run a real kernel compilation. Real Image/LKM evidence
is created only when the user manually triggers the workflow.
