# Development workflow

All repository changes must be made through pull requests.

- Never write directly to `main`.
- Start from the latest `main` and create a task-specific branch.
- Keep each PR scoped to one coherent change and avoid unrelated refactors.
- Add or update regression tests for behavior changes.
- Verify the actual files on the PR branch, not only the PR description.
- Wait for required CI checks to pass before merging.
- Re-check the merged `main` files and post-merge CI.
- Create or update a release only after the merged `main` is verified.
- If a cross-repository change is required, use one PR per repository and document the dependency/order.
- Do not bypass safety boundaries, write measurement EEPROM, or introduce unbounded Bluetooth retry loops without an explicit design change and tests.
