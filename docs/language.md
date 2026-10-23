# Tyvera core language reference

Tyvera is an independent, statically typed language. Java, Elixir and C++ inform
its design; they are not compatibility targets. TypeScript is not part of the
design. Python hosts the bootstrap implementation only.

## Files, declarations and statements

Source files use `.tyv`. A program can contain multiple modules connected with
explicit relative file imports. The entry module defines a global `fn main()`
returning `Void`. Functions, types and methods can use declarations that appear
later in the same module. Functions can recurse. Parameters require types and
are immutable. Return annotations use `-> Type`; an omitted annotation means
`Void`.

```tyv
fn max(a: Int, b: Int) -> Int {
    if a > b {
        return a
    } else {
        return b
    }
}

fn main() -> Void {
    var score: Int = 10
    score = max(score + 5, 12)
    println(score)
}
```

Newlines or `;` separate statements and function declarations. A final statement
before `}` needs no separator. Compound statements also need a separator before
the next independent statement. Expressions and conditions stay on one line;
the opening `{` follows the condition on that line. `else` may follow `}` on
the same line or after newlines, but not after a semicolon.

Supported declaration and statement grammar (EBNF; expression follows the
precedence below):

```text
block       = "{" [statements] "}" ;
import      = "import" string "as" identifier ;
top-level   = import | ["pub"] (function | class | interface) ;
declaration = ("let" | "var") identifier [":" type] "=" expression ;
assignment  = identifier "=" expression ;
field-set   = expression "." identifier "=" expression ;
conditional = "if" expression block ["else" (block | conditional)] ;
loop        = "while" expression block | for-loop ;
match       = "match" expression "{" {pattern "=>" (block | statement)} "}" ;
return      = "return" [expression] ;
statement   = declaration | assignment | field-set | conditional | loop | match | return
            | "break" | "continue" | block | function-call ;
for-loop    = "for" identifier "in" expression block ;
function    = "fn" identifier ["<" identifier {"," identifier} ">"] "(" [parameters] ")" ["->" type] block ;
class       = "class" identifier ["<" identifier {"," identifier} ">"] ["implements" type-list] "{" {field | method} "}" ;
field       = ("let" | "var") identifier ":" type ;
method      = function ;
interface   = "interface" identifier "{" {method-signature} "}" ;
method-signature = "fn" identifier "(" [parameters] ")" ["->" type] ;
type-list   = identifier {"," identifier} ;
parameters  = identifier ":" type {"," identifier ":" type} ;
type        = identifier {"." identifier} ["<" type {"," type} ">"] ;
pattern     = literal | "_" | identifier | ("Some" | "Ok" | "Err") "(" [identifier | "_"] ")" | "None" ;
```

Assignments are statements, not expressions. Function and method calls can be
used as expression statements. Local declarations require an initializer;
class fields declare their type without an initializer and are initialized by
the constructor.

## Lexical rules and literals

- Identifiers start with an ASCII letter or `_`, followed by ASCII letters,
  digits or `_`. Keywords cannot be identifiers.
- `//` starts a comment ending at the next newline.
- Integers are decimal digits. A leading minus is a unary operator.
- Float literals have decimal digits on both sides of `.`, e.g. `2.5`.
  Exponent notation is not supported.
- Boolean literals are `true` and `false`.
- Strings use double quotes and escapes `\n`, `\r`, `\t`, `\"`, `\\`.
  Literal newlines inside strings are not allowed.

## Primitive types

| Type | Values and restrictions |
| --- | --- |
| `Int` | Signed 64-bit integers, −9223372036854775808 through 9223372036854775807. Out-of-range literals are static errors; arithmetic overflow is a located runtime error. |
| `Float` | Finite IEEE binary64 values. Decimal literals round to binary64. Non-finite literals and arithmetic overflow are errors; ordinary binary64 rounding and underflow apply. |
| `Bool` | Exactly `true` or `false`; no truthiness conversion. |
| `String` | Text, concatenation and equality. No indexing or implicit string conversion. |
| `Void` | No return value; not allowed for bindings or parameters. No literal. |

