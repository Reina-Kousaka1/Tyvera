# Host platform foundation

Tyvera's language contract stays independent of Linux, Ubuntu, CPython, and
the host C library. `tyvera/platform_runtime.py` is the bootstrap runtime's
platform boundary. It normalizes host facts and provides services without
exposing Python platform objects to Tyvera programs.

## Status

| Area | Implemented | Boundary / not yet implemented |
| --- | --- | --- |
| Host detection | `hostOS()` and `hostArchitecture()` report normalized strings in the reference runtime and native C++17 subset. Both recognize Linux, Windows, macOS, x86-64, AArch64, x86, and unknown values. | Ubuntu x86-64 is the primary CI reference. A separate Ubuntu 24.04 ARM64 lane runs the complete Python 3.13/native suite; the language does not depend on a distribution name. |
| Environment | `environment(name) -> Option<Secret<String>>` reads a syntactically valid process environment name in the reference runtime and experimental native C++17 runtime. Missing and invalid names produce `None`. | Values remain secret-wrapped and cannot be printed or concatenated. Reading a value does not reveal it; `Secret.reveal()` is explicit. The environment intrinsic does not inherit project-local `.tyvera.secrets`; `secrets.NAME` keeps its existing provider precedence. |
| Time | Runtime deadlines and restart windows use an injected monotonic clock; the reference and native runtime expose monotonic milliseconds. | Wall-clock timestamps and a public wall-clock API are not added. |
| Signals and exit | `tyvera run` and generated native C++17 executables translate SIGINT/SIGTERM into cooperative shutdown at safe points. Runtime success is 0, ordinary failure is 1, SIGINT is 130, and SIGTERM is 143. | `check`, `build`, and `new` do not install runtime signal handlers. Synchronous Tyvera statements are not preempted. A repeated signal does not bypass cleanup. Native signal integration is only exercised by Linux C++ CI. |
| Child processes | The Python runtime adapter has an owned `ChildProcess` service with argv-separated launch, `shell=False`, explicit child environment, bounded stdout/stderr capture, wait, timeout, terminate, and kill. It does not log argv or environment values in normalized errors. | This adapter is not a Tyvera language API and is not available to native programs. No shell-command string API is exposed. Process-tree termination and platform job objects are not implemented. |
| Packaging | Experimental Debian metadata builds an architecture-independent Python bootstrap package; an Ubuntu 24.04 CI job is configured to build two revisions and test install, upgrade, reinstall, remove, and purge while retaining a user project and local secret file. | CI validates the package lifecycle; it does not publish a release. No PPA, signed source upload, package repository, or standalone runtime bundle exists. Generated native executables still link the host C++ runtime. |

The checked-in [platform example](../examples/platform_runtime.tyv) exercises
host detection and secret-wrapped environment lookup. Ubuntu CI asserts the
expected architecture and runs the complete test suite on Python 3.10/3.13
for x86-64 and Python 3.13 for ARM64. Each lane compiles and executes native
C++17 programs, including local fake Gateway/REST differential tests, with
the host's GNU compiler and OpenSSL. The separate package job targets
Ubuntu 24.04 x86-64.
Windows and macOS remain host-compatible bootstrap targets where the current
Python runtime and tests work. The experimental C++17 backend has native TCP,
DNS, verified TLS, HTTP/1.1 client, and WebSocket client support on Linux. Native
network calls return a controlled unsupported-platform `Result` on other native
targets; the native compiler rejects programs using TLS, HTTP, or WebSockets
outside Linux. TLS uses OpenSSL and
requires its development package at build time plus the system OpenSSL runtime
libraries. The CI matrix explicitly exercises x86-64 and AArch64 hosts; this
is experimental host-platform coverage rather than a cross-compilation
interface or release certification. This native subset
does not make Linux-specific details part of the language.

## Native architecture boundary

Tyvera `Int` uses checked `std::int64_t`; `Byte` uses `std::uint8_t`.
Buffer lengths and offsets are checked before conversion to `std::size_t`;
no pointer is exposed as an integer. Actor and Task identities are fixed-width
counters, independent of pointer size. Runtime atomics do not assume lock-free
implementation, and mutex/condition-variable synchronization does not depend on
x86 memory ordering. Timeouts use `std::chrono::steady_clock`.

Linux socket descriptors remain private integer handles owned by RAII objects;
TLS uses the host OpenSSL build and its verified trust paths. No x86 assembly,
SIMD requirement, or platform-specific value layout is part of the language.
The ARM64 lane uses its native compiler and libraries. Target-triple selection,
foreign sysroots, cross-linking, and ARM64 Debian package lifecycle tests are
not supplied by this wave. Architecture detection alone never certifies support.

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
processes receive an explicit environment mapping; by default it is empty. The
native runtime ignores `SIGPIPE` so a disconnected network peer is handled as
an I/O failure instead of terminating the process.
Arguments are separate argv elements, never shell text. Diagnostics omit
argument and environment contents. The environment intrinsic returns only
`Secret<String>`. The existing `Secret` redaction and `.tyvera.secrets` file
protections remain in force.

No raw signal handler, process handle, Python exception, OS socket structure,
or native pointer is a Tyvera value. The native C++17 executable implements
host facts, monotonic time, cooperative signals, and secret-wrapped process
environment lookup. Child-process services remain available only through the
reference runtime adapter.

