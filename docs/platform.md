# Host platform foundation

Tyvera's language contract stays independent of Linux, Ubuntu, CPython, and
the host C library. `tyvera/platform_runtime.py` is the bootstrap runtime's
platform boundary. It normalizes host facts and provides services without
exposing Python platform objects to Tyvera programs.

## Status

| Area | Implemented | Boundary / not yet implemented |
| --- | --- | --- |
| Host detection | `hostOS()` and `hostArchitecture()` report normalized strings. The Python adapter recognizes Linux, Windows, macOS, x86-64, AArch64, x86, and unknown values. | Ubuntu is the Linux CI reference; the language does not depend on a distribution name. Linux x86-64 is the required CI host. AArch64 is normalized, but this branch does not add an ARM64 runner or native-backend conformance run. |
| Environment | `environment(name) -> Option<Secret<String>>` reads a syntactically valid process environment name. Missing and invalid names produce `None`. | Values remain secret-wrapped and cannot be printed or concatenated. Reading a value does not reveal it; `Secret.reveal()` is explicit. The environment intrinsic does not inherit project-local `.tyvera.secrets`; `secrets.NAME` keeps its existing provider precedence. |
| Time | Runtime deadlines and restart windows use an injected monotonic clock; production bootstrap uses a monotonic host clock. | Wall-clock timestamps and a public wall-clock API are not added. |
| Signals and exit | `tyvera run` translates SIGINT/SIGTERM into cooperative shutdown at interpreter safe points. Exit status is 0 for success, 1 for ordinary Tyvera diagnostics, 130 for SIGINT, and 143 for SIGTERM. | `check`, `build`, and `new` do not install runtime signal handlers. Synchronous Tyvera statements are not preempted; shutdown is observed at statement, loop, task, actor, and I/O wakeup boundaries. A second signal does not bypass cleanup. |
| Child processes | The Python runtime adapter has an owned `ChildProcess` service with argv-separated launch, `shell=False`, explicit child environment, bounded stdout/stderr capture, wait, timeout, terminate, and kill. It does not log argv or environment values in normalized errors. | This adapter is not a Tyvera language API and is not available to native programs. No shell-command string API is exposed. Process-tree termination and platform job objects are not implemented. |
| Packaging | The existing `pyproject.toml`, version source, and CI install path remain the bootstrap distribution foundation. Ubuntu CI builds/tests the Python package and compiles native examples with the system C++17 compiler. | There is no `.deb`, standalone Tyvera runtime bundle, package manager, or release pipeline yet. Generated native executables still link the host C++ runtime. |

The checked-in [platform example](../examples/platform_runtime.tyv) exercises
host detection and secret-wrapped environment lookup. Ubuntu CI asserts the
Linux x86-64 host and runs the complete test suite on Python 3.10 and 3.13.
Windows and macOS remain host-compatible bootstrap targets where the current
Python runtime and tests work; this is not a native-backend support claim.

## Cooperative shutdown

The CLI installs SIGINT/SIGTERM handlers only while running the reference
interpreter. A handler records the first signal and wakes the selector without
performing application cleanup. The interpreter turns that request into a
source-located diagnostic at a safe point. Ordinary scope unwinding then
closes owned resources in reverse acquisition order, cancels tasks and timers,
stops supervisor trees from the root downward, and drains the bounded I/O
worker pool before exit. Cleanup failure is appended without replacing the
signal or its conventional exit code.

This is cooperative shutdown, not preemption. A currently executing
synchronous Tyvera call must return before the next safe point. No configurable
grace period or forced termination of arbitrary child-process trees exists.

## Security boundary

Environment values and process output are bounded at the adapter. Child
processes receive an explicit environment mapping; by default it is empty.
Arguments are separate argv elements, never shell text. Diagnostics omit
argument and environment contents. The environment intrinsic returns only
`Secret<String>`. The existing `Secret` redaction and `.tyvera.secrets` file
protections remain in force.

No raw signal handler, process handle, Python exception, OS socket structure,
or native pointer is a Tyvera value. Native platform support for these runtime
services remains future work.
