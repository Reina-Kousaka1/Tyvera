#!/usr/bin/env python3
"""Small, non-gating local benchmarks for the Tyvera Python bootstrap."""

from __future__ import annotations

import argparse
import io
import platform
import statistics
import time

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.lexer import Lexer
from tyvera.parser import Parser
from tyvera.typesys import TypeChecker


def _compiler_source() -> str:
    functions = "\n".join(
        f"fn add{index}(left: Int, right: Int) -> Int {{ return left + right }}"
        for index in range(100)
    )
    return functions + "\nfn main() { println(add0(20, 22)) }\n"


COLLECTION_SOURCE = '''fn main() {
    let values = List<Int>()
    var next = 0
    while next < 500 {
        values.append(next)
        next = next + 1
    }
    println(values.contains(499))
}'''


ACTOR_SOURCE = '''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on increment() { self.value = self.value + 1 }
}
fn main() {
    let counter = spawn(Counter(0))
    var sent = 0
    while sent < 500 {
        counter.increment()
        sent = sent + 1
    }
    runActors()
}'''


def _measure(label: str, action, iterations: int) -> None:
    action()  # warm up imports and one-time interpreter paths
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        action()
        samples.append(time.perf_counter() - started)
    print(f"{label:24} median {statistics.median(samples) * 1000:9.3f} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5, help="measured runs per workload (default: 5)")
    arguments = parser.parse_args()
    if arguments.iterations < 1:
        parser.error("--iterations must be at least 1")

    source = _compiler_source()
    tokens = Lexer(source, "benchmark.tyv").tokenize()
    program = Parser(tokens).parse()
    collection_program, _ = compile_source(COLLECTION_SOURCE, "collections-benchmark.tyv")
    actor_program, _ = compile_source(ACTOR_SOURCE, "actors-benchmark.tyv")

    print(f"Python {platform.python_version()} | {arguments.iterations} measured run(s) per workload")
    _measure("lexer", lambda: Lexer(source, "benchmark.tyv").tokenize(), arguments.iterations)
    _measure("parser", lambda: Parser(tokens).parse(), arguments.iterations)
    _measure("type checker", lambda: TypeChecker(program, "benchmark.tyv").check(), arguments.iterations)
    _measure("full check pipeline", lambda: compile_source(source, "benchmark.tyv"), arguments.iterations)
    _measure(
        "interpreter",
        lambda: Interpreter(program, io.StringIO()).run(),
        arguments.iterations,
    )
    _measure(
        "List workload (500 appends)",
        lambda: Interpreter(collection_program, io.StringIO()).run(),
        arguments.iterations,
    )
    _measure(
        "actor scheduler (500 msgs)",
        lambda: Interpreter(actor_program, io.StringIO()).run(),
        arguments.iterations,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
