# Changelog

Notable changes will be recorded here, grouped under `Unreleased` while they
are being prepared and under dated version headings after a release.

## Unreleased

- Completed explicit one-shot runtime shutdown, nested task ownership and
  joins, actor-owned task cancellation, detached-task failure reporting, and
  actor reply fairness in the cooperative scheduler.
- Added the `CheckedProgram` and `ExecutionBackend` boundary while keeping the
  Python reference interpreter as the only implemented backend.
- Documented runtime semantics, the proposed Memory Model V1, the planned
  runtime ABI, target platforms, native backend options, and `tyvera build`
  acceptance criteria.

