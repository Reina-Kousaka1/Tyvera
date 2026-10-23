# Release and versioning

Tyvera is still pre-1.0. The project does not currently automate package
publishing or GitHub releases. A maintainer will decide when to tag and publish
a release; no tag or release is implied by the version in `pyproject.toml`.

The project intends to use Semantic Versioning for published releases. Before
`1.0.0`, the `0.y.z` version communicates that language and toolchain APIs are
still evolving: a minor-version release may contain breaking language or API
changes, while a patch release should be limited to compatible fixes. Release
notes should call out syntax, semantics, CLI, standard-library, and migration
changes that affect users. The project will define its stable compatibility
guarantees before declaring `1.0.0`.

Keep unreleased change notes in [`CHANGELOG.md`](../CHANGELOG.md). Do not add
release entries until changes are selected for a real release, and do not
introduce publishing credentials or release automation without a separate
reviewed change.
