# GitHub repository governance

## Current branch and pull request path

The repository's current default and only development branch is `Dev`. Keep it
as the active target; do not introduce or rename a branch by convention alone.
The intended contribution path is:

1. Create a short-lived feature or fix branch from `Dev`.
2. Open a pull request targeting `Dev`.
3. Require the `CI` workflow to pass on the pull request.
4. Review the change and resolve all conversations.
5. Merge the reviewed pull request into `Dev`.

CI also runs on pushes to `Dev` and can be started manually. It does not publish
packages or create releases.

## Recommended manual branch protection

These controls must be enabled in GitHub repository settings; a workflow or
documentation file cannot enforce them. Protect `Dev` with:

- Require a pull request before merging.
- Require both CI checks: `CI / test (Python 3.10)` and
  `CI / test (Python 3.13)`.
- Require the branch to be up to date with `Dev` before merging, if the
  resulting review/CI cycle is practical for the repository.
- Require at least one approval once external contributions are being
  accepted and an independent reviewer is available.
- Require all review conversations to be resolved.
- Disable force pushes and branch deletion.
- Apply the rules to administrators where practical, so direct pushes do not
  bypass the pull request path.

As of 2026-10-23, GitHub reports `Dev` as unprotected, with no required status
checks configured. These settings were not changed by this repository update.
After the CI workflow has run once, enable the rules above in GitHub settings
and select both named matrix checks.