`Unit` remains an exact compatibility alias for `Void`; signatures and
diagnostics use `Void`. Inference applies to initialized local bindings only.
Assignment, initialization, arguments and returns require exact type matches,
except that a class implementing an interface is assignable to that interface.
An `Int` cannot silently be stored in a `Float` binding. Numeric promotion is
limited to the explicit arithmetic rules below.

`println(value)` accepts one `Int`, `Float`, `Bool` or `String` and returns
`Void`. Booleans print in lowercase. Function names and local bindings occupy
separate namespaces; global calls name declared functions directly, constructors
use the class name, and instance methods use member-call syntax. `println` cannot
be redeclared or shadowed.

## Scope, bindings and mutation

Every function body, conditional branch, loop body and standalone `{ ... }`
creates a lexical scope. Parameters share the function's outer body scope.
Bindings are visible only after their declaration and in descendant scopes.
There are no global variables, closures or dynamic caller scopes.

`let` is immutable; `var` may be assigned another value of the same type.
Both may omit a type annotation to infer it from the initializer.

```tyv
let name: String = "Charlotte"
var count: Int = 0
count = count + 1
```

Duplicate names in the same scope are errors, including redeclaring a
parameter in the function's outer body. Nested shadowing is allowed, even
with another type or mutability. The initializer is checked/evaluated before
the new binding is introduced, so `let n = n + 1` in a child scope reads the
outer `n`. Assignment targets the nearest binding and requires that binding
to be mutable. Leaving the child scope restores the outer binding. Assignment
to an unshadowed outer `var` persists after the block. Loop iterations create
fresh body scopes; declarations do not leak into the next iteration.

## Classes, fields and interfaces

Classes are reference values with named fields and instance methods; `==` and
`!=` compare instances by identity. A method receives its instance through the
reserved `self` name. Fields and methods are public; there are no access
modifiers or static methods yet. Field access uses
`instance.field`, method calls use `instance.method(arguments)`, and assignment
to a field is a statement.

```tyv
class User {
    let name: String
    var score: Int

    fn init(name: String, score: Int) {
        self.name = name
        self.score = score
    }

    fn addScore(points: Int) {
        self.score = self.score + points
    }
}
```

`init` is the constructor. Constructing `User("Charlotte", 10)` calls
`init` with those arguments and produces a `User` instance. A class with fields
must declare one `init`; a class without fields may omit it, in which case
`ClassName()` constructs an empty instance. Constructors return `Void`.
Every field must be initialized on every path that can finish construction.
An immutable `let` field must be initialized exactly once on each path;
mutable `var` fields may be reassigned. Constructor field assignments through
`self` are the only way to initialize immutable fields. The checker rejects
reads of an uninitialized field and passing or using `self` before all fields
are initialized. Immutable fields cannot be initialized inside loops.

Interfaces declare method signatures, and a class can implement multiple
interfaces:

```tyv
interface Printable {
    fn print() -> Void
}

class User implements Printable {
    let name: String
    fn init(name: String) { self.name = name }
    fn print() { println(self.name) }
}

fn display(value: Printable) { value.print() }
```

The checker verifies that each required method exists and has exactly matching
parameter and return types. A class implementing an interface can be assigned
to an interface-typed binding or passed to an interface-typed parameter;
method calls dispatch to the instance's class implementation. Interfaces have
no fields, default methods or inheritance. Classes/interfaces and global
functions share one declaration namespace within each module. Module
declarations are private by default; `pub fn`, `pub class` and `pub interface`
make a declaration importable. Class fields and methods remain public once the
class itself is imported.

## Generics

Generic functions and classes use named type parameters. Calls infer parameters
from value arguments when possible; explicit type arguments are available when
inference has no input:

```tyv
fn identity<T>(value: T) -> T { return value }

class Box<T> {
    let value: T
    fn init(value: T) { self.value = value }
    fn get() -> T { return self.value }
}

fn main() {
    let number: Int = identity(42)
    let box: Box<Int> = Box(number)
    println(box.get())
}
```

Type arguments are checked recursively and cannot be `Void`. Generic types are
invariant. Generic methods, generic interfaces and type-parameter constraints
are not supported. The checker substitutes concrete types at each call; the
bootstrap interpreter erases type arguments in runtime object records, and no
runtime cast or dynamic generic dispatch is exposed.

## Core collections and iteration

