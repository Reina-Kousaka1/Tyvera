# Standard library reference

The standard library is still a small bootstrap core. APIs below are
implemented and tested; listed future namespaces are not importable yet.

## Implemented core collections

These generic collection types are currently available without an import:

| Type | Operations |
| --- | --- |
| `List<T>` | `List<T>()`, `append`, `insert`, `get`, `set`, `removeAt`, `contains`, `clear`, `.length`, and `for` over a snapshot of its elements. |
| `Map<K,V>` | `Map<K,V>()`, `put`, `get`, `remove`, `containsKey`, `keys`, `values`, `clear`, `.length`, and `for` over a snapshot of its keys. |

See [the language reference](language.md#core-collections-and-iteration) for
signatures, key restrictions, ordering, mutation and runtime error behavior.
Collections are mutable references even when held by a `let` binding.

## Planned namespaces

The module paths below are design targets, not current imports:

| Namespace | Planned APIs |
| --- | --- |
| `tyvera.io` | Text files, stdout/stderr, and paths with explicit error results. |
| `tyvera.json` | Typed JSON values, parsing and encoding. |
| `tyvera.time` | Duration, monotonic time and timers. |
| `tyvera.net` | Async HTTP and WebSocket clients. |
| `tyvera.secrets` | Provider-backed secret access with redaction and explicit unwrapping. |

There is no environment or secret reader, file I/O API, JSON implementation,
timer or network API in this version. Collection operations are compiler
intrinsics in the bootstrap and have not yet been moved into a source `.tyv`
library module.
