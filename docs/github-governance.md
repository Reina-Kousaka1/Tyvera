# GitHub repository governance

## Contribution path

`Dev` is the repository's current integration branch. Use a short-lived feature
or fix branch based on `Dev`, then open a pull request targeting `Dev`:

1. The contributor describes the change and runs the relevant local checks.
2. GitHub Actions runs the `CI` workflow on the pull request.
3. A maintainer reviews the implementation, tests, and documentation; all
   review conversations should be resolved.
4. A repository owner or maintainer merges after required checks and review
   pass.
5. Delete a topic branch only when its work is complete or intentionally
   abandoned. Do not force-push protected branches.

The workflow also runs on pushes to `Dev` and can be started manually. It has
read-only repository permissions and does not publish packages or create
releases. See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for local commands and
language-change expectations.

## Language proposals

Use a Language Proposal / RFC for a substantial change to syntax, semantics,
type-system rules, runtime/concurrency behavior, or the compiler/backend
boundary, especially when several areas must agree before implementation.
Smaller library, tooling, and documentation ideas can use their regular issue
forms. Filing an RFC requests design discussion; it does not imply acceptance.

## Recommended manual branch protection

These controls must be set in GitHub repository settings; files in the
repository cannot enable them. Protect `Dev` with:

- Pull requests required before merge.
- Both matrix checks required: `CI / test (Python 3.10)` and
  `CI / test (Python 3.13)`.
- Require the branch to be up to date with `Dev` before merging when practical.
- Require at least one approval when an independent reviewer is available,
  particularly for external contributions.
- Require all review conversations to be resolved.
- Disable force pushes and branch deletion on the protected branch.
- Apply rules to administrators where practical.

Confirm the current protection state and check names in GitHub settings before
relying on these recommendations; this document does not change repository
settings.

## Recommended issue and pull request labels

The following vocabulary helps route work without imposing a label on every
change. These are recommendations only; they do not claim that the labels have
been created in GitHub. Keep existing repository labels and add only missing
ones when a maintainer can manage labels directly.

| Label | Use |
| --- | --- |
| `bug` | Reproducible incorrect behavior |
| `enhancement` | General improvement request |
| `language` | Syntax or language-semantics design |
| `runtime` | Interpreter, actor, task, or scheduler behavior |
| `type-system` | Type checking, inference, or diagnostics rules |
| `stdlib` | Standard-library API or behavior |
| `toolchain` | CLI, project model, formatting, or test tooling |
| `compiler` | Lexer, parser, AST, semantic analysis, or backend architecture |
| `documentation` | README, specification, guides, and reference material |
| `performance` | Measured performance work or regression |
| `security` | Security-sensitive report or hardening work; use private reporting for vulnerabilities |
| `breaking-change` | A change that intentionally alters existing source or API compatibility |
| `good first issue` | Bounded work suitable for a first contribution |
| `help wanted` | Work where maintainers welcome contributor assistance |

## Releases and versions

Tyvera is pre-1.0, and release publishing is manual and not configured by CI.
See [`release-process.md`](release-process.md) for the intended versioning
policy and [`../CHANGELOG.md`](../CHANGELOG.md) for unreleased notes.
