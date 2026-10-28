from __future__ import annotations

import io
import os
import signal
import shlex
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tyvera.backend import ReferenceInterpreterBackend
from tyvera.cli import main as cli_main
from tyvera.compiler import compile_checked_project, compile_checked_source
from tyvera.cpp_backend import CppNativeBackend, emit_cpp
from tyvera.ir import (
    IRAppliedType,
    IRActorRefType,
    IRActorSend,
    IRActorRequest,
    IRActorSpawn,
    IRCall,
    IRCollectionLength,
    IRBuiltinCall,
    IRForEach,
    IRFunction,
    IRIf,
    IRLet,
    IRLiteral,
    IRMatch,
    IRMethodCall,
    IRMove,
    IRModule,
    IROutput,
    IRType,
    IRUsing,
    IRVariant,
    validate_ir,
)
from tyvera.lowering import lower_checked_program
from tyvera.json_limits import MAX_JSON_DOCUMENT_BYTES, MAX_JSON_NESTING
from tyvera.source import Diagnostic, Location
from tyvera.websocket_protocol import make_accept_value


REPOSITORY = Path(__file__).resolve().parents[1]


def _compiler_available() -> bool:
    configured = os.environ.get("CXX", "").strip()
    if configured:
        try:
            command = shlex.split(configured, posix=os.name != "nt")
        except ValueError:
            return False
        return bool(command) and (Path(command[0]).is_file() or shutil.which(command[0]) is not None)
    return any(shutil.which(command) for command in ("clang++", "g++", "c++", "cl"))


HAS_CPP_COMPILER = _compiler_available()


