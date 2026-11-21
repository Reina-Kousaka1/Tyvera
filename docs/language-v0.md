# Tyvera language specification (front-end draft 0.1)

This document defines the source syntax accepted by the first Tyvera front end.
It is deliberately small enough to implement and test before type checking or
execution exists. A successful `tyvera parse` means only that the source is
lexically and grammatically valid.

## Source files and modules

Source is UTF-8. A file starts with one module declaration:

~~~tyvera
module geometry;
import "std/math" as math;
~~~

Module names and all identifiers use ASCII letters or `_` initially, followed
by ASCII letters, digits, or `_`. The `pub` modifier marks declarations that
may be exported by a module. Imports use a quoted path and optional alias.

## Declarations

~~~tyvera
pub const tau: Float = 6.28318;
type PointList = [Point];

pub struct Point {
    x: Float,
    y: Float,
}

pub enum Shape {
    Circle(Float),
    Rectangle(Float, Float),
    Empty,
}

pub fn area(shape: Shape) -> Float {
    return 0.0;
}

task fn worker(input: chan<Int>) -> Task<Int> {
    return await receive(input);
}
~~~

Functions without a result annotation return `Unit`. `task fn` declares an
asynchronous task function. `Task<T>` and `chan<T>` are built-in type forms;
their runtime semantics are specified in the execution-model design and will
be implemented after the front end.

## Statements

~~~tyvera
let immutable: Int = 1;
var mutable: Int = immutable;
mutable = mutable + 1;

if mutable > 1 {
    return mutable;
} else {
    return 0;
}

while mutable < 10 {
    mutable = mutable + 1;
}
~~~

Bindings require an initializer. A type annotation is optional when a later
type checker can infer the type. `let` is immutable; `var` is mutable. Return,
binding, and expression statements end with `;`. Blocks use braces.

## Types

The initial built-in type names are `Int`, `Float`, `Bool`, `String`, and
`Unit`. Structs and enums are nominal types. The front end also accepts:

~~~text
[T]                 array of T
chan<T>             channel carrying T
Task<T>             task that produces T
?T                  optional T
Map<String, Int>    named generic type application
fn(Int, String) -> Bool
~~~

Generic declaration parameters, interfaces, type inference, and type checking
are future front-end stages. The parser records type syntax without claiming
that a referenced type exists or that a program is well-typed.

## Expressions

Primary expressions are names, integer/float/string/boolean/nil literals,
parenthesized expressions, and array literals. Postfix expressions support
calls, field access, and indexing. Prefix expressions support `-`, `!`,
`spawn`, and `await`. Assignment is an expression syntactically; the type
checker will restrict its left side to assignable places.

Operators, from weakest to strongest:

| Operators | Associativity |
|---|---|
| `=` | right |
| `||` | left |
| `&&` | left |
| `==` `!=` `<` `<=` `>` `>=` | left |
| `+` `-` | left |
| `*` `/` `%` | left |
| prefix `-` `!` `spawn` `await` | right |
| call `(...)`, field `.name`, index `[...]` | left |

## Lexical rules

Whitespace is insignificant between tokens. `//` begins a line comment and
`/* ... */` a non-nested block comment. Numeric separators use `_`; decimal
fractions and `e`/`E` exponents are supported. Strings use double quotes and
the escapes `\n`, `\r`, `\t`, `\0`, `\"`, and `\\`. Newlines are not
allowed inside a string literal.

## Initial semantic commitments

The language is statically typed, nominal by default, and has no implicit
numeric narrowing. `nil` is valid only for optional/reference-compatible
types. Overload resolution, generic constraints, and exhaustive enum matching
are deferred until those features are implemented.

The current checker validates built-in and declared types, aliases, function
signatures and calls, struct field access, enum constructors, local bindings,
assignments, operators, arrays, and return paths in a single module. It does
not resolve imports or generic declarations. The parser accepts generic type
application syntax, but `tyvera check` reports it as unsupported until generic
declarations and their type rules exist.

The current checker also reserves the prelude function `print(String) ->
Unit`. This is a type-checking intrinsic only; no runtime implementation is
available yet.

Memory is hybrid by policy:

- Scalar values are ordinary stack/register values.
- Safe heap objects use a precise, non-moving, generational tracing collector.
- OS resources and explicitly linear values are released deterministically by
  scoped ownership, not by waiting for collection.
- Raw pointers, manual allocation, pinning, and foreign calls require an
  explicit `unsafe` boundary and are excluded from ordinary safe code.

Concurrency is task-based and structured by default. `spawn` creates a child
task, `await` joins its typed result, and child cancellation follows its owning
scope. Typed channels transfer values between tasks; channel close and
cancelled-send/receive behavior will be specified before those operations are
executable. The first runtime target is a cooperative scheduler; parallel
execution requires a later memory-model revision.

The memory and concurrency rules above are design commitments, not implemented
guarantees in front-end draft 0.1. The current tool can parse and type-check a
subset of one module, but cannot generate or execute a program.
