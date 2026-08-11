# Vivo kernel vermagic design

## Problem

The public `vivo_vermagic` switch currently changes only the generated
`kernelsu.ko`.  The built-in Image retains the standard GKI vermagic contract,
so Vivo devices select the parallel `6.1-gki` module set instead of the OEM
module set whose vermagic contains the independent `vivo` token.

Build-ID checks on the test device confirmed that loaded `zram`, `zsmalloc`,
`kheaders`, and `rtl8150` modules came from the standard GKI directory even
though the workflow request enabled `vivo_vermagic`.

## Contract

- Keep one public boolean switch.
- Keep the switch visible for every release, but reject `true` outside kernel
  series 5.10, 5.15, and 6.1 before source synchronization.
- When enabled, both automatically generated variants carry the feature:
  - `builtin-image`: insert exactly one `"vivo "` token immediately before
    `MODULE_ARCH_VERMAGIC` in `common/include/linux/vermagic.h` before the
    kernel build.
  - `lkm-module`: keep inserting exactly one `vivo` token before `aarch64` in
    the built module's `.modinfo` vermagic.
- Do not hard-code a release string.  The kernel build remains the source of
  the UTS release and all other vermagic fields.
- Fail closed on a missing or duplicated header anchor and record the applied
  strategy in the feature provenance record.

## Expected result

The built-in Image and its separately built KernelSU LKM agree on the Vivo
vermagic suffix while preserving KMI/modversion checks.  Device-side module
selection still requires a flash test; compilation alone does not prove that
the OEM loader selected the Vivo module directory.
