# Tyvera language reference (0.1)

Tyvera is an independent, statically typed language. Its syntax and rules are its own; Java, Elixir, and C++ are design references, not source languages or compatibility targets. TypeScript is not part of the design.

This first specification describes the executable subset in the bootstrap toolchain. Reserved concepts such as classes, interfaces, generics, pattern matching, actors, supervision, and native code are future work and are not accepted as implemented features.

## Source files and entry point

Tyvera source files use `.tyv`. A program has one or more function declarations and must define a zero-argument `main` that returns `Unit`:

```tyv
fn main() {
    println("Hello from Tyvera!")
}
```

Functions may call functions declared later in the same file. A function without an explicit return type returns `Unit`:

```tyv
fn greet(name: String) {
    println("Hello, " + name)
}

fn main() {
    greet("Tyvera")
}
```

An explicit return type uses `->`, and a value-returning function uses `return`:

```tyv
fn add(left: Int, right: Int) -> Int {
    return left + right
}
```

## Lexical rules

- Identifiers begin with an ASCII letter or `_`, followed by letters, digits, or `_`.
- Integer literals use decimal digits. Float literals have digits on both sides of a decimal point, such as `2.5`.
- Strings use double quotes. Supported escapes are `\\n`, `\\r`, `\\t`, `\\"`, and `\\\\`.
- `//` begins a comment that continues to the end of the line.
- A newline or `;` separates statements. A newline may appear between statements; expressions currently stay on one line.
- Whitespace other than newlines separates tokens and is ignored.

## Types

The initial type set is `Int`, `Float`, `Bool`, `String`, and `Unit`. `Int` is a signed 64-bit integer; arithmetic overflow is a runtime error. `Float` is a finite binary64 value; a literal or arithmetic result outside its supported finite range is an error. `Unit` is the return type for functions without a value; it cannot be used for parameters or variables. Local variables are declared with `let` and may include a type annotation:

```tyv
let count: Int = 3
let message = "ready"
```

An unannotated local takes the type of its initializer. Assignments, mutation, and variable shadowing are not supported in this version. Local names are unique within a function.

`println` accepts one `Int`, `Float`, `Bool`, or `String` and writes it followed by a newline. User functions have fixed parameter types and argument counts. The `main` function must take no parameters and return `Unit`.

## Expressions

The supported operators, from lowest to highest precedence, are:

1. `or`
2. `and`
3. `==`, `!=`
4. `<`, `<=`, `>`, `>=`
5. `+`, `-`
6. `*`, `/`
7. unary `-`, `!`

`and` and `or` require `Bool` operands and short-circuit. Arithmetic accepts `Int` and `Float`; mixing them produces `Float`. `/` always produces `Float`. `String + String` concatenates. Equality requires matching types, except that numeric types may be compared with each other. Ordered comparisons currently accept numbers only.

Expression statements must be function calls. There are no conditionals, loops, mutable bindings, imports, or nested functions in 0.1.

## Diagnostics and execution

Lexical, syntax, and type errors include the source filename, line, and column. Programs are parsed and type-checked before any statements execute. Runtime errors, including division by zero and excessive recursive call depth, also carry a source location.

Run a program with `tyvera run path/to/file.tyv`. Use `tyvera check path/to/file.tyv` to check it without execution. The current interpreter executes the checked AST; it is the first working toolchain stage, not a native-code backend.
