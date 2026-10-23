# Tyvera core language reference

Tyvera is an independent, statically typed language. Java, Elixir and C++ inform
its design; they are not compatibility targets. TypeScript is not part of the
design. Python hosts the bootstrap implementation only.

## Files, functions and statements

Source files use `.tyv`. Each program is one file containing function
declarations, including `fn main()` returning `Void`. Functions can call later
declarations and recurse. Parameters require types and are immutable. Return
annotations use `-> Type`; an omitted annotation means `Void`.

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

Supported statement grammar (EBNF; expression follows the precedence below):

```text
block       = "{" [statements] "}" ;
declaration = ("let" | "var") identifier [":" type] "=" expression ;
assignment  = identifier "=" expression ;
conditional = "if" expression block ["else" (block | conditional)] ;
loop        = "while" expression block ;
return      = "return" [expression] ;
statement   = declaration | assignment | conditional | loop | return
            | "break" | "continue" | block | function-call ;
```

Assignments are statements, not expressions. Only function calls can be used
as expression statements. Declarations require an initializer.

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
Assignment, initialization, arguments and returns require exact type matches:
an `Int` cannot silently be stored in a `Float` binding. Numeric promotion is
limited to the explicit arithmetic rules below.

`println(value)` accepts one `Int`, `Float`, `Bool` or `String` and returns
`Void`. Booleans print in lowercase. Function names and local bindings occupy
separate namespaces; calls name declared functions directly. `println` cannot
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

Current limits: one file; no imports/modules, classes, interfaces, generics,
collections, pattern matching, actors/processes, messages, supervisors, native
interop, garbage collector implementation, package manager or Discord SDK.
No compiler backend or performance claim. No termination checking or loop
execution budget; users can write infinite loops. Parsing and execution use
the host stack, so extremely deeply nested source is not supported. The
interpreter is replaceable bootstrap technology, not part of Tyvera semantics.
