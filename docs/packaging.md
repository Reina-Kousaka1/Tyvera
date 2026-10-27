# Ubuntu package preparation

Tyvera has an experimental single-package Debian source layout under `debian/`.
The package installs the Python bootstrap/reference CLI as an architecture
independent package and places the repository-local Discord source package
under `/usr/share/tyvera/libraries/`. A C++17 compiler is recommended for users
who invoke `tyvera build`; it is not required to run the bootstrap interpreter.

## Current scope

- **Implemented:** Debian control/changelog/rules/copyright metadata and PEP 517
  pybuild integration. The Ubuntu 24.04 CI job is configured for deterministic
  binary/source builds and install/upgrade/reinstall/remove smoke using two
  revisions; this job has not yet run for the unpushed worktree changes.
- **Experimental:** the package job is configured for Ubuntu 24.04 x86-64 only.
  The package is `Architecture: all`; that metadata describes its Python
  payload, not native backend support on every architecture.
- **Not implemented:** public releases, signed uploads, `.deb` publication,
  package repository, Launchpad PPA, dependency registry, or migration scripts.

The bootstrap metadata version remains sourced from `pyproject.toml`. Debian
package revisions are maintained in `debian/changelog`; CI uses a second
revision of the same source solely to exercise `dpkg` upgrade behavior. A real
release must align the upstream source version and changelog, target an
explicitly supported Ubuntu series, and be reviewed before upload.

## Build and local package smoke

On Ubuntu 24.04, install `build-essential`, `debhelper`, `dh-python`,
`pybuild-plugin-pyproject`, `python3-all`, and `python3-setuptools`, then run:

```sh
dpkg-buildpackage -us -uc -b
```

The GitHub Actions package job is configured to build and install two unsigned
local binary packages to verify upgrade behavior, then verify remove/purge
preservation. It also builds an unsigned `.dsc` source-package smoke artifact
from a deterministic upstream archive. This is not a signed Debian source
upload or a public release build. Package revision numbers follow the
`upstream-version-debian-revision` form; update the upstream version from
`pyproject.toml` and the changelog together for a real release.

## Launchpad preparation boundary

No PPA address or public channel exists in this repository. Uploading later
requires a maintainer-selected Ubuntu series, a valid signer outside the
repository, a source package built from the matching upstream tarball, and an
authorized signed upload. Private keys and upload credentials must remain
outside Git and CI logs. This repository does not create accounts, sign, upload,
or publish packages.