`List<T>` and `Map<K,V>` are built-in, statically typed mutable reference
collections. Construct them with explicit type arguments; an empty collection
cannot infer its element types:

```tyv
fn main() {
    let names: List<String> = List<String>()
    names.append("Mira")
    names.append("Kai")
    for name in names {
        println(name)
    }
}
```

`List<T>` supports `append(T)`, `insert(Int,T)`, `get(Int) -> Option<T>`,
`set(Int,T)`, `removeAt(Int) -> Option<T>`, `contains(T) -> Bool`, `clear()`
and the read-only `.length` property. Indexes are zero-based; missing reads and
removals return `None`; invalid writes or insertions produce runtime diagnostics.

`Map<K,V>` supports `put(K,V)`, `get(K) -> Option<V>`, `remove(K) -> Option<V>`,
`containsKey(K) -> Bool`, `keys() -> List<K>`, `values() -> List<V>`,
`clear()` and read-only `.length`. Keys must be primitive values or class/
interface references. Iterating a map
visits keys in insertion order. A `for` loop evaluates its collection once and
iterates a snapshot, so mutations during the loop do not alter its current
iteration. Loop bindings are immutable and scoped to the loop body; `break`
and `continue` target the innermost loop. A `for` loop may execute zero times
and cannot by itself satisfy a non-`Void` function's return-path requirement.

These core types are available without an import. A separately packaged
`tyvera.collections` module is not implemented yet.

## Optional and result values

`Option<T>` represents either a present `Some(value)` or absent `None`; `Result<T,E>`
represents success `Ok(value)` or expected failure `Err(error)`. Their payload
types are statically checked. `Some(value)` infers `T`; `None<T>()` needs an
explicit type argument. `Ok<T,E>(value)` and `Err<T,E>(error)` use explicit
type arguments because the unused side cannot be inferred. `Void` is not a
valid payload type.

These are ordinary values. A failed `Result` is an expected application
outcome, not a runtime failure or process crash. The interpreter does not
automatically unwrap either type.

## Pattern matching

`match` is a statement; each arm has a pattern and a statement or block body.
Supported patterns are primitive literals, `_`, a binding name, and the
`Some(value)`, `None`, `Ok(value)`, `Err(error)` variants. A binding is immutable
and visible only in its arm. Literal types must exactly match the subject type.
Option/Result and Bool matches are checked for exhaustiveness; other types may
use a wildcard/binding default. A non-exhaustive match simply executes no arm
when no pattern matches. Duplicate cases and arms after exhaustive coverage
are errors.

```tyv
match response {
    Ok(value) => println(value)
    Err(message) => println(message)
}
```

This is a first sum-type slice; destructuring nested values, guards, and
message-pattern matching are not implemented.

## Text I/O, paths and secrets

The bootstrap core provides `readText(path) -> Result<String,String>`,
`writeText(path, contents) -> Result<Bool,String>`, `pathJoin(left,right) ->
String`, `pathExists(path) -> Bool`, `println` and `eprintln`. Relative file
paths resolve from the project root. I/O failures return `Err` values; file
contents and operating-system error details are not copied into the error
message. Path joining follows the host platform's path rules. The text-file
API cannot read or overwrite the project `.tyvera.secrets` file.

The built-in namespace `secrets.NAME` returns `Option<Secret<String>>`. The
local provider reads `.tyvera.secrets` first, then checks the process
environment. The local file uses one `NAME = "JSON-escaped string"` entry per
line; malformed files produce a generic, value-redacted runtime diagnostic.
The file is gitignored; `.tyvera.secrets.example` may be committed. `Secret<T>`
cannot be passed to `println`/`eprintln`, concatenated with strings, or
implicitly converted. The explicit `secret.reveal()` method returns the value
as `T`; applications should pass the wrapper to trusted APIs and only reveal
it when needed. Secret values are redacted from the wrapper's representation
and are never included in provider diagnostics. External secret stores are not
implemented; the provider boundary is the extension point.

## Modules and projects

Each file is one module. Imports name a `.tyv` file relative to the importing
file and bind it to an alias:

```tyv
import "models/user.tyv" as models

fn main() {
    let user: models.User = models.User("Charlotte")
    println(user.name)
}
```

