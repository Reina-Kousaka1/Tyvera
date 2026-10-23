# Contributing to Tyvera

Tyvera is an independent, statically typed language. Its design combines
Java-inspired structure, Elixir-inspired concurrency and fault isolation, and
C++-inspired performance and controlled native access. Python is the current
bootstrap implementation host; it does not define Tyvera's syntax or runtime
semantics. Contributions should preserve that boundary.

## Set up the repository

Requirements: Python 3.10 or newer. The bootstrap has no third-party runtime
dependencies.

```sh
git clone https://github.com/Reina-Kousaka1/Tyvera.git
cd Tyvera
python -m pip install -e .
```

The default and current development branch is `Dev`. Do not rename it or assume
that a separate `main` branch exists.

## Run the local quality checks

From the repository root:

```sh
python -m unittest discover -v
tyvera --version
tyvera check hello.tyv
tyvera run hello.tyv
```

Check and run every checked-in example:

```sh
for source in hello.tyv examples/*.tyv; do
    tyvera check "$source"
    tyvera run "$source"
done
```

The CI workflow also creates a project in a temporary directory with
`tyvera new`, then checks and runs it. No generated project is committed.

## Branch and pull request workflow

1. Start a short-lived feature or fix branch from `Dev`.
2. Make a focused change and run the relevant checks above.
3. Open a pull request targeting `Dev`; explain the problem, design choice,
   affected compiler/runtime stages, tests, documentation, and security impact.
4. Let the `CI` checks pass and resolve review conversations.
5. Merge through GitHub review. Avoid direct pushes to `Dev` for normal changes.

The repository currently has only the `Dev` branch. The CI workflow runs for
pull requests targeting `Dev` and pushes to `Dev`. See
[`docs/github-governance.md`](docs/github-governance.md) for recommended manual
branch protection settings.

Commit prefixes are a recommendation, not a gate: `feat:`, `fix:`, `docs:`,
`test:`, or `ci:` followed by a concise description. Releases and package
publishing are not automated by this repository.

## Language and architecture changes

Keep the existing boundaries between lexer, parser, AST, typechecker,
interpreter/runtime, CLI, and diagnostics. When a change affects language
semantics, consider each relevant stage and update the tests and language
specification in the same pull request:

- **Lexer/parser/AST:** syntax, tokens, grammar, source locations, and a
  Tyvera-owned AST representation.
- **Typechecker:** static constraints, scope, assignability, error paths, and
  useful source-located diagnostics.
- **Interpreter/runtime:** behavior consistent with the specified semantics;
  do not expose Python classes, conversions, exceptions, or concurrency as
  Tyvera behavior.
- **Tests:** positive and negative cases, regression coverage, and CLI behavior
  when the public interface changes. Keep existing tests meaningful.
- **Specification/docs:** describe only features that are implemented and
  tested. Update examples if their behavior or syntax changes.

Not every change touches every stage. State why a stage is unaffected when
that helps reviewers follow the design. New language features should account
for multi-file projects, static diagnostics, and the future native backend
without introducing an unimplemented feature claim.

## Security, secrets, and license

Never commit `.tyvera.secrets`, tokens, credentials, or other private values.
Use `.tyvera.secrets.example` only with placeholders. Do not put secret values
in issues, pull requests, tests, examples, diagnostics, or logs. See
[`SECURITY.md`](SECURITY.md) for vulnerability reporting.

Tyvera is distributed under **GNU GPLv3**. Preserve the existing `LICENSE` file
and notices. By submitting a contribution, you confirm it may be distributed
under the repository's GPLv3 terms; do not add code or assets with incompatible
license terms.
