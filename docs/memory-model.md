# Tyvera memory model

This document separates observed bootstrap behavior from the proposed Memory
Model V1. The proposed rules are design direction, not implemented language
features.

## Implemented reference behavior

- `Int`, `Float`, `Bool`, `String`, and `Duration` are immutable values.
- Classes, `List<T>`, and `Map<K,V>` are mutable reference values. Passing one
  through a function or storing it in another binding shares the same object.
  `let` prevents rebinding; it does not freeze the referenced object.
- Records, tuples, enums, and newtypes are immutable tagged values. Record and
  newtype immutability is shallow: a contained list or map remains mutable.
  `type` aliases are transparent and do not create runtime wrappers.
- Lists, maps, records, tuples, enums, newtypes, `Option`, `Result`, and `Json`
  use Tyvera-defined structural equality. Class and runtime-handle equality is
  identity-based. Mixed `Int`/`Float` equality compares the exact represented
  numeric values; Bool is a separate type.
- Map storage uses a Tyvera-defined stable hash/equality contract. Keys are
  restricted to recursively hashable values and stable identity references;
  mutable collections and values containing them cannot be keys.
- Actor state is isolated. Actor messages accept the types described in the
  [language reference](language.md#actors). Transferable value payloads are
  copied; an `ActorRef` continues to identify the same actor.
- Tasks share references captured in their arguments. Tasks are serial and
  cooperative, so they do not race in parallel, but their interleaving can
  make shared mutation order observable.
- `TextFile` is an identity resource that can only be acquired inside a
  `using` scope. The interpreter's Python garbage collector manages its own
  implementation objects; that allocation strategy is not a Tyvera guarantee.
  Deterministic ownership is implemented for text files only. Streams,
  sockets, native handles, and general move transfer remain planned.

The bootstrap's internal 64-bit hash algorithm is not exposed as a language
value. Backends may choose different hash bytes as long as equality, key
stability, and lookup behavior remain the same. A portable native Map ABI is
still future work.

## Proposed Memory Model V1

V1 aims for simple value behavior plus explicit identity and ownership for
mutable state. It does not require a general borrow checker.

### Values and references

| Category | V1 direction | Status |
| --- | --- | --- |
| `Int`, `Float`, `Bool`, `Duration` | Immutable scalar values; assignment copies the value. | Implemented; signed 64-bit and finite binary64 rules are specified. |
| `String` | Immutable Unicode scalar sequence; equality compares exact sequence, without normalization. | Implemented for source and file I/O; indexing and grapheme APIs remain open. |
| `Json`, `Option<T>`, `Result<T,E>`, enums, newtypes, tuples | Immutable tagged values with structural equality; nested references retain their own identity/value behavior. | Implemented in the bootstrap; native representation remains planned. |
| Records | Immutable nominal value records; fields compare structurally. Nested mutable references remain shared. | Implemented, including generic types, destructuring, and safe derived map keys. |
| Class instances, lists, maps | Classes use stable identity; lists and maps compare by contents. Assignment copies references; collection contents remain shared. | Implemented in the bootstrap. |
| Actor references, tasks, reply handles, timers, supervisors | Opaque identity handles. Copying a handle does not copy the actor, task, or operation it names. | Implemented in the interpreter; public runtime ABI is planned. |
| Resource handles | One live owner, explicit transfer, deterministic release. | First slice implemented for scoped `TextFile`; general moves, transfer, and other resource types remain planned. |

For mutable ordinary references, `==` compares identity. Value equality
compares scalar and immutable tagged values structurally, recursively applying
the nested value's rule. Lists and maps are mutable but intentionally compare
by current contents; they cannot be map keys. Class, actor, task, timer,
supervisor and reply identities use runtime identity tokens rather than Python
object addresses. Tyvera exposes neither Python's `id()`, `hash()`, nor object
layout. The numerical hash representation remains a bootstrap detail.

### Mutability and sharing

Binding mutability and object mutability are separate. `let` makes a binding
non-reassignable. A mutable reference held by that binding can still be changed
through its declared operations. A `var` binding permits rebinding. Class field
mutability remains controlled by the existing field rules.

Tasks may share class/list/map references while they run on the cooperative
single-threaded scheduler. This is deterministic for a fixed schedule, but it
does not promise isolation from logical interference. Actor state remains
private to its actor; actors communicate through typed messages and explicit
reply capabilities. If Tyvera later adds parallel tasks, shared mutation will
need a separately designed synchronization boundary such as an explicit
shared-state type.

No deep copy occurs implicitly. Actor message transfer is a narrow, checked
exception for immutable payloads and actor handles. Ordinary collection and
class values cannot be transferred into actor state.

### Move and resource ownership

Resource handles are move-only. A move transfers the sole close
responsibility and makes the prior binding unusable. Ordinary values and
ordinary references keep their current copy/reference behavior; Tyvera will
not make the whole language affine. The current `TextFile` slice enforces a
single owner by restricting acquisition to `using` and rejecting handle use
as a value, so general-purpose move expressions are not part of this slice.

The resource-scope contract is:

1. Acquisition creates one owning handle.
2. Scope exit closes still-owned handles in reverse acquisition order.
3. Cleanup also runs on return, runtime failure, task failure, cancellation,
   and unwinding from a nested block.
4. Explicit `close()` releases the handle once; it returns `false` on a second
   close, and scope cleanup observes that it is already closed.
5. Cleanup does not suspend. If cleanup fails during an existing failure, the
   original failure remains primary and cleanup failures are retained as
   secondary diagnostics.
6. A resource cannot cross actor boundaries unless its type defines a safe,
   explicit transfer operation.

`using name = openText(path, mode) { ... }` is the first implemented owner
scope. It works across synchronous and cooperative async code; cancellation
closes active scopes while discarding task continuations. Text operations do
not suspend. The same ownership boundary should extend to streams, sockets,
native handles, and memory regions without introducing general-purpose raw
`malloc`/`free` operations. Borrowing remains limited to temporary non-owning
access within the owner's lifetime; no Rust-style lifetime parameter system
is part of V1's current plan.

### Lifetimes and runtime ownership

Ordinary heap values remain reachable while Tyvera references them; the
collector or allocator is a runtime implementation choice. Task, actor, and
resource lifetimes instead have explicit owners and shutdown rules. The
implemented task and actor ownership rules are in the
[runtime document](runtime.md). `TextFile` follows the scope rules above;
other resource lifetimes must be added to that ownership tree before async I/O
expands.
