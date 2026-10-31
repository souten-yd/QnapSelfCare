# Development workflow

All repository changes must be made through pull requests.

- Never write directly to `main`.
- Start from the latest `main` and create a task-specific branch.
- Keep each PR scoped to one coherent change and avoid unrelated refactors.
- Add or update regression tests for behavior changes.
- Verify the actual files on the PR branch, not only the PR description.
- Run required validation locally before merging. If GitHub branch protection requires a check, resolve that requirement explicitly rather than silently bypassing it.
- Re-check the merged `main` files against the locally validated revision.
- Create or update a release only after the merged `main` is verified.
- If a cross-repository change is required, use one PR per repository and document the dependency/order.
- Do not bypass safety boundaries, write measurement EEPROM, or introduce unbounded Bluetooth retry loops without an explicit design change and tests.

## Standing delivery instruction (2026-10-31)

- For every development task, continue through implementation, local tests/build, PR, merge, post-merge verification, and release publication by default. Do not stop at PR creation or ask again for merge/release approval.
- A newer explicit user instruction to stop at review, avoid merging, or avoid release takes precedence. If checks fail or delivery is blocked, fix within scope or report the concrete blocker; never report an unpublished release as complete.
- Keep application, QPKG, and release workflow versions aligned. Verify published release assets and report the release link.

## Local-first validation and build (2026-10-31)

- Prefer development-environment tests and QPKG builds. Do not automatically duplicate them on GitHub Actions for pushes or pull requests. Record test results, source revision, and build checksums.
- GitHub Actions is manual-only fallback. Do not dispatch it without explicit user authorization; first try to resolve local build limitations. Never omit required validation or claim an unbuilt package is released.
- Publish locally built release artifacts after verifying merged main matches the validated source. Keep the merge-and-release delivery rule for application changes.
- Documentation/workflow-only changes may be merged without rebuilding or republishing unchanged application binaries; report the existing applicable release.