class NativeLoweringTests(unittest.TestCase):
    def lower(self, source: str):
        return lower_checked_program(compile_checked_source(source, "native_ir.tyv"))

    def test_ast_lowers_to_typed_functions_calls_and_locals(self) -> None:
        module = self.lower('''fn add(left: Int, right: Int) -> Int { return left + right }
fn main() {
    let answer: Int = add(20, 22)
    println(answer)
}
''')
        self.assertEqual(module.entry_symbol, "tyv_fn_1")
        function = module.functions[0]
        self.assertEqual(function.return_type, IRType.INT)
        self.assertEqual([item.type for item in function.parameters], [IRType.INT, IRType.INT])
        self.assertEqual(function.body[0].value.type, IRType.INT)
        self.assertEqual(module.functions[1].body[0].variable.type, IRType.INT)

    def test_if_else_while_and_loop_control_lower(self) -> None:
        module = self.lower('''fn main() {
    var index = 0
    while index < 4 {
        index = index + 1
        if index == 2 { continue }
        if index == 4 { break }
    }
}
''')
        loop = module.functions[0].body[1]
        self.assertEqual(type(loop).__name__, "IRWhile")
        self.assertEqual(type(loop.body[1]).__name__, "IRIf")
        self.assertEqual(loop.body[1].then_body[0].kind, "continue")
        self.assertEqual(loop.body[2].then_body[0].kind, "break")

    def test_shadowed_locals_get_distinct_ir_ids(self) -> None:
        module = self.lower('''fn main() {
    let value = 1
    if true { let value = 2; println(value) }
    println(value)
}
''')
        outer = module.functions[0].body[0].variable
        inner = module.functions[0].body[1].then_body[0].variable
        self.assertNotEqual(outer.identifier, inner.identifier)
        self.assertEqual(module.functions[0].body[2].value.variable_id, outer.identifier)

    def test_signed_64_bit_minimum_has_a_single_typed_ir_literal(self) -> None:
        module = self.lower("fn main() { println(-9223372036854775808) }")
        value = module.functions[0].body[0].value
        self.assertEqual(value.type, IRType.INT)
        self.assertEqual(value.value, -(1 << 63))

    def test_json_calls_and_methods_lower_with_explicit_types(self) -> None:
        module = self.lower('''fn main() {
    let parsed: Result<Json, String> = parseJson("null")
    let keyed: Map<Json, Int> = Map<Json, Int>()
    keyed.put(jsonNull(), 1)
    println(keyed.containsKey(jsonNull()))
    match parsed {
        Ok(value) => println(value.kind())
        Err(_) => println("invalid")
    }
}''')
        parsed = module.functions[0].body[0].value
        self.assertIsInstance(parsed, IRBuiltinCall)
        self.assertEqual(parsed.builtin, "parseJson")
        self.assertEqual(parsed.type, IRAppliedType("Result", (IRType.JSON, IRType.STRING)))
        kind_call = module.functions[0].body[4].arms[0].body[0].value
        self.assertIsInstance(kind_call, IRMethodCall)
        self.assertEqual(kind_call.receiver.type, IRType.JSON)
        self.assertEqual(kind_call.method, "kind")
        validate_ir(module)
        generated = emit_cpp(module)
        self.assertIn("tyvera_runtime::parse_json", generated)
        self.assertIn("tyvera_runtime::json_kind", generated)
        self.assertIn("tyvera_runtime::map_put<tyvera_runtime::JsonValue", generated)

    def test_discord_decode_package_lowers_nominal_types_and_task_primitives(self) -> None:
        entry = REPOSITORY / "libraries" / "discord" / "examples" / "decode.tyv"
        checked = compile_checked_project(entry, REPOSITORY)
        module = lower_checked_program(checked)
        self.assertEqual(len(module.records), 4)
        self.assertEqual(len(module.enums), 3)
        self.assertTrue(any(item.name.endswith("GatewayDispatch") for item in module.enums))
        validate_ir(module)

        generated = emit_cpp(module)
        self.assertIn("GatewayDispatch", generated)
        self.assertIn("RestFailure", generated)
        self.assertIn("tyvera_runtime::SecretString", generated)
        self.assertIn("tyvera_runtime::sleep_for", generated)

    def test_environment_lowering_keeps_values_secret_wrapped(self) -> None:
        module = self.lower('''fn main() {
    match environment("TYVERA_NATIVE_FAKE_TOKEN") {
        Some(token) => println("present")
        None => println("missing")
    }
}''')
        call = module.functions[0].body[0].value
        self.assertIsInstance(call, IRBuiltinCall)
        self.assertEqual(
            call.type,
            IRAppliedType("Option", (IRAppliedType("Secret", (IRType.STRING,)),)),
        )
        generated = emit_cpp(module)
        self.assertIn("tyvera_runtime::environment_value", generated)
        self.assertIn("std::optional<tyvera_runtime::SecretString>", generated)

    def test_transparent_primitive_aliases_lower_to_their_target_type(self) -> None:
        module = self.lower('''type Count = Int
fn increment(value: Count) -> Count { return value + 1 }
fn main() { println(increment(4)) }
''')
        self.assertEqual(module.functions[0].parameters[0].type, IRType.INT)
        self.assertEqual(module.functions[0].return_type, IRType.INT)

    def test_linked_modules_with_scalar_functions_lower(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-modules-") as temporary:
            root = Path(temporary)
            (root / "math.tyv").write_text(
                "pub fn add(left: Int, right: Int) -> Int { return left + right }\n",
                encoding="utf-8",
            )
            entry = root / "main.tyv"
            entry.write_text(
                'import "math.tyv" as math\nfn main() { println(math.add(2, 3)) }\n',
                encoding="utf-8",
            )
            module = lower_checked_program(compile_checked_project(entry, root))
            self.assertEqual(len(module.functions), 2)
            entry_function = next(function for function in module.functions if function.name == "main")
            call = entry_function.body[0].value
            self.assertIsInstance(call, IRCall)
            self.assertEqual(call.type, IRType.INT)
            self.assertIn(call.function, {function.symbol for function in module.functions})

    def test_linked_module_actor_lowers_without_discord_specific_handling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-module-actor-") as temporary:
            root = Path(temporary)
            (root / "counter.tyv").write_text(
                "pub actor Counter {\n"
                "    var value: Int\n"
                "    init(start: Int) { self.value = start }\n"
                "    on report() { println(self.value) }\n"
                "}\n",
                encoding="utf-8",
            )
            entry = root / "main.tyv"
            entry.write_text(
                'import "counter.tyv" as counters\n'
                'fn main() {\n'
                '    let counter: ActorRef<counters.Counter> = spawn(counters.Counter(7))\n'
                '    counter.report()\n'
                '    let result = runActors()\n'
                '}\n',
                encoding="utf-8",
            )
            module = lower_checked_program(compile_checked_project(entry, root))
            generated = emit_cpp(module)
            self.assertEqual(module.actors[0].name, "counter.Counter")
            self.assertIn("class tyv_actor_counter_x2e_Counter", generated)
            self.assertIn("tyv_actor_counter_x2e_Counter", generated)

    def test_actor_spawn_state_and_mailbox_sends_lower_to_typed_ir(self) -> None:
        module = self.lower('''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on add(amount: Int) { self.value = self.value + amount }
    on report() { println(self.value) }
}
fn main() {
    let counter: ActorRef<Counter> = spawn(Counter(10))
    counter.add(2)
    counter.report()
    let result = runActors()
}
''')
        self.assertEqual(module.actors[0].name, "Counter")
        self.assertEqual(module.actors[0].fields[0].type, IRType.INT)
        self.assertEqual(module.actors[0].initializer_symbol, "tyv_actor_0_handler_0")
        self.assertEqual(module.functions[0].body[0].value.type, IRActorRefType("Counter"))
        self.assertIsInstance(module.functions[0].body[0].value, IRActorSpawn)
        self.assertIsInstance(module.functions[0].body[1].expression, IRActorSend)

    def test_actor_request_reply_lowers_to_typed_ir(self) -> None:
        module = self.lower('''actor Worker {
    on get(reply: ReplySender<Int>) { reply.send(1) }
}
fn main() {
    let worker = spawn(Worker())
    let response = worker.request(get)
}
''')
        request = module.functions[0].body[1].value
        self.assertIsInstance(request, IRActorRequest)
        self.assertEqual(request.actor_name, "Worker")
        self.assertEqual(request.handler_symbol, module.actors[0].handler_symbols[0][1])
        self.assertEqual(request.type, IRAppliedType("ReplyReceiver", (IRType.INT,)))
        self.assertIsNone(request.timeout)

    def test_supervisor_strategy_and_child_start_lower_to_typed_ir(self) -> None:
        module = self.lower('''actor Worker { on work() {} }
fn main() {
    let supervisor = restForOne(2, 1000)
    let worker = supervisor.start(Worker())
}
''')
        self.assertEqual(module.functions[0].body[0].value.type, IRType.SUPERVISOR)
        start = module.functions[0].body[1].value
        self.assertIsInstance(start, IRMethodCall)
        self.assertEqual(start.receiver.type, IRType.SUPERVISOR)
        self.assertIsInstance(start.arguments[0], IRActorSpawn)
        self.assertTrue(start.arguments[0].restartable)

    def test_actor_owned_tasks_lower_to_typed_ir(self) -> None:
        module = self.lower('''async fn background() {}
actor Worker { on start() { let task = background() } }
fn main() {
    let worker = spawn(Worker())
    worker.start()
    runActors()
}
''')
        handler = next(function for function in module.functions if function.actor_name == "Worker")
        self.assertEqual(handler.body[0].variable.type, IRAppliedType("Task", (IRType.VOID,)))
        self.assertEqual(handler.body[0].value.type, IRAppliedType("Task", (IRType.VOID,)))
        generated = emit_cpp(module)
        self.assertIn("ActorHandlerScope", generated)
        self.assertIn("attach_task_to_current_actor", generated)

    def test_native_task_cancellation_methods_lower_to_typed_ir(self) -> None:
        module = self.lower('''async fn background() {}
fn main() {
    let task = background()
    println(task.isDone())
    println(task.cancel())
    println(task.isCancelled())
}
''')
        calls = [
            statement.value for statement in module.functions[1].body
            if isinstance(statement, IROutput)
        ]
        self.assertTrue(all(isinstance(call, IRMethodCall) for call in calls))
        self.assertEqual([call.method for call in calls], ["isDone", "cancel", "isCancelled"])
        self.assertTrue(all(call.receiver.type == IRAppliedType("Task", (IRType.VOID,)) for call in calls))
        generated = emit_cpp(module)
        self.assertIn(".is_done()", generated)
        self.assertIn(".cancel()", generated)
        self.assertIn(".is_cancelled()", generated)

    def test_task_result_polling_has_typed_ir_and_resource_moves(self) -> None:
        module = self.lower('''async fn answer() -> Int { return 42 }
async fn empty() {}
fn main() {
    let number = answer()
    let polled = number.tryResult()
    let task = empty()
    let done = task.tryResult()
}
''')
        body = module.functions[-1].body
        self.assertIsInstance(body[1].value, IRMethodCall)
        self.assertEqual(body[1].value.type, IRAppliedType(
            "Result", (IRAppliedType("Option", (IRType.INT,)), IRType.STRING)))
        self.assertEqual(body[3].value.type, IRAppliedType("Result", (IRType.BOOL, IRType.STRING)))
        generated = emit_cpp(module)
        self.assertIn(".try_result<false>()", generated)
        self.assertIn(".try_result()", generated)

        owned = self.lower('''fn main() {
    let task = tcpListen("127.0.0.1", 0, 8)
    let result = task.tryResult()
}
''')
        self.assertIsInstance(owned.functions[-1].body[1].value.receiver, IRMove)
        self.assertIn(".try_result<true>()", emit_cpp(owned))
        with self.assertRaisesRegex(Diagnostic, "moved"):
            compile_checked_source('''fn main() {
    let task = tcpListen("127.0.0.1", 0, 8)
    let result = task.tryResult()
    println(task.isDone())
}
''', "consumed_task.tyv")

    def test_ir_validator_rejects_wrong_task_poll_result_type(self) -> None:
        from dataclasses import replace

        module = self.lower('''async fn answer() -> Int { return 42 }
fn main() {
    let task = answer()
    let result = task.tryResult()
}
''')
        main = module.functions[-1]
        statement = main.body[1]
        invalid = replace(statement, value=replace(statement.value, type=IRType.BOOL))
        broken_main = replace(main, body=(main.body[0], invalid))
        broken = replace(module, functions=(*module.functions[:-1], broken_main))
        with self.assertRaisesRegex(Diagnostic, "result has the wrong type"):
            validate_ir(broken)

    def test_native_duration_and_task_timeout_lower_to_typed_ir(self) -> None:
        module = self.lower('''async fn value() -> Int { return 7 }
fn main() {
    let duration = seconds(2) - milliseconds(500)
    println(duration.inMilliseconds())
    let task = value()
    let timed = task.withTimeout(milliseconds(20))
}
''')
        calls = [
            statement.value for statement in module.functions[1].body
            if isinstance(statement, IROutput)
        ]
        self.assertIsInstance(calls[0], IRMethodCall)
        self.assertEqual(calls[0].receiver.type, IRType.DURATION)
        self.assertEqual(calls[0].method, "inMilliseconds")
        generated = emit_cpp(module)
        self.assertIn("duration_seconds", generated)
        self.assertIn("task_with_timeout", generated)

    def test_native_verified_tls_connect_lowers_to_typed_ir(self) -> None:
        module = self.lower('''async fn connect() {
    let opened = await tlsConnect("localhost", 443)
    using connection = move opened {
        println(connection.isOpen())
        connection.close()
    } else error {
        println(error)
    }
}
fn main() {
    let task = connect()
    runTasks()
}''')
        using_statement = next(
            statement for statement in module.functions[0].body if isinstance(statement, IRUsing)
        )
        self.assertEqual(using_statement.resource.type, IRType.TLS_CONNECTION)
        generated = emit_cpp(module)
        self.assertIn("#define TYVERA_NATIVE_TLS 1", generated)
        self.assertIn("tyvera_runtime::tls_connect", generated)
        self.assertIn("tyvera_runtime::tls_is_open", generated)

    def test_native_byte_buffer_and_binary_io_lower_to_typed_ir(self) -> None:
        module = self.lower('''fn main() {
    let buffer = byteBuffer(4)
    let read_result = readBytes("input.bin")
    let write_result = writeBytes("output.bin", bytesFromString("data"))
    let text_result = readText("input.txt")
    let joined = pathJoin("folder", "file.txt")
    let exists = pathExists(joined)
}
''')
        builtins = [statement.value.builtin for statement in module.functions[0].body if isinstance(statement, IRLet)]
        self.assertEqual(builtins, ["byteBuffer", "readBytes", "writeBytes", "readText", "pathJoin", "pathExists"])
        self.assertEqual(module.functions[0].body[0].value.type, IRAppliedType("Result", (IRType.BYTE_BUFFER, IRType.STRING)))

    def test_native_using_lowers_checked_resource_transfer_and_move(self) -> None:
        module = self.lower('''fn relay(opened: Result<TextFile,String>) -> Result<TextFile,String> {
    return move opened
}
fn main() {
    using file = openText("out.txt", "write") {
        file.write("text")
    } else error {
        println(error)
    }
}
''')
        relay_move = module.functions[0].body[0].value
        self.assertIsInstance(relay_move, IRMove)
        using = module.functions[1].body[0]
        self.assertIsInstance(using, IRUsing)
        self.assertEqual(using.resource.type, IRType.TEXT_FILE)
        self.assertEqual(using.acquisition.type, IRAppliedType("Result", (IRType.TEXT_FILE, IRType.STRING)))
        self.assertEqual(using.error_variable.type, IRType.STRING)

    def test_native_await_moves_resource_results_and_tcp_using_has_cleanup_metadata(self) -> None:
        module = self.lower('''async fn reserve() {
    let pending = tcpListen("127.0.0.1", 0, 8)
    let opened = await move pending
    using listener = move opened {
        println(listener.isOpen())
    } else error {
        println(error)
    }
}
fn main() {
    let task = reserve()
    runTasks()
}
''')
        source = emit_cpp(module)
        self.assertIn(".await_move(", source)
        self.assertIn("set_resource_location(tyv_local_", source)
        self.assertIn("cannot close TCP listener", source)

    def test_native_byte_buffer_and_io_signatures_reject_wrong_types(self) -> None:
        invalid_sources = (
            '''fn main() {
    match byteBuffer(1) {
        Err(_) => {}
        Ok(buffer) => buffer.set(0, "not a Byte")
    }
}''',
            'fn main() { writeBytes("out.bin", "not Bytes") }',
            'fn main() { pathJoin(1, "file") }',
            'fn main() { byteBuffer("not a length") }',
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(Diagnostic) as caught:
                    compile_checked_source(source, "native-io-types.tyv")
                self.assertEqual(caught.exception.stage, "type")

    def test_ir_validator_rejects_mistyped_native_byte_buffer_builtin(self) -> None:
        from tyvera.ir import IRExpressionStatement

        location = Location("bad_buffer_ir.tyv", 1, 1)
        call = IRBuiltinCall(
            "byteBuffer", (IRLiteral(1, IRType.INT, location),), IRType.BYTE_BUFFER, location,
        )
        main = IRFunction("main", "tyv_fn_main", (), IRType.VOID,
                          (IRExpressionStatement(call, location),), location)
        with self.assertRaisesRegex(Diagnostic, "invalid typed IR builtin 'byteBuffer'"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_sum_types_and_recursive_patterns_lower_with_checked_types(self) -> None:
        module = self.lower('''fn main() {
    let option: Option<Int> = Some(3)
    match option {
        Some(value) => println(value)
        None => println(0)
    }
    let result: Result<Option<Int>, String> = Ok<Option<Int>, String>(Some(5))
    match result {
        Ok(Some(value)) => println(value)
        Ok(None) => println(0)
        Err(reason) => println(reason)
    }
}
''')
        function = module.functions[0]
        self.assertIsInstance(function.body[0].value, IRVariant)
        self.assertEqual(function.body[0].variable.type, IRAppliedType("Option", (IRType.INT,)))
        option_match = function.body[1]
        result_match = function.body[3]
        self.assertIsInstance(option_match, IRMatch)
        self.assertIsInstance(result_match, IRMatch)
        self.assertTrue(option_match.exhaustive)
        self.assertTrue(result_match.exhaustive)
        self.assertEqual(option_match.covered_cases, frozenset({"Some", "None"}))

    def test_list_map_and_immutable_bytes_lower_to_typed_ir(self) -> None:
        module = self.lower('''fn main() {
    let values: List<Int> = List<Int>()
    values.append(2)
    println(values.length)
    for value in values { println(value) }
    let entries: Map<String, Int> = Map<String, Int>()
    entries.put("key", 3)
    for key in entries { println(key) }
    let bytes: Bytes = bytesFromString("a")
    println(bytes.length())
}
''')
        body = module.functions[0].body
        self.assertEqual(body[0].variable.type, IRAppliedType("List", (IRType.INT,)))
        self.assertIsInstance(body[1].expression, IRMethodCall)
        self.assertIsInstance(body[2].value, IRCollectionLength)
        self.assertIsInstance(body[3], IRForEach)
        self.assertEqual(body[3].iterable.type.name, "List")
        self.assertIsInstance(body[6], IRForEach)
        self.assertEqual(body[6].iterable.type.name, "Map")
        self.assertEqual(body[7].variable.type, IRType.BYTES)

    def test_ir_validator_rejects_out_of_range_byte_literal(self) -> None:
        location = Location("bad_byte_ir.tyv", 1, 1)
        main = IRFunction(
            "main", "tyv_fn_main", (), IRType.VOID,
            (IROutput("stdout", IRLiteral(256, IRType.BYTE, location), location),), location,
        )
        with self.assertRaisesRegex(Diagnostic, "invalid Byte literal"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_ir_validator_rejects_untyped_control_flow(self) -> None:
        location = Location("bad_ir.tyv", 1, 1)
        main = IRFunction(
            "main", "tyv_fn_main", (), IRType.VOID,
            (IRIf(IRLiteral(1, IRType.INT, location), (), (), location),), location,
        )
        with self.assertRaisesRegex(Diagnostic, "if condition must be Bool"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_ir_validator_rejects_out_of_scope_local_reference(self) -> None:
        location = Location("bad_ir.tyv", 1, 1)
        from tyvera.ir import IRExpressionStatement, IRLocalRef

        main = IRFunction(
            "main", "tyv_fn_main", (), IRType.VOID,
            (IRExpressionStatement(IRLocalRef(42, IRType.INT, location), location),), location,
        )
        with self.assertRaisesRegex(Diagnostic, "outside its scope"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_ir_validator_rejects_nonvoid_fallthrough(self) -> None:
        location = Location("bad_ir.tyv", 1, 1)
        main = IRFunction("main", "tyv_fn_main", (), IRType.VOID, (), location)
        value = IRFunction("value", "tyv_fn_value", (), IRType.INT, (), location)
        with self.assertRaisesRegex(Diagnostic, "fall through without returning Int"):
            validate_ir(IRModule((main, value), "tyv_fn_main"))

    def test_generated_cpp_contains_typed_runtime_and_no_python_bridge(self) -> None:
        module = self.lower('''fn add(a: Int, b: Int) -> Int { return a + b }
fn main() { println(add(1, 2)); println("Tyvera") }
''')
        source = emit_cpp(module)
        self.assertIn("std::int64_t tyv_fn_0", source)
        self.assertIn("checked_add", source)
        self.assertIn("tyvera_runtime::Failure", source)
        self.assertIn("if (!tyvera_runtime::install_shutdown_handlers())", source)
        self.assertIn("ExitCode::initialization_failure", source)
        self.assertNotIn("python", source.lower())
        self.assertNotIn("PyObject", source)

    def test_generated_actor_runtime_uses_bounded_round_robin_mailboxes(self) -> None:
        module = self.lower('''actor Worker {
    var value: Int
    init(start: Int) { self.value = start }
    on add(amount: Int) { self.value = self.value + amount }
    on again() { self.add(1) }
}
fn main() {
    let worker = spawn(Worker(0))
    worker.add(2)
    worker.again()
    let result = runActors()
}
''')
        source = emit_cpp(module)
        self.assertIn("MAX_ACTOR_MAILBOX = 100000", source)
        self.assertIn("MAX_ACTOR_DELIVERIES = 100000", source)
        self.assertIn("std::deque<std::function<void()>> mailbox", source)
        self.assertIn("std::vector<std::shared_ptr<ActorBase>> actors", source)
        self.assertIn("actor_handler_active()", source)
        self.assertIn("runActors cannot be called from an actor message handler", source)
        self.assertIn("void tyv_clear_state() noexcept override", source)
        self.assertIn("actor->tyv_clear_state();", source)
        self.assertIn("actor->mailbox.pop_back()", source)
        self.assertIn("class tyv_actor_Worker final : public tyvera_runtime::ActorBase", source)
        self.assertIn("std::weak_ptr<tyv_actor_Worker>", source)
        self.assertIn("run_actors", source)
        self.assertNotIn("PyObject", source)

    def test_new_native_examples_lower_and_emit_cpp_without_python_bridge(self) -> None:
        for name in (
            "native_sum_types.tyv", "native_collections.tyv", "native_bytes.tyv", "byte_buffers.tyv",
            "tcp_echo.tyv",
        ):
            with self.subTest(example=name):
                checked = compile_checked_project(REPOSITORY / "examples" / name, REPOSITORY)
                module = lower_checked_program(checked)
                source = emit_cpp(module)
                self.assertIn("std::optional", source)
                self.assertIn("exhaustive match had no matching pattern", source)
                self.assertIn("tyvera_generated", source)
                self.assertNotIn("PyObject", source)
                self.assertNotIn("python", source.lower())
                if name == "native_collections.tyv":
                    self.assertIn(
                        "tyvera_runtime::map_put<std::optional<std::int64_t>, std::string>", source,
                    )
                    self.assertIn(
                        "tyvera_runtime::map_get<std::optional<std::int64_t>, std::string>", source,
                    )
                    self.assertIn(
                        "tyvera_runtime::list_append<std::optional<std::int64_t>>", source,
                    )
                    self.assertIn(
                        "tyvera_runtime::map_put<std::string, std::optional<std::int64_t>>", source,
                    )
                if name == "tcp_echo.tyv":
                    self.assertIn("struct TaskState", source)
                    self.assertIn("::getaddrinfo", source)
                    self.assertIn("tyvera_runtime::tcp_connect", source)
                    self.assertIn("tyvera_runtime::tcp_listen", source)
                    self.assertIn("tyvera_runtime::tcp_accept", source)
                    self.assertIn("tyvera_runtime::tcp_read", source)
                    self.assertIn("tyvera_runtime::tcp_write_all", source)

    def test_build_cli_reports_unsupported_native_construct_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-build-") as temporary:
            root = Path(temporary)
            source = root / "unsupported.tyv"
            source.write_text('''actor Worker {
    on ping() {}
}
fn main() {
    let worker = spawn(Worker())
    let timer = worker.sendAfter(milliseconds(1), ping)
}''', encoding="utf-8")
            output = root / "unsupported"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli_main(["build", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("native actor timers are not yet supported", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_build_cli_typechecks_before_creating_any_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-static-error-") as temporary:
            root = Path(temporary)
            source = root / "invalid.tyv"
            source.write_text('fn main() { println(1 + true) }', encoding="utf-8")
            output = root / "invalid"
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                result = cli_main(["build", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("type error", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_build_cli_reports_missing_compiler_without_creating_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-no-cxx-") as temporary:
            root = Path(temporary)
            source = root / "hello.tyv"
            source.write_text("fn main() { println(1) }", encoding="utf-8")
            output = root / "hello"
            stderr = io.StringIO()
            with patch.dict(os.environ, {"CXX": "tyvera-cxx-that-does-not-exist"}):
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    result = cli_main(["build", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("C++ compiler from CXX was not found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_missing_cxx_compiler_has_a_controlled_diagnostic(self) -> None:
        module = self.lower("fn main() { println(1) }")
        with tempfile.TemporaryDirectory(prefix="tyvera-no-cxx-") as temporary:
            with patch.dict(os.environ, {"CXX": "tyvera-cxx-that-does-not-exist"}):
                with self.assertRaises(Diagnostic) as caught:
                    CppNativeBackend().build(module, Path(temporary) / "app")
            self.assertEqual(caught.exception.stage, "backend")
            self.assertIn("was not found", caught.exception.message)


    def test_native_task_group_lowering_preserves_await_and_owner_methods(self) -> None:
        module = self.lower('''async fn child() {}
async fn exercise() {
    let group = taskGroup()
    let pending = child()
    group.add(pending)
    await group
    println(group.isDone())
}
fn main() {
    let pending = exercise()
    runTasks()
}
''')
        generated = emit_cpp(module)
        self.assertIn("tyvera_runtime::task_group(", generated)
        self.assertIn(".add(", generated)
        self.assertIn(".await(", generated)
        self.assertIn(".is_done(", generated)


@unittest.skipUnless(HAS_CPP_COMPILER, "native end-to-end checks require a C++17 compiler")
class NativeExecutionTests(unittest.TestCase):
    def _build_and_run(
        self, source_file: Path, output: Path, *, project_root: Path = REPOSITORY,
        cwd: Path = REPOSITORY,
    ) -> subprocess.CompletedProcess[str]:
        checked = compile_checked_project(source_file, project_root)
        module = lower_checked_program(checked)
        CppNativeBackend().build(module, output, project_root=project_root)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if os.name != "nt":
            # The absolute executable must not depend on Python being on PATH.
            environment["PATH"] = ""
        return subprocess.run(
            [str(output)], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=environment, check=False,
        )

    @staticmethod
    def _reference_result(source: str, project_root: Path) -> tuple[int, str, str]:
        checked = compile_checked_source(source, "native-io.tyv")
        output = io.StringIO()
        errors = io.StringIO()
        try:
            ReferenceInterpreterBackend().execute(
                checked, output=output, error_output=errors, project_root=project_root,
            )
        except Diagnostic as error:
            return 1, output.getvalue(), error.render() + "\n"
        return 0, output.getvalue(), errors.getvalue()

    def _native_source_result(
        self, source: str, project_root: Path, output: Path, cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        checked = compile_checked_source(source, "native-io.tyv")
        CppNativeBackend().build(lower_checked_program(checked), output, project_root=project_root)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if os.name != "nt":
            environment["PATH"] = ""
        return subprocess.run(
            [str(output)], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", env=environment, check=False, timeout=30,
        )

    @staticmethod
    def _tyvera_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def test_native_task_result_polling_preserves_values_and_actor_replies(self) -> None:
        source = '''actor Counter {
    on read(reply: ReplySender<Int>) { reply.send(42) }
}
async fn answer(counter: ActorRef<Counter>) -> Int {
    let value: Int = await counter.request(read)
    return value
}
async fn empty() {}
fn main() {
    let counter = spawn(Counter())
    let task = answer(counter)
    let voidTask = empty()
    match task.tryResult() {
        Ok(None) => println("pending")
        _ => println("unexpected early result")
    }
    match voidTask.tryResult() {
        Ok(done) => println(done)
        Err(error) => println(error)
    }
    runTasks()
    match task.tryResult() {
        Ok(Some(value)) => println(value)
        _ => println("missing result")
    }
    match task.tryResult() {
        Ok(Some(value)) => println(value)
        _ => println("missing repeated result")
    }
    match voidTask.tryResult() {
        Ok(done) => println(done)
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-poll-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "poll", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "pending\nfalse\n42\n42\ntrue\n")

    def test_native_task_polling_reports_failure_cancellation_and_timeout(self) -> None:
        source = '''async fn answer() -> Int { return 42 }
async fn broken() -> Int {
    let invalid: Float = 1 / 0
    return 0
}
async fn slow() { await sleepFor(seconds(30)) }
async fn parent() { let child = broken() }
fn main() {
    let cancelled = answer()
    cancelled.cancel()
    match cancelled.tryResult() {
        Err(error) => println(error)
        _ => println("unexpected cancelled result")
    }
    runTasks()
    let failed = broken()
    runTasks()
    match failed.tryResult() {
        Err(error) => println(error)
        _ => println("unexpected failed result")
    }
    let owner = parent()
    runTasks()
    match owner.tryResult() {
        Err(error) => println(error)
        _ => println("unexpected owner result")
    }
    let timed = slow().withTimeout(milliseconds(0))
    runTasks()
    match timed.tryResult() {
        Err(error) => println(error)
        _ => println("unexpected timeout result")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-poll-errors-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "poll-errors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertTrue(native.stdout.startswith("task was cancelled\n"))
            self.assertIn("owned child task failed:", native.stdout)
            self.assertTrue(native.stdout.endswith("task timed out\n"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "native socket resource polling requires Linux")
    def test_native_task_polling_transfers_resources_and_cancels_pending_acquisition(self) -> None:
        source = '''fn closeListener(opened: Result<TcpListener,String>) -> Int {
    using listener = move opened {
        println(listener.isOpen())
        match listener.localAddress() {
            Ok(address) => return address.port()
            Err(error) => println(error)
        }
    } else error { println(error) }
    return 0
}
fn main() {
    let task = tcpListen("127.0.0.1", 0, 8)
    runTasks()
    let polled = task.tryResult()
    var port: Int = 0
    match move polled {
        Ok(maybe) => match move maybe {
            Some(opened) => port = closeListener(move opened)
            None => println("missing listener")
        }
        Err(error) => println(error)
    }
    // The original listener must already be closed before binding its port again.
    let rebound = tcpListen("127.0.0.1", port, 8)
    runTasks()
    let second = rebound.tryResult()
    match move second {
        Ok(maybe) => match move maybe {
            Some(opened) => { let ignored = closeListener(move opened) }
            None => println("missing rebound listener")
        }
        Err(error) => println(error)
    }
    let pending = tcpListen("127.0.0.1", 0, 8)
    let early = pending.tryResult()
    match move early {
        Ok(maybe) => match move maybe {
            None => println("pending cancelled")
            Some(opened) => { let unexpected = closeListener(move opened) }
        }
        Err(error) => println(error)
    }
    runTasks()
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-poll-resource-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "poll-resource", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "true\ntrue\npending cancelled\n")

    def test_native_task_scheduler_services_idle_supervisor_backoff(self) -> None:
        source = '''actor Worker {
    init() { println("started") }
    on fail(reply: ReplySender<Int>) {
        reply.send(1)
        let failure: Float = 1 / 0
    }
}
async fn observe(worker: ActorRef<Worker>) {
    let acknowledged: Int = await worker.request(fail)
    println(acknowledged)
    await sleepFor(milliseconds(250))
    println(worker.isAlive())
}
fn main() {
    let supervisor = oneForOne(2, 5000, exponentialBackoff(10, 10))
    let worker = supervisor.start(Worker())
    let task = observe(worker)
    match runTasks() {
        Ok(_) => println("drained")
        Err(error) => println(error)
    }
    println(worker.isAlive())
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-shared-deadlines-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "deadlines", root)
            self.assertEqual((native.returncode, native.stderr), (reference[0], reference[2]))
            reference_lines = reference[1].splitlines()
            native_lines = native.stdout.splitlines()
            self.assertEqual(sorted(native_lines), sorted(reference_lines))
            self.assertEqual(native_lines.count("started"), 2)
            self.assertEqual(native_lines.count("true"), 2)
            self.assertEqual(native_lines[-2:], ["drained", "true"])

    def test_native_monitor_stop_wakeups_do_not_invert_scheduler_locks(self) -> None:
        source = '''actor Worker { on work() {} }
async fn observe(monitor: ReplyReceiver<String>) {
    let reason: String = await monitor
    println(reason)
}
async fn stop(worker: ActorRef<Worker>) { worker.stop() }
fn main() {
    var index: Int = 0
    while index < 32 {
        let worker = spawn(Worker())
        let observer = observe(worker.monitor())
        let stopper = stop(worker)
        runTasks()
        println(worker.isAlive())
        index = index + 1
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-monitor-wakeup-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "monitor-wakeup", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "normal stop\nfalse\n" * 32)

    def test_native_sleep_for_matches_reference_task_scheduling(self) -> None:
        source = '''async fn pause() {
    await sleepFor(milliseconds(0))
    println("awake")
}
fn main() {
    let pending = pause()
    let result = runTasks()
    println("finished")
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-sleep-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "sleep", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_environment_keeps_values_secret_wrapped(self) -> None:
        source = '''fn main() {
    match environment("TYVERA_NATIVE_FAKE_TOKEN") {
        Some(token) => println("present")
        None => println("missing")
    }
    match environment("INVALID NAME") {
        Some(_) => println("invalid name was accepted")
        None => println("invalid name rejected")
    }
}'''
        with patch.dict(os.environ, {"TYVERA_NATIVE_FAKE_TOKEN": "fake-native-token"}):
            with tempfile.TemporaryDirectory(prefix="tyvera-native-environment-") as temporary:
                root = Path(temporary)
                reference = self._reference_result(source, root)
                native = self._native_source_result(source, root, root / "environment", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_discord_decode_example_matches_reference_interpreter(self) -> None:
        entry = REPOSITORY / "libraries" / "discord" / "examples" / "decode.tyv"
        checked = compile_checked_project(entry, REPOSITORY)
        output = io.StringIO()
        errors = io.StringIO()
        try:
            ReferenceInterpreterBackend().execute(
                checked, output=output, error_output=errors, project_root=REPOSITORY,
            )
            reference = (0, output.getvalue(), errors.getvalue())
        except Diagnostic as error:
            reference = (1, output.getvalue(), error.render() + "\n")

        with tempfile.TemporaryDirectory(prefix="tyvera-native-discord-decode-") as temporary:
            root = Path(temporary)
            CppNativeBackend().build(lower_checked_program(checked), root / "discord-decode", project_root=REPOSITORY)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            if os.name != "nt":
                environment["PATH"] = ""
            native = subprocess.run(
                [str(root / "discord-decode")], cwd=REPOSITORY, capture_output=True,
                text=True, encoding="utf-8", env=environment, check=False,
            )
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_actor_state_fifo_self_send_and_round_robin_match_reference(self) -> None:
        source = '''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on add(amount: Int) { self.value = self.value + amount }
    on report() { println(self.value) }
}
actor Marker {
    on begin() { println("begin"); self.finish() }
    on finish() { println("finish") }
}
fn main() {
    let counter = spawn(Counter(5))
    let marker = spawn(Marker())
    counter.add(2)
    counter.add(3)
    counter.report()
    marker.begin()
    marker.finish()
    match runActors() {
        Ok(count) => println(count)
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actors-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_actor_request_reply_fifo_matches_reference(self) -> None:
        source = '''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on add(amount: Int, reply: ReplySender<Int>) {
        self.value = self.value + amount
        reply.send(self.value)
    }
}
fn main() {
    let counter = spawn(Counter(10))
    let first = counter.request(add, 3)
    let second = counter.request(add, 4)
    println(first.isReady())
    match runActors() {
        Ok(count) => println(count)
        Err(error) => println(error)
    }
    match first.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("first pending")
        Err(error) => println(error)
    }
    match second.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("second pending")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-reply-fifo-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "false\n2\n13\n17\n")

    def test_native_actor_request_timeout_and_cancel_match_reference(self) -> None:
        source = '''actor Worker {
    on get(reply: ReplySender<Int>) { reply.send(9) }
}
fn main() {
    let worker = spawn(Worker())
    let timed = worker.requestWithin(milliseconds(0), get)
    let cancelled = worker.request(get)
    println(timed.isReady())
    println(timed.isTimedOut())
    println(cancelled.cancel())
    println(cancelled.isCancelled())
    match runActors() {
        Ok(count) => println(count)
        Err(error) => println(error)
    }
    match timed.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("timed pending")
        Err(error) => println(error)
    }
    match cancelled.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("cancelled empty")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-reply-timeout-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout,
                             "true\ntrue\ntrue\ntrue\n2\nrequest timed out\ncancelled empty\n")

    def test_native_task_await_reply_pumps_actor_and_consumes_value(self) -> None:
        source = '''actor Worker {
    on get(reply: ReplySender<Int>) { reply.send(73) }
}
async fn query() {
    let worker = spawn(Worker())
    let pending = worker.request(get)
    let value = await pending
    println(value)
}
fn main() {
    let task = query()
    match runTasks() {
        Ok(_) => println("finished")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-await-reply-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "await-reply", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "73\nfinished\n")


    def test_native_concurrent_actor_reply_waiters_share_task_scheduler(self) -> None:
        source = '''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on increment(reply: ReplySender<Int>) {
        self.value = self.value + 1
        println("handled")
        reply.send(self.value)
    }
}
async fn query(counter: ActorRef<Counter>) {
    let answer: Int = await counter.request(increment)
    println(answer)
}
async fn spin() {
    var index: Int = 0
    while index < 8 {
        println("tick")
        index = index + 1
    }
}
fn main() {
    let counter = spawn(Counter(0))
    let first = query(counter)
    let second = query(counter)
    let spinner = spin()
    match runTasks() {
        Ok(_) => println("complete")
        Err(error) => eprintln(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-concurrent-replies-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "concurrent-replies", root)
            self.assertEqual((native.returncode, native.stderr), (reference[0], reference[2]))
            reference_lines = reference[1].splitlines()
            native_lines = native.stdout.splitlines()
            self.assertEqual(sorted(native_lines), sorted(reference_lines))
            for lines in (reference_lines, native_lines):
                self.assertEqual(lines.count("handled"), 2)
                self.assertEqual(lines.count("tick"), 8)
                self.assertEqual(sorted(line for line in lines if line in {"1", "2"}), ["1", "2"])
                self.assertEqual(lines[-1], "complete")

    def test_native_actor_reply_waiter_is_not_starved_by_ready_tasks(self) -> None:
        source = '''async fn spin() {
    var index: Int = 0
    while index < 20 {
        println(index)
        index = index + 1
    }
}
actor Worker {
    on ping(reply: ReplySender<Int>) {
        println("handled")
        reply.send(7)
    }
}
async fn query(worker: ActorRef<Worker>) {
    let answer: Int = await worker.request(ping)
    if answer == 7 {
        println("reply delivered")
    } else {
        println("reply mismatch")
    }
}
fn main() {
    let worker = spawn(Worker())
    let spinner = spin()
    let request = query(worker)
    match runTasks() {
        Ok(_) => println("complete")
        Err(reason) => println(reason)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-reply-fairness-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actor-reply-fairness", root)
            self.assertEqual((native.returncode, native.stderr), (reference[0], reference[2]))
            reference_lines = reference[1].splitlines()
            native_lines = native.stdout.splitlines()
            for lines in (reference_lines, native_lines):
                self.assertEqual(
                    [line for line in lines if line.isdigit()],
                    [str(index) for index in range(20)],
                )
                self.assertEqual(lines.count("handled"), 1)
                self.assertEqual(lines.count("reply delivered"), 1)
                self.assertEqual(lines.count("complete"), 1)
                self.assertLess(lines.index("handled"), lines.index("reply delivered"))
                self.assertLess(lines.index("reply delivered"), lines.index("19"))
                self.assertGreater(lines.index("complete"), lines.index("19"))
            self.assertEqual(
                [line for line in reference_lines if line in {"handled", "reply delivered", "complete"}],
                [line for line in native_lines if line in {"handled", "reply delivered", "complete"}],
            )

    def test_native_bare_await_reply_preserves_try_receive_value(self) -> None:
        source = '''actor Worker {
    on get(reply: ReplySender<Int>) { reply.send(27) }
}
async fn query() {
    let worker = spawn(Worker())
    let pending = worker.request(get)
    await pending
    match pending.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("pending")
        Err(error) => println(error)
    }
}
fn main() {
    let task = query()
    match runTasks() {
        Ok(_) => println("finished")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-await-bare-reply-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "await-bare-reply", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "27\nfinished\n")

    def test_native_actor_stop_and_supervisor_restart_cancel_owned_tasks(self) -> None:
        source = '''async fn delayed() {
    await sleepFor(seconds(3))
    println("late")
}
actor Worker {
    on launch(reply: ReplySender<Int>) {
        let task = delayed()
        reply.send(1)
    }
    on fail(reply: ReplySender<Int>) {
        let task = delayed()
        reply.send(2)
        let failure: Float = 1 / 0
    }
}
async fn exercise() {
    let worker = spawn(Worker())
    let launched = worker.request(launch)
    let launch_value = await launched
    println(launch_value)
    worker.stop()

    let supervisor = oneForOne(2, 1000)
    let crashing = supervisor.start(Worker())
    let crashed = crashing.request(fail)
    let crash_value = await crashed
    println(crash_value)
    println(crashing.isAlive())
    crashing.stop()
}
fn main() {
    let task = exercise()
    let result = runTasks()
    match result {
        Ok(_) => println("tasks finished")
        Err(_) => println("actor failure observed")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-task-owner-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actor-task-owner", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "1\n2\ntrue\ntasks finished\n")
            self.assertNotIn("late", native.stdout)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_actor_crash_and_supervisor_restart_drain_pending_accept(self) -> None:
        source = '''async fn waitForAccept(ready: ReplySender<Int>) {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(_) => ready.send(-1)
        Ok(listener) => match listener.localAddress() {
            Err(_) => { ready.send(-1); listener.close() }
            Ok(address) => {
                ready.send(address.port())
                let pending = listener.accept()
                let accepted = await move pending
                match move accepted {
                    Err(_) => {}
                    Ok(connection) => connection.close()
                }
                listener.close()
            }
        }
    }
}
actor Acceptor {
    on start(ready: ReplySender<Int>) {
        let task = waitForAccept(ready)
    }
    on fail() {
        let failure: Float = 1 / 0
    }
}
actor Barrier {
    on ping(reply: ReplySender<Int>) { reply.send(1) }
}
async fn isPortReleased(port: Int) -> Bool {
    let opened = await tcpListen("127.0.0.1", port, 8)
    match move opened {
        Err(_) => return false
        Ok(listener) => {
            listener.close()
            return true
        }
    }
}
async fn exercise() {
    let barrier = spawn(Barrier())
    let unmanaged = spawn(Acceptor())
    let started = unmanaged.request(start)
    let port: Int = await started
    unmanaged.fail()
    let unmanagedBarrier = barrier.request(ping)
    let unmanagedAck: Int = await unmanagedBarrier
    let unmanagedReleased = await isPortReleased(port)
    println(unmanagedReleased)

    let supervisor = oneForOne(2, 1000)
    let managed = supervisor.start(Acceptor(), permanentChildPolicy())
    let supervisedStart = managed.request(start)
    let supervisedPort: Int = await supervisedStart
    managed.fail()
    let supervisedBarrier = barrier.request(ping)
    let supervisedAck: Int = await supervisedBarrier
    let supervisedReleased = await isPortReleased(supervisedPort)
    println(supervisedReleased)
}
fn main() {
    let task = exercise()
    match runTasks() {
        Ok(_) => println("tasks finished")
        Err(_) => println("actor failure observed")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-failure-network-cleanup-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actor-failure-network-cleanup", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "true\ntrue\nactor failure observed\n")

    def test_native_actor_crash_diagnostics_redact_secret_arguments(self) -> None:
        source = '''actor Broken {
    on fail() {
        match environment("TYVERA_NATIVE_ACTOR_SECRET") {
            Some(token) => {
                let failure: Float = 1 / 0
            }
            None => {}
        }
    }
}
fn main() {
    let broken = spawn(Broken())
    broken.fail()
    match runActors() {
        Ok(_) => println("unexpected success")
        Err(reason) => eprintln(reason)
    }
}
'''
        secret = "fake-actor-secret-must-not-appear"
        with patch.dict(os.environ, {"TYVERA_NATIVE_ACTOR_SECRET": secret}):
            with tempfile.TemporaryDirectory(prefix="tyvera-native-secret-actor-") as temporary:
                root = Path(temporary)
                reference = self._reference_result(source, root)
                native = self._native_source_result(source, root, root / "secret-actor", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
                self.assertNotIn(secret, native.stdout + native.stderr)
                self.assertNotIn(secret, reference[1] + reference[2])

    def test_native_actor_stop_waits_for_pending_accept_cleanup(self) -> None:
        source = '''async fn waitForAccept(ready: ReplySender<Int>) {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(_) => ready.send(-1)
        Ok(listener) => match listener.localAddress() {
            Err(_) => { ready.send(-1); listener.close() }
            Ok(address) => {
                ready.send(address.port())
                let pending = listener.accept()
                let accepted = await move pending
                match move accepted {
                    Err(_) => {}
                    Ok(connection) => connection.close()
                }
                listener.close()
            }
        }
    }
}
actor Acceptor {
    on start(ready: ReplySender<Int>) {
        let task = waitForAccept(ready)
    }
}
async fn checkOne() -> Bool {
    let worker = spawn(Acceptor())
    let pending = worker.request(start)
    let port: Int = await pending
    if port <= 0 {
        worker.stop()
        return false
    }
    worker.stop()
    let reopened = await tcpListen("127.0.0.1", port, 8)
    match move reopened {
        Err(_) => return false
        Ok(listener) => {
            listener.close()
            return true
        }
    }
}
async fn verifyCleanup() {
    var count: Int = 0
    var released: Int = 0
    while count < 4 {
        let result = await checkOne()
        if result { released = released + 1 }
        count = count + 1
    }
    println(released)
}
fn main() {
    let task = verifyCleanup()
    match runTasks() {
        Ok(_) => println("tasks finished")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-network-cleanup-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actor-network-cleanup", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "4\ntasks finished\n")

    def test_native_repeated_actor_reply_wakeup_matches_reference(self) -> None:
        source = '''actor Worker {
    on get(value: Int, reply: ReplySender<Int>) { reply.send(value + 1) }
}
async fn query() {
    let worker = spawn(Worker())
    var index: Int = 0
    var total: Int = 0
    while index < 48 {
        let pending = worker.request(get, index)
        let result = await pending
        total = total + result
        index = index + 1
    }
    println(total)
}
fn main() {
    let task = query()
    match runTasks() {
        Ok(_) => println("finished")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-reply-wakeup-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "reply-wakeup", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "1176\nfinished\n")

    def test_native_await_reply_timeout_and_deadlock_match_reference(self) -> None:
        programs = (
            '''actor Worker {
    on get(reply: ReplySender<Int>) { reply.send(19) }
}
async fn query() {
    let worker = spawn(Worker())
    let pending = worker.requestWithin(milliseconds(0), get)
    let value = await pending
    println(value)
}
fn main() {
    let task = query()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(error) => println(error)
    }
}
''',
            '''actor Worker {
    on ignore(reply: ReplySender<Int>) {}
}
async fn query() {
    let worker = spawn(Worker())
    let pending = worker.request(ignore)
    let value = await pending
    println(value)
}
fn main() {
    let task = query()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(error) => println(error)
    }
}
''',
        )
        with tempfile.TemporaryDirectory(prefix="tyvera-native-await-reply-errors-") as temporary:
            root = Path(temporary)
            for index, source in enumerate(programs):
                with self.subTest(index=index):
                    reference = self._reference_result(source, root)
                    native = self._native_source_result(source, root, root / f"await-reply-error-{index}", root)
                    self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_await_reply_actor_crash_matches_reference(self) -> None:
        source = '''actor Broken {
    on get(reply: ReplySender<Int>) {
        let failure: Float = 1 / 0
        reply.send(9)
    }
}
async fn query() {
    let worker = spawn(Broken())
    let pending = worker.request(get)
    let value = await pending
    println(value)
}
fn main() {
    let task = query()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-await-reply-crash-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "await-reply-crash", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_actor_stop_fails_pending_reply_like_reference(self) -> None:
        source = '''actor Worker {
    on get(reply: ReplySender<Int>) { reply.send(9) }
}
fn main() {
    let worker = spawn(Worker())
    let response = worker.request(get)
    worker.stop()
    match response.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("pending")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-reply-stop-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "actor stopped before replying\n")

    def test_native_actor_crash_fails_pending_reply_like_reference(self) -> None:
        source = '''actor Broken {
    on get(reply: ReplySender<Int>) {
        let failure: Float = 1 / 0
        reply.send(9)
    }
}
fn main() {
    let worker = spawn(Broken())
    let response = worker.request(get)
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
    match response.tryReceive() {
        Ok(Some(value)) => println(value)
        Ok(None) => println("pending")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-reply-crash-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertIn("actor 'Broken' crashed at native-io.tyv:", native.stdout)
            self.assertEqual(native.stdout.splitlines()[0], native.stdout.splitlines()[1])

    def test_native_imported_actor_matches_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-imported-actor-") as temporary:
            root = Path(temporary)
            (root / "counter.tyv").write_text(
                "pub actor Counter {\n"
                "    var value: Int\n"
                "    init(start: Int) { self.value = start }\n"
                "    on report() { println(self.value) }\n"
                "}\n",
                encoding="utf-8",
            )
            source = root / "main.tyv"
            source.write_text(
                'import "counter.tyv" as counters\n'
                'fn main() {\n'
                '    let counter: ActorRef<counters.Counter> = spawn(counters.Counter(7))\n'
                '    counter.report()\n'
                '    match runActors() {\n'
                '        Ok(count) => println(count)\n'
                '        Err(reason) => println(reason)\n'
                '    }\n'
                '}\n',
                encoding="utf-8",
            )
            checked = compile_checked_project(source, root)
            reference_output = io.StringIO()
            reference_errors = io.StringIO()
            ReferenceInterpreterBackend().execute(
                checked, output=reference_output, error_output=reference_errors,
                project_root=root,
            )
            CppNativeBackend().build(
                lower_checked_program(checked), root / "imported-actor", project_root=root,
            )
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            if os.name != "nt":
                environment["PATH"] = ""
            native = subprocess.run(
                [str(root / "imported-actor")], cwd=root, capture_output=True,
                text=True, encoding="utf-8", env=environment, check=False,
            )
            self.assertEqual(
                (native.returncode, native.stdout, native.stderr),
                (0, reference_output.getvalue(), reference_errors.getvalue()),
            )
            self.assertEqual(native.stdout, "7\n1\n")

    def test_native_supervisor_restart_strategies_match_reference(self) -> None:
        worker = '''actor Worker {
    var value: Int
    init(start: Int) { self.value = start }
    on bump() { self.value = self.value + 1 }
    on fail() { let failure: Float = 1 / 0 }
    on report() { println(self.value) }
}
'''
        programs = [
            worker + '''fn main() {
    let supervisor = oneForOne(3, 1000)
    let first = supervisor.start(Worker(10))
    let second = supervisor.start(Worker(20))
    first.bump()
    second.bump()
    first.fail()
    match runActors() {
        Ok(_) => println("recovered")
        Err(error) => println(error)
    }
    first.report()
    second.report()
    runActors()
}
''',
            worker + '''fn main() {
    let supervisor = oneForAll(3, 1000)
    let first = supervisor.start(Worker(10))
    let second = supervisor.start(Worker(20))
    first.bump()
    second.bump()
    first.fail()
    runActors()
    first.report()
    second.report()
    runActors()
}
''',
            worker + '''fn main() {
    let supervisor = restForOne(3, 1000)
    let first = supervisor.start(Worker(10))
    let second = supervisor.start(Worker(20))
    let third = supervisor.start(Worker(30))
    first.bump()
    second.fail()
    third.bump()
    runActors()
    first.report()
    second.report()
    third.report()
    runActors()
}
''',
        ]
        with tempfile.TemporaryDirectory(prefix="tyvera-native-supervisor-") as temporary:
            root = Path(temporary)
            for index, source in enumerate(programs):
                with self.subTest(strategy=index):
                    reference = self._reference_result(source, root)
                    native = self._native_source_result(source, root, root / f"supervisor-{index}", root)
                    self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_supervisor_policies_and_nested_restart_match_reference(self) -> None:
        source = '''actor Worker {
    var value: Int
    init(start: Int) { self.value = start }
    on fail() { let failure: Float = 1 / 0 }
    on report() { println(self.value) }
}
fn main() {
    let root = oneForOne(3, 1000)
    let nested = oneForOne(0, 1000)
    let child = nested.start(Worker(17), transientChildPolicy())
    root.start(nested)
    child.fail()
    match runActors() {
        Ok(_) => println("recovered")
        Err(error) => println(error)
    }
    println(nested.isAlive())
    println(child.isAlive())
    child.report()
    runActors()
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-supervisor-tree-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "supervisor-tree", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_supervisor_restart_intensity_matches_reference(self) -> None:
        source = '''actor Worker { on fail() { let failure: Float = 1 / 0 } }
fn main() {
    let supervisor = oneForOne(0, 1000)
    let child = supervisor.start(Worker())
    child.fail()
    match runActors() {
        Ok(_) => println("unexpected")
        Err(error) => println(error)
    }
    println(supervisor.isAlive())
    println(child.isAlive())
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-supervisor-limit-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "supervisor-limit", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_supervisor_restart_window_expires_during_backoff(self) -> None:
        source = '''actor Worker {
    var value: Int
    init(start: Int) { self.value = start }
    on fail() { let failure: Float = 1 / 0 }
    on report() { println(self.value) }
}
fn main() {
    let supervisor = oneForOne(1, 2, exponentialBackoff(10, 10))
    let child = supervisor.start(Worker(31))
    child.fail()
    runActors()
    child.fail()
    match runActors() {
        Ok(_) => println("recovered")
        Err(error) => println(error)
    }
    child.report()
    runActors()
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-supervisor-window-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "supervisor-window", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    @unittest.skipIf(os.name == "nt", "native signal shutdown requires POSIX signals")
    def test_native_sigterm_cancels_supervisor_backoff_and_shuts_down(self) -> None:
        source = '''actor Worker { on work() {} }
fn main() {
    let supervisor = oneForOne(2, 1000, exponentialBackoff(2000, 2000))
    let child = supervisor.start(Worker())
    child.stop()
    eprintln("waiting for restart")
    runActors()
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-supervisor-signal-") as temporary:
            root = Path(temporary)
            checked = compile_checked_source(source, "native-supervisor-signal.tyv")
            output = root / "supervisor-signal"
            CppNativeBackend().build(lower_checked_program(checked), output, project_root=root)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment["PATH"] = ""
            process = subprocess.Popen(
                [str(output)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=environment,
            )
            try:
                assert process.stderr is not None
                self.assertEqual(process.stderr.readline().strip(), "waiting for restart")
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 143)
                self.assertEqual(stdout, "")
                self.assertIn("runtime shutdown", stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=4)

    def test_native_supervisor_backoff_is_monotonic_and_deferred_until_dispatch(self) -> None:
        source = '''actor Worker {
    var value: Int
    init(start: Int) { self.value = start }
    on report() { println(self.value) }
}
fn main() {
    let supervisor = oneForOne(2, 1000, exponentialBackoff(1, 4))
    let child = supervisor.start(Worker(23))
    eprintln("backoff checkpoint: before-stop")
    child.stop()
    eprintln("backoff checkpoint: after-stop")
    println(child.isAlive())
    eprintln("backoff checkpoint: before-first-dispatch")
    runActors()
    eprintln("backoff checkpoint: after-first-dispatch")
    println(child.isAlive())
    child.report()
    eprintln("backoff checkpoint: before-second-dispatch")
    runActors()
    eprintln("backoff checkpoint: after-second-dispatch")
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-supervisor-backoff-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            try:
                native = self._native_source_result(source, root, root / "supervisor-backoff", root)
            except subprocess.TimeoutExpired as error:
                progress = error.stderr or ""
                if isinstance(progress, bytes):
                    progress = progress.decode("utf-8", errors="replace")
                checkpoints = [
                    marker for marker in (
                        "before-stop", "after-stop", "before-first-dispatch",
                        "after-first-dispatch", "before-second-dispatch", "after-second-dispatch",
                    ) if marker in progress
                ]
                self.fail(f"native supervisor backoff timed out; reached checkpoints: {checkpoints}")
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_actor_crash_is_isolated_and_reported_like_reference(self) -> None:
        source = '''actor Broken {
    on fail() { let failure: Float = 1 / 0 }
}
actor Healthy {
    on work() { println("healthy") }
}
fn main() {
    let broken = spawn(Broken())
    let healthy = spawn(Healthy())
    broken.fail()
    broken.fail()
    healthy.work()
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
    println(broken.isAlive())
    println(healthy.isAlive())
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-crash-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_native_actor_links_propagate_crashes_and_supervise_like_reference(self) -> None:
        source = """actor Worker {
    on fail() { let failure: Float = 1 / 0 }
}
fn main() {
    let supervisor = oneForOne(5, 5000)
    let first = supervisor.start(Worker())
    let second = supervisor.start(Worker())
    let monitor = second.monitor()
    println(first.link(second))
    first.fail()
    runActors()
    println(first.isAlive())
    println(second.isAlive())
    println(monitor.isReady())
    match monitor.tryReceive() {
        Ok(Some(reason)) => println("linked crash")
        Ok(None) => println("pending")
        Err(error) => println(error)
    }
    println(second.unlink(first))
}
"""
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-links-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "true\ntrue\ntrue\ntrue\nlinked crash\nfalse\n")

    def test_native_transitive_actor_links_match_root_crash_across_supervisors(self) -> None:
        source = """actor Origin {
    on fail() { let failure: Float = 1 / 0 }
}
actor Middle { on work() {} }
actor Leaf { on work() {} }
fn main() {
    let firstSupervisor = oneForOne(4, 5000)
    let secondSupervisor = oneForOne(4, 5000)
    let origin = firstSupervisor.start(Origin())
    let middle = secondSupervisor.start(Middle())
    let leaf = secondSupervisor.start(Leaf())
    let middleMonitor = middle.monitor()
    let leafMonitor = leaf.monitor()
    println(origin.link(middle))
    println(middle.link(leaf))
    origin.fail()
    match runActors() {
        Ok(_) => println("drained")
        Err(_) => println("failure observed")
    }
    println(origin.isAlive())
    println(middle.isAlive())
    println(leaf.isAlive())
    match middleMonitor.tryReceive() {
        Ok(Some(reason)) => println(reason)
        Ok(None) => println("middle pending")
        Err(error) => println(error)
    }
    match leafMonitor.tryReceive() {
        Ok(Some(reason)) => println(reason)
        Ok(None) => println("leaf pending")
        Err(error) => println(error)
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="tyvera-native-transitive-links-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            lines = native.stdout.splitlines()
            self.assertEqual(sum("because linked actor 'Origin' failed:" in line for line in lines), 2)
            self.assertEqual(lines[:6], ["true", "true", "drained", "true", "true", "true"])

    def test_native_actor_monitor_stop_and_demonitor_match_reference(self) -> None:
        source = """actor Worker { on work() {} }
fn main() {
    let worker = spawn(Worker())
    let monitor = worker.monitor()
    worker.stop()
    println(monitor.isReady())
    match monitor.tryReceive() {
        Ok(Some(reason)) => println(reason)
        Ok(None) => println("pending")
        Err(error) => println(error)
    }
    println(monitor.demonitor())
    let stopped = worker.monitor()
    println(stopped.isReady())
    let cancellable = spawn(Worker()).monitor()
    println(cancellable.demonitor())
    println(cancellable.isCancelled())
}
"""
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-monitor-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "true\nnormal stop\nfalse\ntrue\ntrue\ntrue\n")

    def test_native_actor_monitor_awaits_crash_reason_like_reference(self) -> None:
        source = """actor Broken {
    on fail() { let failure: Float = 1 / 0 }
}
async fn observe(monitor: ReplyReceiver<String>) {
    let reason: String = await monitor
    println(reason)
}
fn main() {
    let broken = spawn(Broken())
    let monitor = broken.monitor()
    let waiter = observe(monitor)
    broken.fail()
    runActors()
    runTasks()
}
"""
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-monitor-crash-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertIn("actor 'Broken' crashed at native-io.tyv:", native.stdout)
            self.assertTrue(native.stdout.endswith("division by zero\n"))

    def test_native_actor_stop_clears_pending_messages_and_rejects_later_send(self) -> None:
        source = '''actor Worker {
    on work() { println("unexpected") }
}
fn main() {
    let worker = spawn(Worker())
    worker.work()
    worker.stop()
    println(worker.isAlive())
    worker.work()
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-stop-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "false\n")

    def test_native_runtime_retains_actors_after_actor_ref_scope_ends(self) -> None:
        source = '''actor Worker { on ping() { println("alive") } }
fn main() {
    {
        let worker = spawn(Worker())
        worker.ping()
    }
    match runActors() {
        Ok(_) => println("complete")
        Err(error) => println(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-runtime-owner-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.stdout, "alive\ncomplete\n")

    def test_native_actor_mailbox_capacity_matches_reference(self) -> None:
        source = '''actor Worker { on work() {} }
fn main() {
    let worker = spawn(Worker())
    var index = 0
    while index < 100001 {
        worker.work()
        index = index + 1
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-actor-mailbox-limit-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "actors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
            self.assertEqual(native.returncode, 1)
            self.assertIn("actor mailbox capacity (100000) exceeded", native.stderr)

    def test_native_duration_conversions_match_reference(self) -> None:
        source = '''fn main() {
    println(milliseconds(-2).inMilliseconds())
    println((seconds(2) - milliseconds(500)).inMilliseconds())
    match durationFromSeconds(0.0001) {
        Ok(value) => println(value.inMilliseconds())
        Err(error) => println(error)
    }
    match durationFromSeconds(-1.0) {
        Ok(_) => println("unexpected duration")
        Err(error) => println(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-duration-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "duration-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "-2\n1500\n1\nDuration seconds must be finite and non-negative\n",
            )

    @unittest.skipUnless(
        sys.platform.startswith("linux") and HAS_CPP_COMPILER,
        "native TLS tests require Linux, OpenSSL, and a C++17 compiler",
    )
    def test_native_tls_verifies_certificates_and_transfers_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-tls-") as temporary:
            root = Path(temporary)
            certificate = root / "server-cert.pem"
            private_key = root / "server-key.pem"
            unrelated_certificate = root / "unrelated-cert.pem"
            unrelated_key = root / "unrelated-key.pem"
            openssl = shutil.which("openssl")
            if openssl is None:
                self.skipTest("OpenSSL command line tools are unavailable")
            for cert, key in ((certificate, private_key), (unrelated_certificate, unrelated_key)):
                subprocess.run(
                    [
                        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "2",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certificate, private_key)

            class EchoHandler(socketserver.BaseRequestHandler):
                def handle(self) -> None:
                    payload = self.request.recv(64)
                    if payload:
                        self.request.sendall(payload.upper())

            class LoopbackTlsServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

                def get_request(self):
                    connection, address = super().get_request()
                    try:
                        return server_context.wrap_socket(connection, server_side=True), address
                    except OSError:
                        connection.close()
                        raise

                def handle_error(self, request, client_address) -> None:
                    return

            server = LoopbackTlsServer(("127.0.0.1", 0), EchoHandler)
            server_thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            server_thread.start()
            port = server.server_address[1]
            source = f'''async fn connect(host: String, port: Int, echo: Bool) {{
    let opened = await tlsConnect(host, port)
    using connection = move opened {{
        if echo {{
            let writing = connection.writeAll(bytesFromString("ping"))
            let written = await writing
            match written {{
                Ok(count) => println(count)
                Err(error) => println(error)
            }}
            let reading = connection.read(16)
            let received = await reading
            match received {{
                Ok(Some(bytes)) => match bytes.toString() {{
                    Ok(text) => println(text)
                    Err(error) => println(error)
                }}
                Ok(None) => println("unexpected EOF")
                Err(error) => println(error)
            }}
        }} else {{
            println("unexpected trusted connection")
        }}
    }} else error {{
        println(error)
    }}
}}
fn main() {{
    let trusted = connect("127.0.0.1", {port}, true)
    match runTasks() {{
        Ok(_) => println("TLS exchange finished")
        Err(error) => println(error)
    }}
    let wrong_host = connect("localhost", {port}, false)
    match runTasks() {{
        Ok(_) => println("TLS verification check finished")
        Err(error) => println(error)
    }}
}}'''
            untrusted_source = f'''async fn connect() {{
    let opened = await tlsConnect("127.0.0.1", {port})
    using connection = move opened {{
        connection.close()
        println("unexpected trusted connection")
    }} else error {{
        println(error)
    }}
}}
fn main() {{
    let task = connect()
    match runTasks() {{
        Ok(_) => println("untrusted TLS check finished")
        Err(error) => println(error)
    }}
}}'''
            try:
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(certificate)}):
                    expected = self._reference_result(source, root)
                    native = self._native_source_result(
                        source, root, root / "tls-client", root,
                    )
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(
                    native.stdout,
                    "4\nPING\nTLS exchange finished\nTLS certificate verification failed\n"
                    "TLS verification check finished\n",
                )
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(unrelated_certificate)}):
                    expected_untrusted = self._reference_result(untrusted_source, root)
                    native_untrusted = self._native_source_result(
                        untrusted_source, root, root / "tls-untrusted-client", root,
                    )
                self.assertEqual(
                    (native_untrusted.returncode, native_untrusted.stdout, native_untrusted.stderr),
                    expected_untrusted,
                )
                self.assertEqual(
                    native_untrusted.stdout,
                    "TLS certificate verification failed\nuntrusted TLS check finished\n",
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=1)

    def test_native_task_group_registers_cancels_and_joins_children(self) -> None:
        source = '''async fn waitLong() {
    await sleepFor(seconds(60))
}
async fn exercise() {
    let group = taskGroup()
    let first = waitLong()
    let second = waitLong()
    println(group.add(first))
    println(group.add(first))
    println(group.add(second))
    println(group.isDone())
    println(group.cancel())
    await group
    println(group.isDone())
}
fn main() {
    let pending = exercise()
    match runTasks() {
        Ok(_) => println("complete")
        Err(error) => println(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-group-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "task-group-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\nfalse\ntrue\nfalse\n2\ntrue\ncomplete\n")

    def test_native_task_group_enforces_creator_ownership_and_matches_reference(self) -> None:
        source = '''async fn misuse(group: TaskGroup) {
    println(group.isDone())
}
async fn owner() {
    let group = taskGroup()
    let pending = misuse(group)
    await pending
    println("unreachable")
}
fn main() {
    let pending = owner()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(error) => println(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-group-owner-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "task-group-owner-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertIn("TaskGroup may only be used by its creating task", native.stdout)

    def test_native_root_task_group_tracks_root_task_completion(self) -> None:
        source = '''async fn quick() {
    await sleepFor(milliseconds(0))
}
fn main() {
    let group = taskGroup()
    let pending = quick()
    println(group.add(pending))
    match runTasks() {
        Ok(_) => {}
        Err(error) => println(error)
    }
    println(group.isDone())
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-root-task-group-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "root-task-group-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\ntrue\n")

    def test_native_task_registry_allows_more_than_64_sequential_tasks(self) -> None:
        source = '''async fn pulse() {
    await sleepFor(milliseconds(0))
}
async fn runMany() {
    var completed = 0
    while completed < 80 {
        let pending = pulse()
        await pending
        completed = completed + 1
    }
    println(completed)
}
fn main() {
    let task = runMany()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-registry-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "task-registry-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "80\n")

    def test_native_task_registry_limits_unfinished_tasks(self) -> None:
        source = '''async fn waitLong() {
    await sleepFor(seconds(60))
}
async fn createTooMany() {
    var created = 0
    while created < 80 {
        let pending = waitLong()
        created = created + 1
    }
}
fn main() {
    let task = createTooMany()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-active-limit-") as temporary:
            root = Path(temporary)
            native = self._native_source_result(source, root, root / "task-active-limit-app", root)
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(native.stdout, "")
            self.assertIn("native task capacity exceeded (maximum 64 unfinished tasks per run)", native.stderr)

    def test_native_task_timeout_cancels_source_and_matches_reference(self) -> None:
        source = '''async fn background() {
    while true {}
}
fn main() {
    let source = background()
    let timed = source.withTimeout(milliseconds(20))
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("timed out")
    }
    println(source.isCancelled())
    println(timed.isDone())
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timeout-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "timeout-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "timed out\ntrue\ntrue\n")

    def test_native_negative_timeout_prevents_unstarted_source_body(self) -> None:
        source = '''async fn sideEffect() {
    println("source started")
}
fn main() {
    let source = sideEffect()
    let timed = source.withTimeout(milliseconds(-1))
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("task timed out")
    }
    println(source.isCancelled())
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-negative-timeout-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "negative-timeout-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "task timed out\ntrue\n")

    def test_native_completed_task_wins_over_an_expired_timeout(self) -> None:
        source = '''async fn immediate() -> Int { return 7 }
async fn run() {
    let original = immediate()
    let first = await original
    let timed = original.withTimeout(milliseconds(-1))
    let second = await timed
    println(first + second)
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => println("ok")
        Err(_) => println("failed")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timeout-complete-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "timeout-complete", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "14\nok\n")

    def test_native_cancelling_timeout_wrapper_cancels_source(self) -> None:
        source = '''async fn background() {
    while true {}
}
fn main() {
    let source = background()
    let timed = source.withTimeout(seconds(10))
    println(timed.cancel())
    println(source.isCancelled())
    match runTasks() {
        Ok(_) => println("cancelled")
        Err(_) => println("failed")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timeout-cancel-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "timeout-cancel", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\ntrue\ncancelled\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking timeout tests require Linux")
    def test_native_timeout_cancels_pending_accept_and_matches_reference(self) -> None:
        source = '''async fn waitForPeer() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    using listener = move opened {
        let pending = listener.accept()
        let timed = pending.withTimeout(milliseconds(30))
        let accepted = await move timed
        match move accepted {
            Ok(connection) => connection.close()
            Err(error) => println(error)
        }
    } else error {
        println(error)
    }
}
fn main() {
    let task = waitForPeer()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("timed out")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-accept-timeout-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "accept-timeout", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "timed out\n")

    def test_native_owned_child_task_cancellation_matches_reference(self) -> None:
        source = '''async fn background() {
    while true {}
}
async fn parent() {
    let child = background()
    println(child.cancel())
    println(child.isCancelled())
    println(child.isDone())
}
fn main() {
    let task = parent()
    match runTasks() {
        Ok(_) => println("ok")
        Err(error) => println(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-child-cancel-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "child-cancel", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\ntrue\ntrue\nok\n")

    def test_native_byte_buffer_binary_and_text_io_match_reference(self) -> None:
        source = '''
fn mutate(buffer: ByteBuffer, value: Byte) {
    let alias = buffer
    match alias.set(1, value) {
        Ok(_) => println("mutated")
        Err(error) => eprintln(error)
    }
}
fn relay(opened: Result<TextFile,String>) -> Result<TextFile,String> {
    return move opened
}
fn main() {
    match byteFromInt(255) {
        Err(error) => eprintln(error)
        Ok(value) => match byteBuffer(3) {
            Err(error) => eprintln(error)
            Ok(buffer) => {
                let alias = buffer
                mutate(alias, value)
                match buffer.get(1) {
                    Err(error) => eprintln(error)
                    Ok(changed) => println(changed.toInt())
                }
                match buffer.toBytes() {
                    Err(error) => eprintln(error)
                    Ok(snapshot) => {
                        match writeBytes("payload.bin", snapshot) {
                            Err(error) => eprintln(error)
                            Ok(_) => match readBytes("payload.bin") {
                                Err(error) => eprintln(error)
                                Ok(contents) => println(contents.length())
                            }
                        }
                    }
                }
            }
        }
    }
    match readBytes("input.bin") {
        Err(error) => eprintln(error)
        Ok(contents) => {
            println(contents.length())
            match writeBytes(pathJoin("nested", "copy.bin"), contents) {
                Err(error) => eprintln(error)
                Ok(_) => println(pathExists("nested/copy.bin"))
            }
        }
    }
    match readText("seed.txt") {
        Err(error) => eprintln(error)
        Ok(contents) => println(contents)
    }
    match writeText("written.txt", "written") {
        Err(error) => eprintln(error)
        Ok(_) => match readText("written.txt") {
            Err(error) => eprintln(error)
            Ok(contents) => println(contents)
        }
    }
    let transferred = relay(openText("resource.txt", "write"))
    match move transferred {
        Err(error) => eprintln(error)
        Ok(file) => {
            match file.write("resource") {
                Err(error) => eprintln(error)
                Ok(_) => match file.readAll() {
                    Err(error) => println(error)
                    Ok(_) => println("unexpected")
                }
            }
            println(file.close())
            println(file.isOpen())
        }
    }
    using writer = openTextWriter("scoped.txt", "write") {
        match writer.write("scoped") {
            Err(error) => eprintln(error)
            Ok(_) => match writer.flush() {
                Err(error) => eprintln(error)
                Ok(_) => println("flushed")
            }
        }
    } else error {
        eprintln(error)
    }
    using reader = openTextReader("scoped.txt") {
        match reader.readAll() {
            Err(error) => eprintln(error)
            Ok(contents) => println(contents)
        }
    } else error {
        eprintln(error)
    }
    using appender = openTextWriter("scoped.txt", "append") {
        match appender.append("-more") {
            Err(error) => eprintln(error)
            Ok(_) => println("appended")
        }
    } else error {
        eprintln(error)
    }
    match readText("scoped.txt") {
        Err(error) => eprintln(error)
        Ok(contents) => println(contents)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-io-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                (root / "nested").mkdir(parents=True)
                (root / "input.bin").write_bytes(b"\x00\xff\x2a")
                (root / "seed.txt").write_bytes(b"first\r\nsecond\r\n")
            expected = self._reference_result(source, roots[0])
            native = self._native_source_result(source, roots[1], workspace / "native-app", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual((roots[0] / "payload.bin").read_bytes(), (roots[1] / "payload.bin").read_bytes())
            self.assertEqual((roots[1] / "payload.bin").read_bytes(), b"\x00\xff\x00")
            self.assertEqual((roots[0] / "nested" / "copy.bin").read_bytes(), (roots[1] / "nested" / "copy.bin").read_bytes())
            for name in ("written.txt", "resource.txt", "scoped.txt"):
                self.assertEqual((roots[0] / name).read_bytes(), (roots[1] / name).read_bytes())
            self.assertEqual((roots[1] / "scoped.txt").read_text(encoding="utf-8"), "scoped-more")

    def test_native_io_errors_buffer_bounds_and_secret_paths_match_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-io-errors-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                root.mkdir()
                (root / "bad-utf8.txt").write_bytes(b"\xff")
                (root / ".tyvera.secrets").write_text("TOKEN = \"must-not-appear\"\n", encoding="utf-8")
                (root / "directory").mkdir()
                (root / "nested").mkdir()
            error_source = '''fn main() {
    match byteBuffer(-1) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match byteBuffer(67108865) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match byteFromInt(256) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match byteBuffer(1) {
        Err(error) => println(error)
        Ok(buffer) => {
            match byteFromInt(0) {
                Err(error) => println(error)
                Ok(zero) => {
                    match buffer.get(-1) {
                        Err(error) => println(error)
                        Ok(_) => println("unexpected")
                    }
                    match buffer.get(1) {
                        Err(error) => println(error)
                        Ok(_) => println("unexpected")
                    }
                    match buffer.set(1, zero) {
                        Err(error) => println(error)
                        Ok(_) => println("unexpected")
                    }
                }
            }
        }
    }
    match readBytes("missing.bin") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    using reader = openTextReader("missing.txt") {
        println("unexpected")
    } else error {
        println(error)
    }
    using writer = openTextWriter("missing-parent/out.txt", "write") {
        println("unexpected")
    } else error {
        println(error)
    }
    using invalidMode = openTextWriter("ignored.txt", "read") {
        println("unexpected")
    } else error {
        println(error)
    }
    match readText("bad-utf8.txt") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText("directory") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match writeBytes("directory", bytesFromString("x")) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText(".tyvera.secrets") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText("./nested/../.tyvera.secrets") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText(@ABS_SECRET@) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match writeText(".tyvera.secrets", "overwritten") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match writeBytes(".tyvera.secrets", bytesFromString("overwritten")) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
}'''
            source_for = lambda root: error_source.replace(
                "@ABS_SECRET@", self._tyvera_string(str(root / ".tyvera.secrets")),
            )
            sources = (source_for(roots[0]), source_for(roots[1]))
            expected = self._reference_result(sources[0], roots[0])
            native = self._native_source_result(sources[1], roots[1], workspace / "native-errors", workspace)
            self.maxDiff = None
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertNotIn("must-not-appear", native.stdout + native.stderr)
            for root in roots:
                self.assertEqual((root / ".tyvera.secrets").read_text(encoding="utf-8"), 'TOKEN = "must-not-appear"\n')

            if os.name != "nt":
                for root in roots:
                    try:
                        (root / "secret-link").symlink_to(root / ".tyvera.secrets")
                    except OSError:
                        self.skipTest("test environment does not permit creating symlinks")
                symlink_source = '''fn main() {
    match readText("secret-link") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
}'''
                expected_link = self._reference_result(symlink_source, roots[0])
                native_link = self._native_source_result(
                    symlink_source, roots[1], workspace / "native-link", workspace,
                )
                self.assertEqual((native_link.returncode, native_link.stdout, native_link.stderr), expected_link)

    def test_native_byte_buffer_accepts_64_mib_and_rejects_the_next_byte(self) -> None:
        source = '''fn main() {
    match byteBuffer(67108864) {
        Err(error) => println(error)
        Ok(buffer) => println(buffer.length())
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-buffer-limit-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                root.mkdir()
            expected = self._reference_result(source, roots[0])
            native = self._native_source_result(source, roots[1], workspace / "native-limit", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)

    def test_native_binary_read_rejects_file_over_64_mib_like_reference(self) -> None:
        source = '''fn main() {
    match readBytes("oversized.bin") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-binary-limit-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                root.mkdir()
                with (root / "oversized.bin").open("wb") as stream:
                    stream.truncate(67_108_865)
            expected = self._reference_result(source, roots[0])
            native = self._native_source_result(source, roots[1], workspace / "native-binary-limit", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)

    def test_native_permission_errors_match_reference_when_enforced(self) -> None:
        if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0:
            self.skipTest("permission-denied fixture requires a non-root POSIX user")
        source = '''fn main() {
    match readBytes("restricted/private.bin") {
        Err(error) => println(error)
        Ok(_) => println("unexpected read")
    }
    match writeBytes("restricted/out.bin", bytesFromString("x")) {
        Err(error) => println(error)
        Ok(_) => println("unexpected write")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-permissions-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                restricted = root / "restricted"
                restricted.mkdir(parents=True)
                (restricted / "private.bin").write_bytes(b"private")
                restricted.chmod(0)
            try:
                expected = self._reference_result(source, roots[0])
                native = self._native_source_result(source, roots[1], workspace / "native-permissions", workspace)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            finally:
                for root in roots:
                    (root / "restricted").chmod(0o700)

    def test_native_cleanup_failure_does_not_replace_primary_failure(self) -> None:
        if not Path("/dev/full").exists():
            self.skipTest("cleanup failure fixture requires /dev/full")
        source = '''fn main() {
    using writer = openTextWriter("/dev/full", "write") {
        writer.write("buffered")
        let invalid = 1 / 0
    } else error {
        eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-cleanup-") as temporary:
            workspace = Path(temporary)
            root = workspace / "project"
            root.mkdir()
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, workspace / "native-cleanup", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)

    def test_native_examples_match_reference_interpreter_without_python_path(self) -> None:
        examples = (
            "native_hello.tyv", "native_control_flow.tyv", "native_values.tyv",
            "native_sum_types.tyv", "native_collections.tyv", "native_bytes.tyv",
            "byte_buffers.tyv",
        )
        if sys.platform.startswith("linux"):
            examples += ("tcp_echo.tyv",)
        with tempfile.TemporaryDirectory(prefix="tyvera-native-e2e-") as temporary:
            for name in examples:
                with self.subTest(example=name):
                    source = REPOSITORY / "examples" / name
                    checked = compile_checked_project(source, REPOSITORY)
                    expected = io.StringIO()
                    ReferenceInterpreterBackend().execute(
                        checked, output=expected, error_output=io.StringIO(), project_root=REPOSITORY,
                    )
                    result = self._build_and_run(source, Path(temporary) / Path(name).stem)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected.getvalue())
                    self.assertEqual(result.stderr, "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_dns_success_and_failure_match_reference(self) -> None:
        source = '''async fn lookups() {
    let local = await resolveHost("localhost", 80)
    match local {
        Ok(addresses) => println(addresses.length > 0)
        Err(error) => println(error)
    }
    let invalid = await resolveHost("not a valid host name", 80)
    match invalid {
        Ok(_) => println("unexpected DNS success")
        Err(error) => println(error)
    }
}
fn main() {
    let task = lookups()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-dns-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "dns-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\nDNS resolution failed\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_task_cancellation_stops_pending_accept_and_matches_reference(self) -> None:
        source = '''async fn cancelPendingAccept() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println(error)
        Ok(listener) => {
            let pending = listener.accept()
            println(pending.cancel())
            println(pending.isCancelled())
            println(pending.isDone())
            listener.close()
        }
    }
}
fn main() {
    let task = cancelPendingAccept()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-cancel-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "task-cancel", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\ntrue\ntrue\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_dns_and_tcp_argument_errors_match_reference(self) -> None:
        source = '''async fn invalidCalls() {
    let emptyHost = await resolveHost("", 80)
    match emptyHost {
        Ok(_) => println("unexpected")
        Err(error) => println(error)
    }
    let invalidPort = await resolveHost("localhost", -1)
    match invalidPort {
        Ok(_) => println("unexpected")
        Err(error) => println(error)
    }
    let invalidConnect = await tcpConnect("127.0.0.1", 0)
    match move invalidConnect {
        Ok(connection) => connection.close()
        Err(error) => println(error)
    }
    let invalidBacklog = await tcpListen("127.0.0.1", 0, 0)
    match move invalidBacklog {
        Ok(listener) => listener.close()
        Err(error) => println(error)
    }
}
fn main() {
    let task = invalidCalls()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-network-errors-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "network-errors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "invalid address\nport must be between 0 and 65535\n"
                "port must be between 1 and 65535\nbacklog must be between 1 and 4096\n",
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_connection_refusal_matches_reference(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''async fn connect() {{
    let result = await tcpConnect("127.0.0.1", {port})
    match move result {{
        Ok(connection) => {{
            connection.close()
            println("unexpected connection")
        }}
        Err(error) => println(error)
    }}
}}
fn main() {{
    let task = connect()
    match runTasks() {{
        Ok(_) => {{}}
        Err(error) => eprintln(error)
    }}
}}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-tcp-error-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "tcp-error-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "connection refused\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_concurrent_tcp_connections_match_reference(self) -> None:
        source = '''async fn client(port: Int, payload: String) -> String {
    let opened = await tcpConnect("127.0.0.1", port)
    match move opened {
        Err(error) => return error
        Ok(connection) => {
            let sent = await connection.writeAll(bytesFromString(payload))
            match sent {
                Err(error) => { connection.close(); return error }
                Ok(_) => {}
            }
            let received = await connection.read(16)
            match received {
                Err(error) => { connection.close(); return error }
                Ok(value) => match value {
                    None => { connection.close(); return "unexpected EOF" }
                    Some(bytes) => match bytes.toString() {
                        Err(error) => { connection.close(); return error }
                        Ok(text) => { connection.close(); return text }
                    }
                }
            }
        }
    }
}
async fn echo(opened: Result<TcpConnection,String>) {
    match move opened {
        Err(error) => println(error)
        Ok(connection) => {
            let received = await connection.read(16)
            match received {
                Err(error) => println(error)
                Ok(value) => match value {
                    None => println("unexpected EOF")
                    Some(bytes) => {
                        let sent = await connection.writeAll(bytes)
                        match sent {
                            Ok(_) => {}
                            Err(error) => println(error)
                        }
                    }
                }
            }
            connection.close()
        }
    }
}
async fn serve() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println(error)
        Ok(listener) => match listener.localAddress() {
            Err(error) => { println(error); listener.close() }
            Ok(address) => {
                let first = client(address.port(), "first")
                let second = client(address.port(), "second")
                let acceptedFirst = await listener.accept()
                let serviceFirst = echo(move acceptedFirst)
                let acceptedSecond = await listener.accept()
                let serviceSecond = echo(move acceptedSecond)
                await serviceFirst
                await serviceSecond
                let firstResponse = await first
                let secondResponse = await second
                println(firstResponse)
                println(secondResponse)
                listener.close()
            }
        }
    }
}
fn main() {
    let task = serve()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-multi-tcp-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "multi-tcp", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "first\nsecond\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native IPv6 networking tests require Linux")
    def test_native_ipv6_tcp_echo_and_eof_match_reference(self) -> None:
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError as error:
            self.skipTest(f"IPv6 loopback is unavailable: {error}")
        source = '''async fn client(port: Int) -> String {
    let opened = await tcpConnect("::1", port)
    using connection = move opened {
        let sent = await connection.writeAll(bytesFromString("x"))
        match sent {
            Err(error) => return error
            Ok(_) => {}
        }
        let echoed = await connection.read(1)
        match echoed {
            Err(error) => return error
            Ok(value) => match value {
                None => return "unexpected EOF"
                Some(bytes) => match bytes.toString() {
                    Err(error) => return error
                    Ok(text) => {
                        let ended = await connection.read(1)
                        match ended {
                            Err(error) => return error
                            Ok(result) => match result {
                                None => return text + "|EOF"
                                Some(_) => return "unexpected extra data"
                            }
                        }
                    }
                }
            }
        }
        return "incomplete client exchange"
    } else error {
        return "connect: " + error
    }
}
async fn serve() {
    let opened = await tcpListen("::1", 0, 8)
    using listener = move opened {
        match listener.localAddress() {
            Err(error) => println(error)
            Ok(address) => {
                let clientTask = client(address.port())
                let accepted = await listener.accept()
                using connection = move accepted {
                    let received = await connection.read(1)
                    match received {
                        Err(error) => println(error)
                        Ok(value) => match value {
                            None => println("unexpected server EOF")
                            Some(bytes) => {
                                let echoed = await connection.writeAll(bytes)
                                match echoed {
                                    Ok(_) => {}
                                    Err(error) => println(error)
                                }
                            }
                        }
                    }
                } else error {
                    println(error)
                }
                let response = await clientTask
                println(address.family())
                println(response)
            }
        }
    } else error {
        println(error)
    }
}
fn main() {
    let server = serve()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-ipv6-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "ipv6-echo", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "IPv6\nx|EOF\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_tcp_resource_cleanup_on_scope_and_error_exit_matches_reference(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''async fn reserve(port: Int, fail: Bool) {{
    let pending = tcpListen("127.0.0.1", port, 8)
    let opened = await move pending
    using listener = move opened {{
        println(listener.isOpen())
        if fail {{
            let invalid = 1 / 0
        }}
    }} else error {{
        println(error)
    }}
}}
fn main() {{
    let normal = reserve({port}, false)
    match runTasks() {{
        Ok(_) => println("normal")
        Err(_) => println("normal failed")
    }}
    let failing = reserve({port}, true)
    match runTasks() {{
        Ok(_) => println("unexpected success")
        Err(_) => println("failure unwound")
    }}
    let reopened = reserve({port}, false)
    match runTasks() {{
        Ok(_) => println("reopened")
        Err(_) => println("reopen failed")
    }}
}}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-tcp-cleanup-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "tcp-cleanup", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "true\nnormal\ntrue\nfailure unwound\ntrue\nreopened\n",
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking signal tests require Linux")
    def test_native_sigterm_interrupts_pending_accept_and_unwinds_tcp_listener(self) -> None:
        source = '''async fn waitForPeer() {
    let pending = tcpListen("127.0.0.1", 0, 8)
    let opened = await move pending
    using listener = move opened {
        writeText("accept-ready", "ready")
        let accepted = await listener.accept()
        match move accepted {
            Ok(connection) => connection.close()
            Err(_) => println("accept ended")
        }
    } else error {
        println(error)
    }
}
fn main() {
    let task = waitForPeer()
    runTasks()
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-pending-accept-") as temporary:
            root = Path(temporary)
            executable = root / "pending-accept"
            checked = compile_checked_source(source, "native-pending-accept.tyv")
            CppNativeBackend().build(
                lower_checked_program(checked), executable, project_root=root,
            )
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment["PATH"] = ""
            process = subprocess.Popen(
                [str(executable)], cwd=root, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            try:
                deadline = time.monotonic() + 5
                marker = root / "accept-ready"
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "native listener did not reach its pending accept")
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 143, error)
            self.assertIn("runtime shutdown requested by SIGTERM", error)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native signal integration requires Linux")
    def test_native_sigint_and_sigterm_unwind_resources_with_stable_exit_codes(self) -> None:
        source = '''fn main() {
    using writer = openTextWriter("signal.txt", "write") {
        writer.write("closed on signal")
        while true {}
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-signals-") as temporary:
            root = Path(temporary)
            executable = root / "signal-app"
            checked = compile_checked_source(source, "native-signal.tyv")
            CppNativeBackend().build(
                lower_checked_program(checked), executable, project_root=root,
            )
            for selected_signal, expected_code, expected_name in (
                (signal.SIGINT, 130, "SIGINT"),
                (signal.SIGTERM, 143, "SIGTERM"),
            ):
                with self.subTest(signal=expected_name):
                    output_file = root / "signal.txt"
                    output_file.unlink(missing_ok=True)
                    process = subprocess.Popen(
                        [str(executable)], cwd=root, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    )
                    try:
                        time.sleep(0.05)
                        self.assertIsNone(process.poll(), "the test program should still be in its loop")
                        process.send_signal(selected_signal)
                        _, error = process.communicate(timeout=5)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                    self.assertEqual(process.returncode, expected_code, error)
                    self.assertIn(f"runtime shutdown requested by {expected_name}", error)
                    self.assertEqual(output_file.read_text(encoding="utf-8"), "closed on signal")

    def test_native_sync_resource_examples_match_reference_interpreter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-resources-") as temporary:
            root = Path(temporary)
            for name in ("resource_scopes.tyv", "resource_ownership.tyv"):
                with self.subTest(example=name):
                    source = REPOSITORY / "examples" / name
                    project_root = source.parent
                    checked = compile_checked_project(source, project_root)
                    expected = io.StringIO()
                    ReferenceInterpreterBackend().execute(
                        checked, output=expected, error_output=io.StringIO(), project_root=project_root,
                    )
                    result = self._build_and_run(
                        source, root / Path(name).stem, project_root=project_root, cwd=root,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected.getvalue())
                    self.assertEqual(result.stderr, "")

    def test_cli_build_produces_executable_and_keeps_python_out_of_runtime(self) -> None:
        source = REPOSITORY / "examples" / "native_hello.tyv"
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-native-") as temporary:
            output = Path(temporary) / ("hello.exe" if os.name == "nt" else "hello")
            result = subprocess.run(
                [sys.executable, "-m", "tyvera", "build", str(source), "--output", str(output)],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            run_environment = os.environ.copy()
            run_environment.pop("PYTHONPATH", None)
            run_environment.pop("PYTHONHOME", None)
            if os.name != "nt":
                run_environment["PATH"] = ""
            program = subprocess.run([str(output)], capture_output=True, text=True, env=run_environment, check=False)
            self.assertEqual(program.returncode, 0, program.stderr)
            self.assertEqual(program.stdout, "Hello from native Tyvera!\n")

    def test_runtime_overflow_is_a_located_tyvera_failure(self) -> None:
        source = compile_checked_source(
            "fn main() { println(9223372036854775807 + 1) }", "overflow.tyv",
        )
        module = lower_checked_program(source)
        with tempfile.TemporaryDirectory(prefix="tyvera-native-overflow-") as temporary:
            executable = Path(temporary) / "overflow"
            CppNativeBackend().build(module, executable)
            result = subprocess.run([str(executable)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("overflow.tyv:1:", result.stderr)
        self.assertIn("Int overflow: result is outside the signed 64-bit range", result.stderr)

    def test_division_by_zero_is_a_controlled_native_runtime_failure(self) -> None:
        checked = compile_checked_source("fn main() { println(4 / 0) }", "divide.tyv")
        module = lower_checked_program(checked)
        with tempfile.TemporaryDirectory(prefix="tyvera-native-divide-") as temporary:
            executable = Path(temporary) / "divide"
            CppNativeBackend().build(module, executable)
            result = subprocess.run([str(executable)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("divide.tyv:1:", result.stderr)
        self.assertIn("division by zero", result.stderr)

    def test_list_bounds_failure_matches_reference_diagnostic(self) -> None:
        for operation in ("values.insert(-1, 8)", "values.set(1, 8)", "values.set(4, 8)"):
            with self.subTest(operation=operation):
                source_text = f'''fn main() {{
    let values: List<Int> = List<Int>()
    values.append(1)
    {operation}
}}
'''
                checked = compile_checked_source(source_text, "list_bounds.tyv")
                with self.assertRaises(Diagnostic) as caught:
                    ReferenceInterpreterBackend().execute(
                        checked, output=io.StringIO(), error_output=io.StringIO(), project_root=REPOSITORY,
                    )
                diagnostic = caught.exception.render()
                with tempfile.TemporaryDirectory(prefix="tyvera-native-list-bounds-") as temporary:
                    executable = Path(temporary) / "list_bounds"
                    CppNativeBackend().build(lower_checked_program(checked), executable)
                    result = subprocess.run(
                        [str(executable)], capture_output=True, text=True, encoding="utf-8", check=False,
                    )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.strip(), diagnostic)
                self.assertEqual(result.stdout, "")

    def test_native_json_values_constructors_and_failures_match_reference(self) -> None:
        source = r'''fn report(document: String) {
    match parseJson(document) {
        Ok(value) => match jsonStringify(value) {
            Ok(encoded) => println(encoded)
            Err(_) => println("encoding failed")
        }
        Err(message) => println(message)
    }
}
fn main() {
    report("{\"unicode\":\"\\ud83d\\ude03\",\"line\":\"a\\nb\"}")
    report("[-0,-0.0,1.5,1e-4,1e-5,1e15,1e16,1e-9999]")
    report("{\"x\":1,\"x\":2}")
    report("9223372036854775808")
    report("NaN")
    report("\"\\ud800\"")
    let values: List<Json> = List<Json>()
    values.append(jsonString("hello\nworld"))
    values.append(jsonBool(true))
    let entries: Map<String, Json> = Map<String, Json>()
    entries.put("items", jsonArray(values))
    entries.put("count", jsonInt(2))
    println(jsonObject(entries) == jsonObject(entries))
    let first: Map<String, Json> = Map<String, Json>()
    first.put("a", jsonInt(1))
    first.put("b", jsonInt(2))
    let second: Map<String, Json> = Map<String, Json>()
    second.put("b", jsonInt(2))
    second.put("a", jsonInt(1))
    println(jsonObject(first) == jsonObject(second))
    let keyed: Map<Json, Int> = Map<Json, Int>()
    keyed.put(jsonObject(first), 7)
    match keyed.get(jsonObject(second)) {
        Some(value) => println(value)
        None => println(-1)
    }
    match jsonStringify(jsonObject(entries)) {
        Ok(encoded) => println(encoded)
        Err(_) => println("encoding failed")
    }
}'''
        deep_document = "[" * (MAX_JSON_NESTING + 1) + "0" + "]" * (MAX_JSON_NESTING + 1)
        source = source.replace(
            '    report("NaN")',
            '    report("NaN")\n    report("' + deep_document + '")',
        )
        with tempfile.TemporaryDirectory(prefix="tyvera-native-json-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "json", root)
        self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
        self.assertIn('"unicode":"😃"', native.stdout)
        self.assertIn("true\ntrue\n7\n", native.stdout)

    def test_native_json_document_size_limit_matches_reference(self) -> None:
        source = '''fn main() {
    match readText("large.json") {
        Ok(document) => match parseJson(document) {
            Ok(_) => println("accepted")
            Err(_) => println("rejected")
        }
        Err(_) => println("read failed")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-json-limit-") as temporary:
            root = Path(temporary)
            (root / "large.json").write_bytes(b" " * (MAX_JSON_DOCUMENT_BYTES + 1))
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "json-limit", root)
        self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
        self.assertEqual(native.stdout, "rejected\n")

    def test_native_json_accessors_and_typed_constructors_match_reference(self) -> None:
        source = r'''fn main() {
    match parseJson("{\"array\":[null,true,12,1.5,\"text\"],\"key\":\"value\"}") {
        Err(_) => println("parse failed")
        Ok(document) => {
            println(document.kind())
            match document.objectLength() {
                Some(length) => println(length)
                None => println(-1)
            }
            match document.objectGet("key") {
                Some(value) => match value.asString() {
                    Some(text) => println(text)
                    None => println("wrong string kind")
                }
                None => println("missing key")
            }
            match document.objectGet("missing") {
                Some(_) => println("unexpected key")
                None => println("missing key")
            }
            match document.objectGet("array") {
                Some(array) => {
                    match array.arrayLength() {
                        Some(length) => println(length)
                        None => println(-1)
                    }
                    match array.arrayAt(0) {
                        Some(value) => println(value.isNull())
                        None => println(false)
                    }
                    match array.arrayAt(1) {
                        Some(value) => match value.asBool() {
                            Some(flag) => println(flag)
                            None => println(false)
                        }
                        None => println(false)
                    }
                    match array.arrayAt(2) {
                        Some(value) => match value.asInt() {
                            Some(number) => println(number)
                            None => println(-1)
                        }
                        None => println(-1)
                    }
                    match array.arrayAt(3) {
                        Some(value) => match value.asFloat() {
                            Some(number) => println(number)
                            None => println(-1.0)
                        }
                        None => println(-1.0)
                    }
                    match array.arrayAt(4) {
                        Some(value) => match value.asString() {
                            Some(text) => println(text)
                            None => println("wrong string kind")
                        }
                        None => println("missing array value")
                    }
                    match array.arrayAt(-1) {
                        Some(_) => println("unexpected negative index")
                        None => println("negative index rejected")
                    }
                    match array.arrayAt(5) {
                        Some(_) => println("unexpected end index")
                        None => println("end index rejected")
                    }
                }
                None => println("missing array")
            }
            match document.objectKeys() {
                Some(keys) => {
                    match keys.get(0) {
                        Some(key) => println(key)
                        None => println("missing first key")
                    }
                    match keys.get(1) {
                        Some(key) => println(key)
                        None => println("missing second key")
                    }
                }
                None => println("wrong object kind")
            }
        }
    }
    match parseJson("null") {
        Ok(value) => println(value.isNull())
        Err(_) => println(false)
    }
    match jsonStringify(jsonFloat(1.0)) {
        Ok(encoded) => println(encoded)
        Err(_) => println("float encoding failed")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-json-accessors-") as temporary:
            root = Path(temporary)
            reference = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "json-accessors", root)
        self.assertEqual((native.returncode, native.stdout, native.stderr), reference)
        self.assertEqual(
            native.stdout,
            "object\n2\nvalue\nmissing key\n5\ntrue\ntrue\n12\n1.5\ntext\n"
            "negative index rejected\nend index rejected\narray\nkey\ntrue\n1.0\n",
        )




    def test_native_websocket_url_values_match_reference(self) -> None:
        source = '''fn main() {
    match webSocketUrl("WSS://example.test:8443/gateway?v=10") {
        Err(error) => println(error)
        Ok(url) => {
            println(url.scheme())
            println(url.host())
            println(url.port())
            println(url.target())
            println(url.hostEndsWith(".test"))
        }
    }
    match webSocketUrl("ftp://example.test/") {
        Err(error) => println(error)
        Ok(_) => println("unexpected URL")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-websocket-url-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "websocket-url", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "wss\nexample.test\n8443\n/gateway?v=10\ntrue\n"
                "WebSocket URL scheme must be ws or wss\n",
            )

    def test_native_http_url_values_match_reference(self) -> None:
        source = '''fn main() {
    match httpUrl("HTTPS://ExAmple.test:8443/path?q=1") {
        Err(error) => println(error)
        Ok(url) => {
            println(url.scheme())
            println(url.host())
            println(url.port())
            println(url.target())
            println(url.authority())
        }
    }
    match httpUrl("http://[::1]:8080/") {
        Err(error) => println(error)
        Ok(url) => {
            println(url.host())
            println(url.port())
            println(url.authority())
        }
    }
    match httpMethod("custom-method") {
        Err(error) => println(error)
        Ok(method) => {
            println(method.name())
            println(method.isStandard())
        }
    }
    match httpUrl("ftp://example.test/") {
        Err(error) => println(error)
        Ok(_) => println("unexpected URL")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-http-url-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "http-url", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "https\nexample.test\n8443\n/path?q=1\nexample.test:8443\n"
                "::1\n8080\n[::1]:8080\ncustom-method\nfalse\n"
                "HTTP URL scheme must be http or https\n",
            )

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "native HTTPS tests require Linux and OpenSSL",
    )
    def test_native_https_client_uses_verified_tls_and_matches_reference(self) -> None:
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL command line tools are unavailable")
        handled = 0
        lock = threading.Lock()
        complete = threading.Event()
        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="tyvera-native-https-") as temporary:
            root = Path(temporary)
            certificate, private_key = root / "cert.pem", root / "key.pem"
            subprocess.run(
                [
                    openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(private_key), "-out", str(certificate), "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                ],
                capture_output=True, text=True, check=True,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, private_key)

            class Handler(socketserver.BaseRequestHandler):
                def handle(self) -> None:
                    nonlocal handled
                    try:
                        self.request.settimeout(5)
                        request = bytearray()
                        while b"\r\n\r\n" not in request:
                            data = self.request.recv(4096)
                            if not data:
                                raise EOFError("request closed during headers")
                            request.extend(data)
                        if not bytes(request).startswith(b"GET /data HTTP/1.1\r\n"):
                            raise AssertionError("unexpected HTTPS request")
                        self.request.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n"
                            b"X-Secure: verified\r\nConnection: close\r\n\r\nsecure"
                        )
                    except Exception as error:
                        failures.append(f"{type(error).__name__}: {error}")
                    finally:
                        with lock:
                            handled += 1
                            if handled == 2:  # reference and native clients
                                complete.set()

            class LoopbackTlsServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

                def get_request(self):
                    raw, address = super().get_request()
                    try:
                        return context.wrap_socket(raw, server_side=True), address
                    except OSError:
                        raw.close()
                        raise

                def handle_error(self, request, client_address) -> None:
                    return

            server = LoopbackTlsServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            port = server.server_address[1]
            source = f'''async fn client() {{
    match httpUrl("https://localhost:{port}/data") {{
        Err(error) => println(error)
        Ok(url) => {{
            let response = await httpGet(url, milliseconds(3000))
            match response {{
                Err(error) => println(error)
                Ok(response) => {{
                    println(response.status())
                    match response.headers().get("x-secure") {{
                        None => println("missing")
                        Some(value) => println(value)
                    }}
                    match response.body().toString() {{
                        Err(error) => println(error)
                        Ok(text) => println(text)
                    }}
                }}
            }}
        }}
    }}
}}
fn main() {{
    let task = client()
    match runTasks() {{
        Err(error) => eprintln(error)
        Ok(_) => {{}}
    }}
}}'''
            try:
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(certificate)}):
                    expected = self._reference_result(source, root)
                    native = self._native_source_result(source, root, root / "https-client", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(native.stdout, "200\nverified\nsecure\n")
                self.assertTrue(complete.wait(timeout=5), "loopback HTTPS handlers did not finish")
                self.assertEqual(failures, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native HTTP tests require Linux")
    def test_native_http_malformed_content_length_matches_reference(self) -> None:
        handled = 0
        lock = threading.Lock()
        complete = threading.Event()
        failures: list[str] = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                nonlocal handled
                try:
                    self.request.settimeout(5)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        data = self.request.recv(4096)
                        if not data:
                            raise EOFError("request closed during headers")
                        request.extend(data)
                    self.request.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
                        b"Content-Length: 1\r\nConnection: close\r\n\r\nx"
                    )
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                finally:
                    with lock:
                        handled += 1
                        if handled == 2:  # reference and native requests
                            complete.set()

        class LoopbackServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with tempfile.TemporaryDirectory(prefix="tyvera-native-http-invalid-") as temporary:
            root = Path(temporary)
            server = LoopbackServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            port = server.server_address[1]
            source = f'''async fn client() {{
    match httpUrl("http://127.0.0.1:{port}/") {{
        Err(error) => println(error)
        Ok(url) => {{
            let response = await httpGet(url, milliseconds(2000))
            match response {{
                Err(error) => println(error)
                Ok(_) => println("unexpected response")
            }}
        }}
    }}
}}
fn main() {{
    let task = client()
    match runTasks() {{
        Err(error) => eprintln(error)
        Ok(_) => {{}}
    }}
}}'''
            try:
                expected = self._reference_result(source, root)
                native = self._native_source_result(source, root, root / "http-invalid", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(native.stdout, "HTTP response parse error: invalid Content-Length\n")
                self.assertTrue(complete.wait(timeout=5), "loopback malformed-response handlers did not finish")
                self.assertEqual(failures, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native HTTP tests require Linux")
    def test_native_http_response_body_limit_matches_reference(self) -> None:
        handled = 0
        lock = threading.Lock()
        complete = threading.Event()
        failures: list[str] = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                nonlocal handled
                try:
                    self.request.settimeout(5)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        data = self.request.recv(4096)
                        if not data:
                            raise EOFError("request closed during headers")
                        request.extend(data)
                    self.request.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                finally:
                    with lock:
                        handled += 1
                        if handled == 2:  # reference and native requests
                            complete.set()

        class LoopbackServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with tempfile.TemporaryDirectory(prefix="tyvera-native-http-limit-") as temporary:
            root = Path(temporary)
            server = LoopbackServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            port = server.server_address[1]
            source = f'''async fn client() {{
    match httpUrl("http://127.0.0.1:{port}/") {{
        Err(error) => println(error)
        Ok(url) => {{
            let response = await httpGet(url, milliseconds(2000))
            match response {{
                Err(error) => println(error)
                Ok(_) => println("unexpected response")
            }}
        }}
    }}
}}
fn main() {{
    let task = client()
    match runTasks() {{
        Err(error) => eprintln(error)
        Ok(_) => {{}}
    }}
}}'''
            try:
                expected = self._reference_result(source, root)
                native = self._native_source_result(source, root, root / "http-limit", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(native.stdout, "HTTP response parse error: HTTP body exceeds configured size limit\n")
                self.assertTrue(complete.wait(timeout=5), "loopback oversized-response handlers did not finish")
                self.assertEqual(failures, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native HTTP tests require Linux")
    def test_native_http_get_post_chunked_matches_reference(self) -> None:
        failures: list[str] = []
        handled = 0
        lock = threading.Lock()
        complete = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                nonlocal handled
                try:
                    self.request.settimeout(5)
                    raw = bytearray()
                    while b"\r\n\r\n" not in raw:
                        chunk = self.request.recv(4096)
                        if not chunk:
                            raise EOFError("request closed during headers")
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise AssertionError("oversized request headers")
                    header_end = raw.index(b"\r\n\r\n") + 4
                    lines = bytes(raw[:header_end]).decode("ascii").split("\r\n")
                    method, target, _ = lines[0].split(" ")
                    headers = {}
                    for line in lines[1:]:
                        if not line:
                            break
                        name, value = line.split(":", 1)
                        headers.setdefault(name.lower(), []).append(value.strip())
                    length = int(headers.get("content-length", ["0"])[0])
                    body = bytearray(raw[header_end:])
                    while len(body) < length:
                        chunk = self.request.recv(length - len(body))
                        if not chunk:
                            raise EOFError("request closed during body")
                        body.extend(chunk)
                    if target != "/api?q=1" or headers.get("connection") != ["close"]:
                        raise AssertionError("unexpected request target or connection mode")
                    if method == "GET":
                        if body:
                            raise AssertionError("GET unexpectedly included a body")
                        response = (
                            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n"
                            b"X-Repeat: alpha\r\nX-Repeat: beta\r\nConnection: close\r\n\r\nhello"
                        )
                    elif method == "POST":
                        if bytes(body[:length]) != b"upload" or headers.get("x-upload") != ["yes"]:
                            raise AssertionError("POST body or header was not preserved")
                        response = (
                            b"HTTP/1.1 201 Created\r\nTransfer-Encoding: chunked\r\n"
                            b"Connection: close\r\n\r\n3\r\nack\r\n0\r\nX-Trailer: ignored\r\n\r\n"
                        )
                    else:
                        raise AssertionError("unexpected HTTP method")
                    self.request.sendall(response)
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                finally:
                    with lock:
                        handled += 1
                        if handled == 4:  # two requests from each backend
                            complete.set()

        class LoopbackServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with tempfile.TemporaryDirectory(prefix="tyvera-native-http-") as temporary:
            root = Path(temporary)
            server = LoopbackServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            port = server.server_address[1]
            source = f'''async fn client(url: HttpUrl) {{
    let received = await httpGet(url, milliseconds(2000))
    match received {{
        Err(error) => println(error)
        Ok(response) => {{
            println(response.status())
            match response.headers().get("x-repeat") {{
                None => println("missing header")
                Some(value) => println(value)
            }}
            match response.body().toString() {{
                Err(error) => println(error)
                Ok(text) => println(text)
            }}
        }}
    }}
    let headers = httpHeaders()
    match headers.add("X-Upload", "yes") {{
        Err(error) => println(error)
        Ok(_) => {{}}
    }}
    let posted = await httpPost(url, headers, bytesFromString("upload"), milliseconds(2000))
    match posted {{
        Err(error) => println(error)
        Ok(response) => {{
            println(response.status())
            match response.body().toString() {{
                Err(error) => println(error)
                Ok(text) => println(text)
            }}
        }}
    }}
}}
fn main() {{
    match httpUrl("http://127.0.0.1:{port}/api?q=1") {{
        Err(error) => println(error)
        Ok(url) => {{
            let task = client(url)
            match runTasks() {{
                Err(error) => eprintln(error)
                Ok(_) => {{}}
            }}
        }}
    }}
}}'''
            try:
                expected = self._reference_result(source, root)
                native = self._native_source_result(source, root, root / "http-client", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(native.stdout, "200\nalpha, beta\nhello\n201\nack\n")
                self.assertTrue(complete.wait(timeout=5), "loopback HTTP handlers did not finish")
                self.assertEqual(failures, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native WebSocket tests require Linux")
    def test_native_websocket_echo_and_close_match_reference(self) -> None:
        failures: list[str] = []
        handlers_finished = 0
        handlers_lock = threading.Lock()
        handlers_done = threading.Event()

        def receive_exact(stream: socket.socket, length: int) -> bytes:
            data = bytearray()
            while len(data) < length:
                chunk = stream.recv(length - len(data))
                if not chunk:
                    raise EOFError("WebSocket peer closed early")
                data.extend(chunk)
            return bytes(data)

        def receive_client_frame(stream: socket.socket) -> tuple[int, bytes]:
            first, second = receive_exact(stream, 2)
            length = second & 0x7f
            if length == 126:
                length = int.from_bytes(receive_exact(stream, 2), "big")
            elif length == 127:
                length = int.from_bytes(receive_exact(stream, 8), "big")
            if not second & 0x80:
                raise AssertionError("client WebSocket frame was not masked")
            mask = receive_exact(stream, 4)
            payload = receive_exact(stream, length)
            return first & 0x0f, bytes(value ^ mask[index & 3] for index, value in enumerate(payload))

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    self.request.settimeout(5)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(self.request.recv(1024))
                        if len(request) > 32768:
                            raise AssertionError("oversized handshake")
                    headers = {}
                    for line in bytes(request).decode("ascii").split("\r\n")[1:]:
                        if not line:
                            break
                        name, value = line.split(":", 1)
                        headers[name.strip().lower()] = value.strip()
                    key = headers["sec-websocket-key"]
                    response = (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n"
                    ).encode("ascii")
                    self.request.sendall(response)
                    opcode, message = receive_client_frame(self.request)
                    if opcode != 1 or message != b"ping":
                        raise AssertionError("unexpected client WebSocket message")
                    response_message = message + b"!"
                    self.request.sendall(bytes((0x81, len(response_message))) + response_message)
                    opcode, close_payload = receive_client_frame(self.request)
                    if opcode != 8:
                        raise AssertionError("client did not start the close handshake")
                    self.request.sendall(bytes((0x88, len(close_payload))) + close_payload)
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                finally:
                    nonlocal handlers_finished
                    with handlers_lock:
                        handlers_finished += 1
                        if handlers_finished == 2:  # interpreter and native runs
                            handlers_done.set()

        class LoopbackServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with tempfile.TemporaryDirectory(prefix="tyvera-native-websocket-") as temporary:
            root = Path(temporary)
            server = LoopbackServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            port = server.server_address[1]
            source = f'''async fn client() {{
    let opened = await webSocketConnect("ws://127.0.0.1:{port}/echo?mode=test", milliseconds(2000))
    using connection = move opened {{
        println(connection.isOpen())
        let sent = await connection.sendText("ping")
        match sent {{
            Err(error) => println(error)
            Ok(count) => println(count)
        }}
        let received = await connection.receive()
        match received {{
            Err(error) => println(error)
            Ok(None) => println("unexpected WebSocket EOF")
            Ok(Some(message)) => {{
                println(message.kind())
                match message.text() {{
                    Err(error) => println(error)
                    Ok(text) => println(text)
                }}
            }}
        }}
        let closed = await connection.closeGracefully()
        match closed {{
            Err(error) => println(error)
            Ok(true) => println("closed cleanly")
            Ok(false) => println("close failed")
        }}
    }} else error {{
        println(error)
    }}
}}
fn main() {{
    let task = client()
    match runTasks() {{
        Ok(_) => {{}}
        Err(error) => eprintln(error)
    }}
}}'''
            try:
                expected = self._reference_result(source, root)
                native = self._native_source_result(source, root, root / "websocket-client", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(
                    native.stdout,
                    "true\n4\ntext\nping!\nclosed cleanly\n",
                )
                self.assertTrue(handlers_done.wait(timeout=5), "loopback WebSocket handlers did not finish")
                self.assertEqual(failures, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)


    @unittest.skipUnless(
        sys.platform.startswith("linux"), "native WSS tests require Linux and OpenSSL",
    )
    def test_native_wss_echo_uses_verified_tls_and_matches_reference(self) -> None:
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL command line tools are unavailable")
        errors: list[str] = []
        handlers_finished = 0
        handlers_lock = threading.Lock()
        handlers_done = threading.Event()
        with tempfile.TemporaryDirectory(prefix="tyvera-native-wss-") as temporary:
            root = Path(temporary)
            certificate, private_key = root / "cert.pem", root / "key.pem"
            subprocess.run(
                [
                    openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(private_key), "-out", str(certificate), "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                ],
                capture_output=True, text=True, check=True,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, private_key)

            def exact(stream: socket.socket, length: int) -> bytes:
                data = bytearray()
                while len(data) < length:
                    chunk = stream.recv(length - len(data))
                    if not chunk:
                        raise EOFError("peer closed early")
                    data.extend(chunk)
                return bytes(data)

            def client_frame(stream: socket.socket) -> tuple[int, bytes]:
                first, second = exact(stream, 2)
                length = second & 0x7f
                if length == 126:
                    length = int.from_bytes(exact(stream, 2), "big")
                elif length == 127:
                    length = int.from_bytes(exact(stream, 8), "big")
                if not second & 0x80:
                    raise AssertionError("client WebSocket frame was not masked")
                mask = exact(stream, 4)
                payload = exact(stream, length)
                return first & 0x0f, bytes(value ^ mask[index & 3] for index, value in enumerate(payload))

            class Handler(socketserver.BaseRequestHandler):
                def handle(self) -> None:
                    try:
                        self.request.settimeout(5)
                        request = bytearray()
                        while b"\r\n\r\n" not in request:
                            request.extend(self.request.recv(1024))
                        headers = {}
                        for line in bytes(request).decode("ascii").split("\r\n")[1:]:
                            if not line:
                                break
                            name, value = line.split(":", 1)
                            headers[name.strip().lower()] = value.strip()
                        key = headers["sec-websocket-key"]
                        self.request.sendall((
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n"
                        ).encode("ascii"))
                        opcode, message = client_frame(self.request)
                        if opcode != 1 or message != b"secure":
                            raise AssertionError("unexpected secure WebSocket payload")
                        response = b"secure!"
                        self.request.sendall(bytes((0x81, len(response))) + response)
                        opcode, close_payload = client_frame(self.request)
                        if opcode != 8:
                            raise AssertionError("client did not close the WebSocket")
                        self.request.sendall(bytes((0x88, len(close_payload))) + close_payload)
                    except Exception as error:
                        errors.append(f"{type(error).__name__}: {error}")
                    finally:
                        nonlocal handlers_finished
                        with handlers_lock:
                            handlers_finished += 1  # interpreter and native runs
                            if handlers_finished == 2:
                                handlers_done.set()

            class LoopbackTlsServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

                def get_request(self):
                    raw, address = super().get_request()
                    try:
                        return context.wrap_socket(raw, server_side=True), address
                    except OSError:
                        raw.close()
                        raise

                def handle_error(self, request, client_address) -> None:
                    return

            server = LoopbackTlsServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            port = server.server_address[1]
            source = f'''async fn client() {{
    let opened = await webSocketConnect("wss://localhost:{port}/gateway", milliseconds(3000))
    using connection = move opened {{
        let sent = await connection.sendText("secure")
        match sent {{
            Err(error) => println(error)
            Ok(count) => println(count)
        }}
        let received = await connection.receive()
        match received {{
            Err(error) => println(error)
            Ok(None) => println("unexpected WebSocket EOF")
            Ok(Some(message)) => match message.text() {{
                Err(error) => println(error)
                Ok(text) => println(text)
            }}
        }}
        let closed = await connection.closeGracefully()
        match closed {{
            Err(error) => println(error)
            Ok(true) => println("secure close complete")
            Ok(false) => println("secure close failed")
        }}
    }} else error {{
        println(error)
    }}
}}
fn main() {{
    let task = client()
    match runTasks() {{
        Ok(_) => {{}}
        Err(error) => eprintln(error)
    }}
}}'''
            try:
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(certificate)}):
                    expected = self._reference_result(source, root)
                    native = self._native_source_result(source, root, root / "wss-client", root)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
                self.assertEqual(native.stdout, "6\nsecure!\nsecure close complete\n")
                self.assertTrue(handlers_done.wait(timeout=5), "loopback WSS handlers did not finish")
                self.assertEqual(errors, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

if __name__ == "__main__":
    unittest.main()

