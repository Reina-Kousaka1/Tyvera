# Standard library reference

The standard library is still a small bootstrap core. APIs below are
implemented and tested; listed future namespaces are not importable yet.

## Implemented bootstrap APIs

These core types and functions are currently available without an import:

| Type | Operations |
| --- | --- |
| `List<T>` | `List<T>()`, `append`, `insert`, `get -> Option<T>`, `set`, `removeAt -> Option<T>`, `contains`, `clear`, `.length`, and `for` over a snapshot of its elements. |
| `Map<K,V>` | `Map<K,V>()`, `put`, `get -> Option<V>`, `remove -> Option<V>`, `containsKey`, `keys`, `values`, `clear`, `.length`, and `for` over a snapshot of its keys. |
| `Option<T>` | `Some(value)`, `None<T>()`, and pattern matching with `Some(value)`/`None`. |
| `Result<T,E>` | `Ok<T,E>(value)`, `Err<T,E>(error)`, and pattern matching with `Ok(value)`/`Err(error)`. |
| Text I/O | `readText`, `writeText`, `pathJoin`, `pathExists`, `println`, and `eprintln`; file errors use `Result`. |
| Secrets | `secrets.NAME` reads `.tyvera.secrets` before the process environment and returns `Option<Secret<String>>`; `Secret.reveal()` is explicit. |
| `Json` | `parseJson(String) -> Result<Json,String>`, `jsonStringify(Json) -> Result<String,String>`, typed constructors, and optional accessors for JSON values. |

See [the language reference](language.md#core-collections-and-iteration) for
collection signatures, key restrictions, ordering, mutation and runtime error
behavior, and [JSON values](language.md#json-values) for JSON signatures and
number handling.
Collections are mutable references even when held by a `let` binding. `Option`
and `Result` are implemented core sum types. `readText`/`writeText` resolve
relative paths from the project root and refuse direct access to
`.tyvera.secrets`. Secret values are redacted in diagnostics, and
`println`/`eprintln` reject `Secret<T>`.

## Planned namespaces

The module paths below are design targets, not current imports:

| Namespace | Planned APIs |
| --- | --- |
| `tyvera.time` | Duration, monotonic time and timers. |
| `tyvera.net` | Async HTTP and WebSocket clients. |
| `tyvera.secrets` | A separately importable provider-backed secret library. |

Timers and networking are not implemented. JSON, collection, I/O and secret
operations are bootstrap intrinsics rather than source `.tyv` modules, so
they are not yet imported through `tyvera.*` namespaces.
