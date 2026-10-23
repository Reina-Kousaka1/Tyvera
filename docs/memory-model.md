# Tyvera memory model

This document separates observed bootstrap behavior from the proposed Memory
Model V1. The proposed rules are design direction, not implemented language
features.

## Implemented reference behavior

- `Int`, `Float`, `Bool`, `String`, and `Duration` are immutable values.
- Classes, `List<T>`, and `Map<K,V>` are mutable reference values. Passing one
  through a function or storing it in another binding shares the same object.
  `let` prevents rebinding; it does not freeze the referenced object.
- Actor state is isolated. Actor messages accept the types described in the
  [language reference](language.md#actors). Transferable value payloads are
  copied; an `ActorRef` continues to identify the same actor.
- Tasks share references captured in their arguments. Tasks are serial and
  cooperative, so they do not race in parallel, but their interleaving can
  make shared mutation order observable.
- The interpreter's Python garbage collector manages its own implementation
  objects. That allocation strategy is not a Tyvera guarantee. Tyvera has no
  deterministic file, stream, socket, or native-handle cleanup yet.

The bootstrap currently relies on Python dictionary behavior for map storage
and hashing. The type checker does not yet express a complete `Map` keyability
constraint, so some statically accepted key types can still fail at runtime.
This limitation must be removed before maps can define a portable native ABI.

## Proposed Memory Model V1

V1 aims for simple value behavior plus explicit identity and ownership for
mutable state. It does not require a general borrow checker.

### Values and references

| Category | V1 direction | Status |
| --- | --- | --- |
| `Int`, `Float`, `Bool`, `Duration` | Immutable scalar values; assignment copies the value. | Core behavior implemented; numeric limits are specified. |
| `String` | Immutable Unicode scalar sequence; equality compares exact sequence, without normalization. | Text operations are implemented; a full encoding/indexing contract is still open. |
| `Json`, `Option<T>`, `Result<T,E>` | Immutable tagged values; equality is structural, and each payload follows its own value or reference rule. | Types exist; complete equality semantics are not frozen. |
| Class instances, lists, maps | Mutable references with stable object identity. Assignment copies the reference; fields and collection contents remain shared. | Implemented for the current object/collection slice. |
| Actor references, tasks, reply handles, timers, supervisors | Opaque identity handles. Copying a handle does not copy the actor, task, or operation it names. | Implemented in the interpreter; public runtime ABI is planned. |
| Resource handles | Affine owners: one live owner, explicit transfer, deterministic release. | Planned; no resource type or syntax exists. |

For ordinary references, `==` compares identity. Value equality applies to
scalars and should compare immutable tagged values structurally. The exact
equality and hashing rules for `Option`, `Result`, `Json`, and `Map` keys remain
open where the bootstrap currently inherits host behavior; they must be
specified and tested before a native backend relies on them. Tyvera will not
expose Python's `id()`, `hash()`, or object layout.

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

Resource handles should be move-only. A move transfers the sole close
responsibility and makes the prior binding unusable. Ordinary values and
ordinary references keep their current copy/reference behavior; Tyvera will
not make the whole language affine.

The resource-scope contract is:

1. Acquisition creates one owning handle.
2. Scope exit closes still-owned handles in reverse acquisition order.
3. Cleanup also runs on return, runtime failure, task failure, cancellation,
   and unwinding from a nested block.
4. Explicit `close()`/`dispose()` releases the handle once; scope cleanup then
   observes that it is already closed.
5. Cleanup does not suspend. If cleanup fails during an existing failure, the
   original failure remains primary and cleanup failures are retained as
   secondary diagnostics.
6. A resource cannot cross actor boundaries unless its type defines a safe,
   explicit transfer operation.

The surface syntax for scoped acquisition is not chosen. An owner scope should
work for files, streams, sockets, native handles, and memory regions without
introducing general-purpose raw `malloc`/`free` operations. Borrowing is
limited to temporary non-owning access within the owner's lifetime; no Rust-
style lifetime parameter system is part of V1's current plan.

### Lifetimes and runtime ownership

Ordinary heap values remain reachable while Tyvera references them; the
collector or allocator is a runtime implementation choice. Task, actor, and
resource lifetimes instead have explicit owners and shutdown rules. The
implemented task and actor ownership rules are in the
[runtime document](runtime.md). Resource lifetimes and deterministic cleanup
are planned and must be added to that ownership tree before I/O expands.
