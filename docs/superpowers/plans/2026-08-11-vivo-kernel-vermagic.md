# Vivo kernel vermagic implementation plan

1. Change resolver tests so a supported `vivo_vermagic=true` request projects
   the feature into both `builtin-image` and `lkm-module`; retain 6.6+ rejection.
2. Add an adapter integration test proving built-in preparation patches the
   real kernel header path and records a kernel-header strategy, while the LKM
   variant remains a deferred `.modinfo` operation.
3. Implement the smallest resolver and feature-adapter changes needed to pass
   those tests.
4. Update the single workflow's help text and repository documentation to
   describe the shared switch accurately.
5. Run targeted tests, all unit tests, repository validation, Python syntax,
   workflow YAML parsing, and whitespace checks.
6. Commit, fast-forward the maintained branch, push it to `main`, then dispatch
   and monitor the matching Android 14 / 6.1.175 Vivo build.
