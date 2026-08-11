# GKI Release Snapshots and `SET_INIT_PGRP` Repair Design

## Goal

Build a reproducible Android 16 / 6.12.58 test kernel for Vivo PD2502 while preserving the `SET_INIT_PGRP` feature and removing its observed kernel-panic path.

## Release selection

The public workflow continues to expose one `release_id` choice. It does not accept a free-form Linux sublevel because `6.12.58` alone does not identify a reproducible GKI source tree.

Add a locked `android16-6.12-2025-12-r1` release beside the current `android16-6.12-lts-2026-08-03` release. The workflow description must show the human-readable base versions:

- Android 16 / 6.12.58 / 2025-12 r1: Vivo PD2502 alignment experiment.
- Android 16 / 6.12.92 / 2026-08-03: current rolling LTS snapshot.

The 6.12.58 release locks the complete Google manifest, superproject, common commit, manifest file digest, expected project count, and expected base release. The resolver and repository validator continue to reject releases whose profile, registry entry, and source lock disagree.

Vivo's suffix `g0a092cc0037a-abogki521987229-4k` does not resolve to a public ACK commit. Therefore the build is explicitly described as the closest reproducible official 6.12.58 snapshot, not as a byte-identical Vivo source reconstruction.

## `SET_INIT_PGRP` behavior

The ioctl remains available for KernelSU, SukiSU, and ReSukiSU in both built-in and LKM source trees. Its purpose remains unchanged: module stage scripts and KernelSU daemons may join the long-lived process group inherited by Android PID 1 instead of leaving a detectable process group whose original leader has exited.

The provider adapter applies one narrowly anchored compatibility repair after the exact provider checkout. The repair must:

1. Find the live Android PID 1 task in `init_pid_ns` instead of using `init_task` as though it were PID 1.
2. Hold the required task/PID references while reading the target session and process group.
3. Keep `tasklist_lock` coverage required by `task_session()`, `task_pgrp()`, and `change_pid()`.
4. Return a normal kernel error if PID 1 or its process group cannot be obtained.
5. Preserve the existing userspace fallback to `setpgid(0, 0)` on ioctl failure.
6. Fail the build adapter if upstream source anchors drift, rather than applying a fuzzy patch.

The repair is a provider compatibility adapter, not a fork-specific feature toggle. The source lock remains immutable and the generated provenance record identifies that the repair was applied.

## Verification build

After tests and static validation pass, push the implementation and manually dispatch one build with:

- Release: Android 16 / 6.12.58 / 2025-12 r1.
- Root source: ReSukiSU.
- SUSFS: enabled.
- KPM: disabled.
- Vivo vermagic: disabled.
- uname tag: empty.

The workflow continues to emit both the built-in Image and LKM module. The device test uses the built-in Image so it matches the configuration that produced the supplied pstore.

## Success criteria

Repository tests must prove that the 6.12.58 release is fully source-locked, selectable, and validated, and that all three provider layouts receive the same anchored `SET_INIT_PGRP` repair. The CI build must compile and verify its declared `6.12.58` base release.

The subsequent device pstore determines runtime results independently:

- No fault in `do_set_init_pgrp()` or `change_pid()` means the ioctl panic is repaired.
- Successful loading of `rust_binder`, `rfkill`, `cfg80211`, and Bluetooth modules means the official 6.12.58 snapshot is compatible with the retained Vivo module set.
- Continued protected-symbol or CRC errors mean sublevel alignment was necessary but insufficient, and the exact Vivo companion GKI module/source snapshot is still required.
