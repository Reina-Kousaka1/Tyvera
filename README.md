# Tyvera

Tyvera is being rebuilt **from zero** as an independent programming language.

## New design influences

- **B:** minimal, systems-oriented foundations
- **ABC:** readability and expressive collections
- **Modula-3:** static types, modules, interfaces and contracts
- **Go:** typed channels, lightweight concurrency and simple tooling

The language will use managed memory by default, with explicitly delimited low-level capabilities. The exact grammar, runtime, collector, and backend are **not implemented yet**.

## Status

**Ground-zero reboot.** This branch intentionally contains no legacy parser, compiler, interpreter, runtime, tests, or generated code. New components must be designed and implemented from scratch, with tests written for the new specification.

The prior implementation is retained in Git history and other existing branches, not in this branch's source tree.

## Repository layout

- `docs/design.md`: initial language charter and open decisions
- `LICENSE`: GPLv3

Do not treat design examples as executable Tyvera until a tested implementation exists.