Calls and construction use `alias.symbol(...)`; types use `alias.Type`.
Unqualified names resolve only inside the current module. Imported symbols
must be explicitly public. Import aliases are unique and reserved against
local variables and parameters in that module. Imports are linked before type
checking; duplicate/missing symbols, missing files, root-escaping paths and
circular imports produce source-located diagnostics. A project can declare
`name` and `entry` string keys in a minimal `tyvera.toml`; there is no package
registry or dependency manager yet.

## Operators

Precedence, lowest first; binary operators associate left:

| Precedence | Operators |
| --- | --- |
| 1 | `||` (`or` compatibility spelling) |
| 2 | `&&` (`and` compatibility spelling) |
| 3 | `==`, `!=` |
| 4 | `<`, `<=`, `>`, `>=` |
| 5 | `+`, `-` |
| 6 | `*`, `/` |
| 7 | Prefix `-`, `!` |

Parentheses override precedence. Calls bind above prefix operators. Binary
operands and function arguments evaluate left to right.

| Operator | Accepted operands | Result |
| --- | --- | --- |
| `+`, `-`, `*` | `Int`/`Int` | checked `Int` |
| `+`, `-`, `*` | numeric pair containing `Float` | `Float` (integer operand promoted to binary64) |
| `/` | any numeric pair | `Float`, including `Int / Int`; zero divisor is a runtime error |
| `+` | `String`/`String` | `String` concatenation |
| `==`, `!=` | same non-`Void` types, or a numeric pair | `Bool`; numeric comparison compares values, not their type names |
| `<`, `<=`, `>`, `>=` | numeric pair only | `Bool` |
| `&&`, `||` | `Bool`/`Bool` only | `Bool`, short-circuit |
| Prefix `!` | `Bool` | `Bool` |
| Prefix `-` | `Int` or `Float` | same type, with overflow checking |

Mixed numeric equality/order compare numeric values without first rounding an
`Int` to binary64; e.g. an integer beyond binary64's exact range is not equal
to a different rounded Float. Strings compare exact text; Bool is never a
number. No other operator combinations or conversions are permitted.
Both sides of a short-circuit expression are still statically checked.

## Conditions, loops and returns

`if`, `else if`, `else` and `while` use braced bodies. Conditions must have
type `Bool`; `if 42 { ... }` is a static error. `while` checks its condition
before every iteration. `break` exits the innermost loop; `continue` starts its
next iteration. Both are statically rejected outside a lexical loop, including
inside a function merely called by a loop.

`return value` exits the current function through any nested blocks or loops;
the value must exactly match its declared result type. `return` without a
value is valid in `Void` functions. There are no implicit last-expression
returns. `Void` functions may fall through. A `Void`-returning call can be
returned by a `Void` function, but is not a storable value.

The checker tracks reachable fallthrough, return, break and continue outcomes.
A non-`Void` function is rejected whenever its body can fall through. An
`if/else` supplies a return only if both reachable branches do. A return after
an incomplete conditional may complete the remaining paths. Literal `true`
and `false` conditions are recognized; other expressions are conservative:
there is no constant folding or correlation of repeated conditions yet.

A nonliteral loop may execute zero times and cannot alone prove a return.
`while true` has no fallthrough unless a reachable `break` exits that loop.
An endless path is allowed: every terminating path must return a correctly
typed value. Nested-loop breaks do not escape outer loops. All code, even
unreachable code, is checked for type and scope errors.

## Diagnostics, execution and limits

Lexical, syntax and type diagnostics carry filename, line and column. The
complete file is checked before execution. Runtime diagnostics include
division by zero, numeric overflow and the configured function call-depth
limit (or the bootstrap host stack limit, if reached sooner).
`tyvera check file.tyv` checks without running;
`tyvera run file.tyv` checks and interprets the AST.

Current limits: classes and interfaces are supported, but there is no
inheritance, generic methods/interfaces/constraints, collection literals,
`try`/`catch`, actor/process model, messaging, supervisor, async runtime,
networking API, JSON library, external secret-store provider, native interop, garbage collector
implementation, package manager or Discord SDK. No compiler backend or
performance claim. No termination checking or loop execution budget; users can
write infinite loops. Parsing and execution use the host stack, so extremely
deeply nested source is not supported. The interpreter is replaceable bootstrap
technology, not part of Tyvera semantics.
