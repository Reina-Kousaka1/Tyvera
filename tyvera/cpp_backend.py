"""C++17 bootstrap code generator and host compiler for the native IR."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import fields, is_dataclass
import subprocess
import tempfile
from pathlib import Path

from .ir import (
    IRAppliedType,
    IRAwait,
    IRBinary,
    IRBlock,
    IRBindingPattern,
    IRBuiltinCall,
    IRCall,
    IRCollectionCreate,
    IRCollectionLength,
    IRExpression,
    IRExpressionStatement,
    IRForEach,
    IRFunction,
    IRIf,
    IRLet,
    IRLiteral,
    IRLiteralPattern,
    IRLocalRef,
    IRLoopControl,
    IRMatch,
    IRMethodCall,
    IRMove,
    IRModule,
    IROutput,
    IRReturn,
    IRUsing,
    IRType,
    IRTypeRef,
    IRUnary,
    IRAssign,
    IRVariable,
    IRVariant,
    IRVariantPattern,
    IRWhile,
    IRWildcardPattern,
    ir_type_name,
    validate_ir,
)
from .source import Diagnostic, Location


class CppNativeBackend:
    """Build a standalone native executable through an installed C++17 compiler."""

    name = "cpp17-native"

    def build(self, module: IRModule, output: str | Path, project_root: str | Path | None = None) -> Path:
        validate_ir(module)
        native_tls = _module_uses_tls(module)
        if native_tls and not sys.platform.startswith("linux"):
            raise Diagnostic(module.functions[0].location, "backend", "native TLS is currently supported only on Linux")
        destination = Path(output).expanduser().resolve()
        if destination.exists() and destination.is_dir():
            raise Diagnostic(module.functions[0].location, "backend", "native output path names a directory")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise Diagnostic(module.functions[0].location, "backend", f"cannot prepare native output directory: {error}") from error
        compiler = _find_compiler(module.functions[0].location)
        source = emit_cpp(module, project_root=project_root)
        try:
            with tempfile.TemporaryDirectory(prefix=".tyvera-build-", dir=destination.parent) as temporary:
                directory = Path(temporary)
                source_file = directory / "program.cpp"
                candidate = directory / ("program.exe" if os.name == "nt" else "program")
                source_file.write_text(source, encoding="utf-8", newline="\n")
                command = _compiler_command(compiler, source_file, candidate, native_tls=native_tls)
                try:
                    result = subprocess.run(
                        command,
                        cwd=directory,
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                except OSError as error:
                    raise Diagnostic(module.functions[0].location, "backend", f"cannot run native C++ compiler: {error}") from error
                if result.returncode != 0:
                    details = (result.stderr or result.stdout).strip()
                    if len(details) > 6000:
                        details = details[:6000] + "\n… compiler output truncated"
                    suffix = f"\n{details}" if details else ""
                    raise Diagnostic(
                        module.functions[0].location,
                        "backend",
                        f"native C++ compilation failed with exit code {result.returncode}{suffix}",
                    )
                if not candidate.is_file():
                    raise Diagnostic(module.functions[0].location, "backend", "C++ compiler reported success but produced no executable")
                os.replace(candidate, destination)
        except Diagnostic:
            raise
        except OSError as error:
            raise Diagnostic(module.functions[0].location, "backend", f"cannot write native executable: {error}") from error
        return destination


def _find_compiler(location: Location) -> tuple[str, ...]:
    configured = os.environ.get("CXX", "").strip()
    if configured:
        if Path(configured).is_file() or shutil.which(configured):
            return (configured,)
        try:
            command = tuple(shlex.split(configured, posix=True))
        except ValueError as error:
            raise Diagnostic(location, "backend", f"invalid CXX compiler command: {error}") from error
        if not command:
            raise Diagnostic(location, "backend", "CXX does not name a C++ compiler")
        executable = command[0]
        if Path(executable).is_file() or shutil.which(executable):
            return command
        raise Diagnostic(location, "backend", f"C++ compiler from CXX was not found: {executable}")
    for executable in ("clang++", "g++", "c++", "cl"):
        if shutil.which(executable):
            return (executable,)
    raise Diagnostic(
        location,
        "backend",
        "native build requires a C++17 compiler (clang++, g++, c++, or cl); install one or set CXX",
    )


def _compiler_command(compiler: tuple[str, ...], source: Path, output: Path, *, native_tls: bool = False) -> list[str]:
    command = list(compiler)
    executable = Path(command[0]).name.lower()
    if executable in {"cl", "cl.exe"}:
        return [*command, "/nologo", "/std:c++17", "/EHsc", str(source), f"/Fe:{output}"]
    command.extend(["-std=c++17", "-O2", "-pthread", str(source), "-o", str(output)])
    if native_tls:
        command.extend(["-lssl", "-lcrypto"])
    return command


def emit_cpp(module: IRModule, project_root: str | Path | None = None) -> str:
    """Return complete C++17 source for a validated Tyvera IR module."""
    validate_ir(module)
    generator = _CppGenerator(module, project_root)
    return generator.generate()


class _CppGenerator:
    def __init__(self, module: IRModule, project_root: str | Path | None = None) -> None:
        self.module = module
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self.function_symbols = {function.symbol: function for function in module.functions}
        self.lines: list[str] = []
        self.indent = 0
        self.temporary = 0
        self.async_context = False

    def generate(self) -> str:
        encoded_root = self.project_root.as_posix().encode("utf-8")
        runtime = _RUNTIME.replace(
            "/*TYVERA_PROJECT_ROOT*/",
            f"inline const std::string project_root_text = std::string({_cpp_byte_string(encoded_root)}, {len(encoded_root)});",
        )
        if _module_uses_tls(self.module):
            runtime = "#define TYVERA_NATIVE_TLS 1\n" + runtime
        self.lines.extend(runtime.splitlines())
        self._line("namespace tyvera_generated {")
        self.indent += 1
        for function in self.module.functions:
            self._line(self._function_signature(function) + ";")
        for function in self.module.functions:
            self._line("")
            self._function(function)
        self.indent -= 1
        self._line("}")
        self._line("")
        self._line("int main() {")
        self.indent += 1
        self._line("if (!tyvera_runtime::install_shutdown_handlers()) {")
        self.indent += 1
        self._line('std::cerr << "<native>:1:1: runtime initialization failed: could not install shutdown handlers\\n";')
        self._line("return static_cast<int>(tyvera_runtime::ExitCode::initialization_failure);")
        self.indent -= 1
        self._line("}")
        self._line("try {")
        self.indent += 1
        self._line(f"tyvera_generated::{self.module.entry_symbol}();")
        self._line('tyvera_runtime::check_shutdown("<native>", 1, 1);')
        self._line("if (tyvera_runtime::report_cleanup_failures()) return 1;")
        self._line("return static_cast<int>(tyvera_runtime::ExitCode::success);")
        self.indent -= 1
        self._line("} catch (const tyvera_runtime::SignalShutdown& signal) {")
        self.indent += 1
        self._line("""std::cerr << signal.file << ':' << signal.line << ':' << signal.column << ": runtime shutdown: " << signal.what() << tyvera_runtime::cleanup_failure_suffix() << '\\n';""")
        self._line("return signal.exit_code;")
        self.indent -= 1
        self._line("} catch (const tyvera_runtime::Failure& error) {")
        self.indent += 1
        self._line("""std::cerr << error.file << ':' << error.line << ':' << error.column << ": runtime error: " << error.message << tyvera_runtime::cleanup_failure_suffix() << '\\n';""")
        self._line("return static_cast<int>(tyvera_runtime::ExitCode::runtime_failure);")
        self.indent -= 1
        self._line("} catch (const std::exception&) {")
        self.indent += 1
        self._line("""std::cerr << "<native>:1:1: runtime error: native runtime failure" << tyvera_runtime::cleanup_failure_suffix() << '\\n';""")
        self._line("return static_cast<int>(tyvera_runtime::ExitCode::runtime_failure);")
        self.indent -= 1
        self._line("}")
        self.indent -= 1
        self._line("}")
        return "\n".join(self.lines) + "\n"

    def _line(self, value: str = "") -> None:
        self.lines.append("    " * self.indent + value if value else "")

    @staticmethod
    def _cpp_type(type_name: IRTypeRef) -> str:
        if isinstance(type_name, IRAppliedType):
            arguments = [_CppGenerator._cpp_type(argument) for argument in type_name.arguments]
            if type_name.name == "Option":
                return f"std::optional<{arguments[0]}>"
            if type_name.name == "Result":
                return f"std::variant<{arguments[0]}, {arguments[1]}>"
            if type_name.name == "List":
                return f"std::shared_ptr<tyvera_runtime::ListData<{arguments[0]}>>"
            if type_name.name == "Map":
                return f"std::shared_ptr<tyvera_runtime::MapData<{arguments[0]}, {arguments[1]}>>"
            if type_name.name == "Task":
                return f"tyvera_runtime::Task<{arguments[0]}>"
            raise ValueError(f"unknown applied Tyvera IR type: {type_name.name}")
        return {
            IRType.INT: "std::int64_t",
            IRType.FLOAT: "double",
            IRType.BOOL: "bool",
            IRType.STRING: "std::string",
            IRType.BYTE: "std::uint8_t",
            IRType.TLS_CONNECTION: "std::shared_ptr<tyvera_runtime::TlsConnection>",
            IRType.DURATION: "tyvera_runtime::Duration",
            IRType.BYTES: "std::vector<std::uint8_t>",
            IRType.BYTE_BUFFER: "std::shared_ptr<tyvera_runtime::ByteBuffer>",
            IRType.TEXT_FILE: "std::shared_ptr<tyvera_runtime::TextFile>",
            IRType.TEXT_READER: "std::shared_ptr<tyvera_runtime::TextFile>",
            IRType.TEXT_WRITER: "std::shared_ptr<tyvera_runtime::TextFile>",
            IRType.SOCKET_ADDRESS: "tyvera_runtime::SocketAddress",
            IRType.TCP_CONNECTION: "std::shared_ptr<tyvera_runtime::TcpConnection>",
            IRType.TCP_LISTENER: "std::shared_ptr<tyvera_runtime::TcpListener>",
            IRType.VOID: "void",
        }[type_name]

    def _function_signature(self, function: IRFunction) -> str:
        parameters = ", ".join(
            f"{self._cpp_type(parameter.type)} tyv_argument_{parameter.identifier}"
            for parameter in function.parameters
        )
        result_type = self._cpp_type(function.return_type)
        if function.is_async:
            result_type = f"tyvera_runtime::Task<{result_type}>"
        return f"{result_type} {function.symbol}({parameters})"

    def _function(self, function: IRFunction) -> None:
        self.temporary = 0
        previous_async_context = self.async_context
        self.async_context = function.is_async
        self._line(self._function_signature(function) + " {")
        self.indent += 1
        if function.is_async:
            captures = ", ".join(
                f"tyv_argument_{parameter.identifier} = std::move(tyv_argument_{parameter.identifier})"
                for parameter in function.parameters
            )
            self._line(
                f"return tyvera_runtime::make_task<{self._cpp_type(function.return_type)}>("
                f"[{captures}]() mutable -> {self._cpp_type(function.return_type)} {{"
            )
            self.indent += 1
        self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope({_location_args(function.location)});")
        for parameter in function.parameters:
            keyword = "auto" if _ir_contains_resource(parameter.type) else "const auto"
            self._line(
                f"{keyword} tyv_local_{parameter.identifier} = "
                f"std::move(tyv_argument_{parameter.identifier});"
            )
        for statement in function.body:
            self._statement(statement)
        if function.is_async:
            if function.return_type == IRType.VOID:
                self._line("return;")
            self.indent -= 1
            self._line(f"}}, {_location_args(function.location)});")
        self.indent -= 1
        self._line("}")
        self.async_context = previous_async_context

    def _statement(self, statement) -> None:
        if self.async_context:
            self._line("tyvera_runtime::task_checkpoint();")
        self._line(f"tyvera_runtime::check_shutdown({_location_args(statement.location)});")
        if isinstance(statement, IRLet):
            self._line(
                f"{self._cpp_type(statement.variable.type)} tyv_local_{statement.variable.identifier} = "
                f"{self._expression(statement.value)};"
            )
        elif isinstance(statement, IRAssign):
            self._line(f"tyv_local_{statement.variable_id} = {self._expression(statement.value)};")
        elif isinstance(statement, IRExpressionStatement):
            self._line(f"(void)({self._expression(statement.expression)});")
        elif isinstance(statement, IROutput):
            self._output(statement)
        elif isinstance(statement, IRReturn):
            if statement.value is None:
                self._line("return;")
            else:
                self._line(f"return {self._expression(statement.value)};")
        elif isinstance(statement, IRAwait):
            task = self._new_temporary()
            self._line(f"auto {task} = {self._expression(statement.task)};")
            task_type = statement.task.type
            if not isinstance(task_type, IRAppliedType) or task_type.name != "Task":
                raise ValueError("validated IR await has a non-Task operand")
            await_method = "await_move" if _ir_contains_resource(task_type.arguments[0]) else "await"
            awaited = f"{task}.{await_method}({_location_args(statement.location)})"
            if statement.result is None:
                self._line(f"(void){awaited};")
            else:
                keyword = "auto" if _ir_contains_resource(statement.result.type) else "const auto"
                self._line(
                    f"{keyword} tyv_local_{statement.result.identifier} = {awaited};"
                )
        elif isinstance(statement, IRBlock):
            self._line("{")
            self.indent += 1
            self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(statement.location)});")
            for nested in statement.statements:
                self._statement(nested)
            self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRIf):
            self._line(f"if ({self._expression(statement.condition)}) {{")
            self.indent += 1
            self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(statement.location)});")
            for nested in statement.then_body:
                self._statement(nested)
            self.indent -= 1
            if statement.else_body:
                self._line("} else {")
                self.indent += 1
                self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(statement.location)});")
                for nested in statement.else_body:
                    self._statement(nested)
                self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRWhile):
            self._line(f"while ({self._expression(statement.condition)}) {{")
            self.indent += 1
            if self.async_context:
                self._line("tyvera_runtime::task_checkpoint();")
            self._line(f"tyvera_runtime::check_shutdown({_location_args(statement.location)});")
            self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(statement.location)});")
            for nested in statement.body:
                self._statement(nested)
            self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRForEach):
            collection = self._new_temporary()
            snapshot = self._new_temporary()
            self._line(f"const auto {collection} = {self._expression(statement.iterable)};")
            if isinstance(statement.iterable.type, IRAppliedType) and statement.iterable.type.name == "List":
                self._line(f"const auto {snapshot} = tyvera_runtime::list_snapshot({collection});")
            else:
                self._line(f"const auto {snapshot} = tyvera_runtime::map_keys_snapshot({collection});")
            self._line(f"for (const auto& {snapshot}_item : {snapshot}) {{")
            self.indent += 1
            if self.async_context:
                self._line("tyvera_runtime::task_checkpoint();")
            self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(statement.location)});")
            self._line(f"const auto tyv_local_{statement.variable.identifier} = {snapshot}_item;")
            for nested in statement.body:
                self._statement(nested)
            self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRMatch):
            subject = self._new_temporary()
            self._line(f"auto {subject} = {self._expression(statement.value)};")
            for index, arm in enumerate(statement.arms):
                prefix = "if" if index == 0 else "else if"
                condition = self._pattern_condition(arm.pattern, subject)
                self._line(f"{prefix} ({condition}) {{")
                self.indent += 1
                self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(arm.location)});")
                self._pattern_bindings(arm.pattern, subject)
                for nested in arm.body:
                    self._statement(nested)
                self.indent -= 1
                self._line("}")
            if statement.exhaustive:
                self._line("else {")
                self.indent += 1
                self._line(
                    f'tyvera_runtime::fail({_location_args(statement.location)}, '
                    '"exhaustive match had no matching pattern");'
                )
                self.indent -= 1
                self._line("}")
        elif isinstance(statement, IRUsing):
            acquired = self._new_temporary()
            self._line(f"auto {acquired} = {self._expression(statement.acquisition)};")
            self._line(f"if ({acquired}.index() == 1) {{")
            self.indent += 1
            if statement.error_variable is None:
                self._line(
                    f"tyvera_runtime::fail({_location_args(statement.location)}, std::get<1>({acquired}));"
                )
            else:
                self._line(f"const auto tyv_local_{statement.error_variable.identifier} = std::get<1>({acquired});")
                for nested in statement.else_body:
                    self._statement(nested)
            self.indent -= 1
            self._line("} else {")
            self.indent += 1
            self._line("{")
            self.indent += 1
            self._line(f"tyvera_runtime::CleanupScope tyv_cleanup_scope_{self._new_temporary()}({_location_args(statement.location)});")
            self._line(
                f"auto tyv_local_{statement.resource.identifier} = std::move(std::get<0>({acquired}));"
            )
            self._line(
                f"tyvera_runtime::set_resource_location(tyv_local_{statement.resource.identifier}, "
                f"{_location_args(statement.location)});"
            )
            for nested in statement.body:
                self._statement(nested)
            self.indent -= 1
            self._line("}")
            self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRLoopControl):
            self._line(f"{statement.kind};")
        else:
            raise ValueError(f"unhandled IR statement: {type(statement).__name__}")

    def _pattern_condition(self, pattern, value: str) -> str:
        if isinstance(pattern, (IRWildcardPattern, IRBindingPattern)):
            return "true"
        if isinstance(pattern, IRLiteralPattern):
            return f"tyvera_runtime::equal({value}, {self._expression(pattern.value)})"
        if isinstance(pattern, IRVariantPattern):
            if pattern.variant == "None":
                return f"!({value}).has_value()"
            if pattern.variant == "Some":
                if pattern.payload is None:
                    raise ValueError("validated Some pattern has no payload")
                payload = f"({value}).value()"
                return f"(({value}).has_value() && ({self._pattern_condition(pattern.payload, payload)}))"
            if pattern.variant in {"Ok", "Err"}:
                index = 0 if pattern.variant == "Ok" else 1
                if pattern.payload is None:
                    raise ValueError(f"validated {pattern.variant} pattern has no payload")
                payload = f"std::get<{index}>({value})"
                return f"(({value}).index() == {index} && ({self._pattern_condition(pattern.payload, payload)}))"
        raise ValueError(f"unhandled IR pattern: {type(pattern).__name__}")

    def _pattern_bindings(self, pattern, value: str) -> None:
        if isinstance(pattern, IRBindingPattern):
            transfer = f"std::move({value})" if _ir_contains_resource(pattern.variable.type) else value
            keyword = "auto" if _ir_contains_resource(pattern.variable.type) else "const auto"
            self._line(f"{keyword} tyv_local_{pattern.variable.identifier} = {transfer};")
        elif isinstance(pattern, IRVariantPattern) and pattern.payload is not None:
            if pattern.variant == "Some":
                self._pattern_bindings(pattern.payload, f"({value}).value()")
            elif pattern.variant == "Ok":
                self._pattern_bindings(pattern.payload, f"std::get<0>({value})")
            elif pattern.variant == "Err":
                self._pattern_bindings(pattern.payload, f"std::get<1>({value})")

    def _output(self, statement: IROutput) -> None:
        stream = "std::cout" if statement.channel == "stdout" else "std::cerr"
        value = self._expression(statement.value)
        temporary = self._new_temporary()
        self._line(f"const auto {temporary} = {value};")
        if statement.value.type == IRType.STRING:
            self._line(f"{stream}.write({temporary}.data(), static_cast<std::streamsize>({temporary}.size()));")
            self._line(f"{stream}.put('\\n');")
        elif statement.value.type == IRType.FLOAT:
            self._line(f"{stream} << tyvera_runtime::format_float({temporary}) << '\\n';")
        elif statement.value.type == IRType.BOOL:
            self._line(f"{stream} << ({temporary} ? \"true\" : \"false\") << '\\n';")
        elif statement.value.type == IRType.BYTE:
            self._line(f"{stream} << static_cast<unsigned int>({temporary}) << '\\n';")
        else:
            self._line(f"{stream} << {temporary} << '\\n';")

    def _expression(self, expression: IRExpression) -> str:
        if isinstance(expression, IRLiteral):
            if expression.type == IRType.STRING:
                encoded = expression.value.encode("utf-8")
                literal = _cpp_byte_string(encoded)
                return f"std::string({literal}, {len(encoded)})"
            if expression.type == IRType.BOOL:
                return "true" if expression.value else "false"
            if expression.type == IRType.BYTE:
                return f"static_cast<std::uint8_t>({expression.value})"
            if expression.type == IRType.FLOAT:
                return _cpp_float_literal(expression.value)
            if expression.value == -(1 << 63):
                return "(std::numeric_limits<std::int64_t>::min())"
            return f"(static_cast<std::int64_t>({expression.value}LL))"
        if isinstance(expression, IRLocalRef):
            return f"tyv_local_{expression.variable_id}"
        if isinstance(expression, IRMove):
            return f"std::move({self._expression(expression.operand)})"
        if isinstance(expression, IRCall):
            arguments = [self._expression(argument) for argument in expression.arguments]
            if not arguments:
                return f"{expression.function}()"
            temporary_names = [self._new_temporary() for _ in arguments]
            result_type = self._cpp_type(expression.type)
            lines = ["([&]() -> " + result_type + " {"]
            lines.extend(
                f"    {'auto' if _ir_contains_resource(argument_ir.type) else 'const auto'} {name} = {argument};"
                for name, argument, argument_ir in zip(temporary_names, arguments, expression.arguments)
            )
            call_arguments = [
                f"std::move({name})" if _ir_contains_resource(argument.type) else name
                for name, argument in zip(temporary_names, expression.arguments)
            ]
            call = f"{expression.function}({', '.join(call_arguments)})"
            if expression.type == IRType.VOID:
                lines.append(f"    {call};")
            else:
                lines.append(f"    return {call};")
            lines.append("}())")
            return "\n".join(lines)
        if isinstance(expression, IRVariant):
            if not isinstance(expression.type, IRAppliedType):
                raise ValueError("validated variant expression has no applied type")
            if expression.type.name == "Option":
                if expression.variant == "None":
                    return "std::nullopt"
                payload_type = self._cpp_type(expression.type.arguments[0])
                return f"std::optional<{payload_type}>{{std::in_place, {self._expression(expression.payload)}}}"
            if expression.type.name == "Result":
                result_type = self._cpp_type(expression.type)
                index = 0 if expression.variant == "Ok" else 1
                return f"{result_type}{{std::in_place_index<{index}>, {self._expression(expression.payload)}}}"
            raise ValueError(f"unsupported variant constructor for {ir_type_name(expression.type)}")
        if isinstance(expression, IRBuiltinCall):
            runtime_functions = {
                "hostOS": "host_os",
                "hostArchitecture": "host_architecture",
                "monotonicMilliseconds": "monotonic_milliseconds",
                "milliseconds": "duration_milliseconds",
                "seconds": "duration_seconds",
                "durationFromSeconds": "duration_from_seconds",
                "byteFromInt": "byte_from_int",
                "bytesFromString": "bytes_from_string",
                "byteBuffer": "byte_buffer",
                "readText": "read_text",
                "writeText": "write_text",
                "readBytes": "read_bytes",
                "writeBytes": "write_bytes",
                "pathJoin": "path_join",
                "pathExists": "path_exists",
                "openText": "open_text",
                "openTextReader": "open_text_reader",
                "openTextWriter": "open_text_writer",
                "runTasks": "run_tasks",
                "resolveHost": "resolve_host",
                "tcpConnect": "tcp_connect",
                "tlsConnect": "tls_connect",
                "tcpListen": "tcp_listen",
            }
            function = runtime_functions.get(expression.builtin)
            if function is None:
                raise ValueError(f"unknown native builtin: {expression.builtin}")
            temporaries = [self._new_temporary() for _ in expression.arguments]
            result_type = self._cpp_type(expression.type)
            lines = ["([&]() -> " + result_type + " {"]
            for name, argument in zip(temporaries, expression.arguments):
                keyword = "auto" if _ir_contains_resource(argument.type) else "const auto"
                lines.append(f"    {keyword} {name} = {self._expression(argument)};")
            values = list(temporaries)
            if expression.builtin in {"byteFromInt", "byteBuffer", "seconds"}:
                values.append(_location_args(expression.location))
            elif expression.builtin == "openText":
                values.extend([_cpp_string_literal("TextFile"), _location_args(expression.location)])
            elif expression.builtin == "openTextReader":
                values.append(_location_args(expression.location))
            elif expression.builtin == "openTextWriter":
                values.extend([_location_args(expression.location)])
            elif expression.builtin == "pathJoin":
                values.append(_location_args(expression.location))
            elif expression.builtin in {"runTasks", "resolveHost", "tcpConnect", "tlsConnect", "tcpListen"}:
                values.append(_location_args(expression.location))
            call = f"tyvera_runtime::{function}({', '.join(values)})"
            lines.append(f"    return {call};")
            lines.append("}())")
            return "\n".join(lines)
        if isinstance(expression, IRCollectionCreate):
            if not isinstance(expression.type, IRAppliedType):
                raise ValueError("validated collection constructor has no applied type")
            arguments = ", ".join(self._cpp_type(item) for item in expression.type.arguments)
            constructor = "make_list" if expression.type.name == "List" else "make_map"
            return f"tyvera_runtime::{constructor}<{arguments}>()"
        if isinstance(expression, IRCollectionLength):
            receiver = self._expression(expression.receiver)
            receiver_type = expression.receiver.type
            if not isinstance(receiver_type, IRAppliedType):
                raise ValueError("validated collection length has no applied receiver type")
            field = "values" if receiver_type.name == "List" else "entries"
            return f"static_cast<std::int64_t>(({receiver})->{field}.size())"
        if isinstance(expression, IRMethodCall):
            return self._method_call(expression)
        if isinstance(expression, IRUnary):
            operand = self._expression(expression.operand)
            if expression.operator == "!":
                return f"(!({operand}))"
            if expression.type == IRType.INT:
                if isinstance(expression.operand, IRLiteral) and expression.operand.value == 1 << 63:
                    return "(std::numeric_limits<std::int64_t>::min())"
                return f"tyvera_runtime::checked_neg({operand}, {_location_args(expression.location)})"
            return f"tyvera_runtime::checked_float(-({operand}), {_location_args(expression.location)})"
        if isinstance(expression, IRBinary):
            left = self._expression(expression.left)
            right = self._expression(expression.right)
            if expression.operator in {"and", "or"}:
                operator = "&&" if expression.operator == "and" else "||"
                return f"(({left}) {operator} ({right}))"
            left_temp = self._new_temporary()
            right_temp = self._new_temporary()
            result = self._binary_value(expression, left_temp, right_temp)
            result_type = self._cpp_type(expression.type)
            return (
                f"([&]() -> {result_type} {{ const auto {left_temp} = {left}; "
                f"const auto {right_temp} = {right}; return {result}; }}())"
            )
        raise ValueError(f"unhandled IR expression: {type(expression).__name__}")

    def _binary_value(self, expression: IRBinary, left: str, right: str) -> str:
        operator = expression.operator
        location = _location_args(expression.location)
        left_type = expression.left.type
        right_type = expression.right.type
        if operator in {"==", "!=", "<", "<=", ">", ">="} and left_type != right_type:
            if left_type == IRType.INT and right_type == IRType.FLOAT:
                comparison = f"tyvera_runtime::compare_int_float({left}, {right})"
            else:
                comparison = f"-tyvera_runtime::compare_int_float({right}, {left})"
            compare_operator = {"==": "== 0", "!=": "!= 0", "<": "< 0", "<=": "<= 0", ">": "> 0", ">=": ">= 0"}[operator]
            return f"({comparison} {compare_operator})"
        if operator in {"==", "!=", "<", "<=", ">", ">="}:
            if operator in {"==", "!="} and isinstance(left_type, IRAppliedType):
                result = f"tyvera_runtime::equal({left}, {right})"
                return f"(!{result})" if operator == "!=" else result
            return f"({left} {operator} {right})"
        if expression.type == IRType.STRING:
            return f"({left} + {right})"
        if expression.type == IRType.DURATION:
            helper = {"+": "duration_add", "-": "duration_sub"}[operator]
            return f"tyvera_runtime::{helper}({left}, {right}, {location})"
        if expression.type == IRType.INT:
            helpers = {
                "+": "checked_add",
                "-": "checked_sub",
                "*": "checked_mul",
                "%": "checked_remainder",
            }
            return f"tyvera_runtime::{helpers[operator]}({left}, {right}, {location})"
        helpers = {"+": "checked_add_float", "-": "checked_sub_float", "*": "checked_mul_float",
                   "/": "checked_div_float", "%": "checked_remainder_float"}
        left_value = f"static_cast<double>({left})" if left_type == IRType.INT else left
        right_value = f"static_cast<double>({right})" if right_type == IRType.INT else right
        return f"tyvera_runtime::{helpers[operator]}({left_value}, {right_value}, {location})"

    def _new_temporary(self) -> str:
        result = f"tyv_temporary_{self.temporary}"
        self.temporary += 1
        return result

    def _method_call(self, expression: IRMethodCall) -> str:
        receiver = self._new_temporary()
        arguments = [self._new_temporary() for _ in expression.arguments]
        result_type = self._cpp_type(expression.type)
        lines = ["([&]() -> " + result_type + " {"]
        receiver_keyword = "auto" if _ir_contains_resource(expression.receiver.type) or expression.receiver.type == IRType.BYTE_BUFFER else "const auto"
        lines.append(f"    {receiver_keyword} {receiver} = {self._expression(expression.receiver)};")
        for name, argument in zip(arguments, expression.arguments):
            lines.append(f"    const auto {name} = {self._expression(argument)};")
        call = self._method_call_expression(expression, receiver, arguments)
        if expression.type == IRType.VOID:
            lines.append(f"    {call};")
        else:
            lines.append(f"    return {call};")
        lines.append("}())")
        return "\n".join(lines)

    @staticmethod
    def _method_call_expression(expression: IRMethodCall, receiver: str, arguments: list[str]) -> str:
        method = expression.method
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "Task":
            if method == "withTimeout":
                move_result = "true" if _ir_contains_resource(expression.type) else "false"
                return (
                    f"tyvera_runtime::task_with_timeout(std::move({receiver}), {arguments[0]}, "
                    f"{move_result}, {_location_args(expression.location)})"
                )
            functions = {"cancel": "cancel", "isCancelled": "is_cancelled", "isDone": "is_done"}
            return f"({receiver}).{functions[method]}()"
        if expression.receiver.type == IRType.DURATION and method == "inMilliseconds":
            return f"({receiver}).milliseconds"
        if expression.receiver.type == IRType.BYTE and method == "toInt":
            return f"static_cast<std::int64_t>({receiver})"
        if expression.receiver.type == IRType.BYTES:
            if method == "length":
                return f"static_cast<std::int64_t>({receiver}.size())"
            if method == "toString":
                return f"tyvera_runtime::bytes_to_string({receiver})"
            if method == "get":
                return f"tyvera_runtime::bytes_get({receiver}, {arguments[0]})"
            if method == "slice":
                return f"tyvera_runtime::bytes_slice({receiver}, {arguments[0]}, {arguments[1]})"
        if expression.receiver.type == IRType.BYTE_BUFFER:
            functions = {
                "length": "byte_buffer_length", "get": "byte_buffer_get",
                "set": "byte_buffer_set", "toBytes": "byte_buffer_to_bytes",
            }
            values = [receiver, *arguments]
            if method != "length":
                values.append(_location_args(expression.location))
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if expression.receiver.type in {IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER}:
            values = [receiver, *arguments]
            values.append(_location_args(expression.location))
            functions = {
                "readAll": "text_read_all", "write": "text_write", "append": "text_append",
                "flush": "text_flush", "close": "text_close", "isOpen": "text_is_open",
            }
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if expression.receiver.type == IRType.SOCKET_ADDRESS:
            return f"tyvera_runtime::socket_address_{method}({receiver})"
        if expression.receiver.type == IRType.TCP_CONNECTION:
            functions = {
                "read": "tcp_read", "write": "tcp_write", "writeAll": "tcp_write_all",
                "localAddress": "tcp_local_address", "remoteAddress": "tcp_remote_address",
                "close": "tcp_close", "isOpen": "tcp_is_open",
            }
            values = [receiver, *arguments]
            if method == "write":
                values.append("false")
            if method not in {"close", "isOpen"}:
                values.append(_location_args(expression.location))
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if expression.receiver.type == IRType.TLS_CONNECTION:
            functions = {
                "read": "tls_read", "write": "tls_write", "writeAll": "tls_write_all",
                "localAddress": "tls_local_address", "remoteAddress": "tls_remote_address",
                "close": "tls_close", "isOpen": "tls_is_open",
            }
            values = [receiver, *arguments]
            if method == "write":
                values.append("false")
            if method not in {"close", "isOpen"}:
                values.append(_location_args(expression.location))
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if expression.receiver.type == IRType.TCP_LISTENER:
            functions = {
                "accept": "tcp_accept", "localAddress": "listener_local_address",
                "close": "listener_close", "isOpen": "listener_is_open",
            }
            values = [receiver, *arguments]
            if method in {"accept", "localAddress"}:
                values.append(_location_args(expression.location))
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "List":
            element_type = _CppGenerator._cpp_type(expression.receiver.type.arguments[0])
            functions = {
                "append": "list_append", "insert": "list_insert", "get": "list_get",
                "set": "list_set", "removeAt": "list_remove_at", "contains": "list_contains",
                "clear": "list_clear",
            }
            values = [receiver, *arguments]
            if method in {"insert", "set"}:
                values.append(_location_args(expression.location))
            return f"tyvera_runtime::{functions[method]}<{element_type}>({', '.join(values)})"
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "Map":
            key_type, value_type = (
                _CppGenerator._cpp_type(argument) for argument in expression.receiver.type.arguments
            )
            functions = {
                "put": "map_put", "get": "map_get", "remove": "map_remove",
                "containsKey": "map_contains_key", "keys": "map_keys", "values": "map_values",
                "clear": "map_clear",
            }
            return (
                f"tyvera_runtime::{functions[method]}<{key_type}, {value_type}>"
                f"({', '.join([receiver, *arguments])})"
            )
        raise ValueError(f"unhandled validated IR method '{method}'")


def _cpp_byte_string(value: bytes) -> str:
    if not value:
        return '""'
    return '"' + "".join(f"\\x{byte:02x}" for byte in value) + '"'


def _cpp_float_literal(value: float) -> str:
    # Hexadecimal floating literals preserve the exact binary64 value in C++17.
    return value.hex()


def _location_args(location: Location) -> str:
    encoded = location.filename.encode("utf-8")
    return f"{_cpp_byte_string(encoded)}, {location.line}, {location.column}"


def _cpp_string_literal(value: str) -> str:
    encoded = value.encode("utf-8")
    return f"std::string({_cpp_byte_string(encoded)}, {len(encoded)})"


def _ir_contains_resource(type_ref: IRTypeRef) -> bool:
    if type_ref in {
        IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER,
        IRType.TCP_CONNECTION, IRType.TLS_CONNECTION, IRType.TCP_LISTENER,
    }:
        return True
    return isinstance(type_ref, IRAppliedType) and any(_ir_contains_resource(item) for item in type_ref.arguments)


def _module_uses_tls(module: IRModule) -> bool:
    seen: set[int] = set()

    def contains_tls(value) -> bool:
        if isinstance(value, IRType):
            return value == IRType.TLS_CONNECTION
        if isinstance(value, IRAppliedType):
            return any(contains_tls(argument) for argument in value.arguments)
        if isinstance(value, (tuple, list)):
            return any(contains_tls(item) for item in value)
        if not is_dataclass(value):
            return False
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        return any(contains_tls(getattr(value, item.name)) for item in fields(value))

    return contains_tls(module)


_RUNTIME = r'''#include <charconv>
#include <cerrno>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

#if defined(__linux__)
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#if defined(TYVERA_NATIVE_TLS)
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#endif
#endif

namespace tyvera_runtime {
/*TYVERA_PROJECT_ROOT*/
inline std::filesystem::path project_root() {
    return std::filesystem::u8path(project_root_text);
}

enum class ExitCode : int {
    success = 0,
    controlled_stop = 0,
    runtime_failure = 1,
    supervisor_escalation = 70,
    initialization_failure = 71,
    platform_failure = 72,
};

inline volatile std::sig_atomic_t requested_shutdown_signal = 0;
inline void shutdown_signal_handler(int number) noexcept {
    // Signal context may only record a small scalar. The first signal owns the
    // shutdown request; repeats do not bypass deterministic RAII cleanup.
    if (requested_shutdown_signal == 0) requested_shutdown_signal = number;
}
inline bool install_shutdown_handlers() noexcept {
    if (std::signal(SIGINT, shutdown_signal_handler) == SIG_ERR) return false;
#ifdef SIGTERM
    if (std::signal(SIGTERM, shutdown_signal_handler) == SIG_ERR) return false;
#endif
#ifdef SIGPIPE
    // TLS BIO writes must report peer disconnects instead of terminating the process.
    if (std::signal(SIGPIPE, SIG_IGN) == SIG_ERR) return false;
#endif
    return true;
}
inline std::string signal_name(int number) {
    if (number == SIGINT) return "SIGINT";
#ifdef SIGTERM
    if (number == SIGTERM) return "SIGTERM";
#endif
    return "signal";
}
struct SignalShutdown final : std::exception {
    std::string file;
    int line;
    int column;
    int exit_code;
    std::string message;
    SignalShutdown(std::string source_file, int source_line, int source_column, int number)
        : file(std::move(source_file)), line(source_line), column(source_column),
          exit_code(128 + number), message("runtime shutdown requested by " + signal_name(number)) {}
    const char* what() const noexcept override { return message.c_str(); }
};
inline void check_shutdown(const std::string& file, int line, int column) {
    const int number = static_cast<int>(requested_shutdown_signal);
    if (number != 0) throw SignalShutdown(file, line, column, number);
}
inline std::int64_t monotonic_milliseconds() noexcept {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
struct Duration {
    std::int64_t milliseconds = 0;
    friend constexpr bool operator==(Duration left, Duration right) noexcept {
        return left.milliseconds == right.milliseconds;
    }
    friend constexpr bool operator!=(Duration left, Duration right) noexcept { return !(left == right); }
    friend constexpr bool operator<(Duration left, Duration right) noexcept {
        return left.milliseconds < right.milliseconds;
    }
    friend constexpr bool operator<=(Duration left, Duration right) noexcept { return !(right < left); }
    friend constexpr bool operator>(Duration left, Duration right) noexcept { return right < left; }
    friend constexpr bool operator>=(Duration left, Duration right) noexcept { return !(left < right); }
};
inline std::chrono::steady_clock::time_point duration_deadline(Duration duration) noexcept {
    const auto now = std::chrono::steady_clock::now();
    if (duration.milliseconds <= 0) return now;
    const auto available = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::time_point::max() - now).count();
    if (duration.milliseconds > available) return std::chrono::steady_clock::time_point::max();
    return now + std::chrono::milliseconds(duration.milliseconds);
}
inline std::string host_os() {
#if defined(__linux__)
    return "linux";
#elif defined(_WIN32)
    return "windows";
#elif defined(__APPLE__)
    return "macos";
#else
    return "other";
#endif
}
inline std::string host_architecture() {
#if defined(__x86_64__) || defined(_M_X64)
    return "x86_64";
#elif defined(__aarch64__) || defined(_M_ARM64)
    return "aarch64";
#elif defined(__i386__) || defined(_M_IX86)
    return "x86";
#else
    return "other";
#endif
}

struct Failure final : std::exception {
    std::string file;
    int line;
    int column;
    std::string message;
    Failure(std::string source_file, int source_line, int source_column, std::string text)
        : file(std::move(source_file)), line(source_line), column(source_column), message(std::move(text)) {}
    const char* what() const noexcept override { return message.c_str(); }
};
struct TaskCancelled final : std::exception {
    const char* what() const noexcept override { return "task was cancelled"; }
};

inline thread_local std::unique_lock<std::mutex>* active_execution_lock = nullptr;
constexpr std::size_t MAX_NATIVE_TASKS_PER_RUN = 64;
struct TaskControl {
    std::atomic<bool> started{false};
    std::atomic<bool> done{false};
    std::atomic<bool> cancellation_requested{false};
    std::atomic<bool> deadline_cancellation{false};
    bool serialized = true;
    std::optional<std::chrono::steady_clock::time_point> start_deadline;
    std::weak_ptr<TaskControl> cancellation_target;
    std::string source_file;
    int source_line = 1;
    int source_column = 1;
    TaskControl* owner = nullptr;
    std::mutex lifecycle_mutex;
    std::condition_variable lifecycle_changed;
    mutable std::mutex children_mutex;
    std::vector<std::shared_ptr<TaskControl>> children;
    mutable std::mutex child_failure_mutex;
    std::string child_failure;
    virtual ~TaskControl() = default;
    virtual void start() = 0;
    virtual void join() = 0;
    virtual std::string failure_message() const = 0;
    virtual void notify_waiters() noexcept {}
    bool prepare_start() {
        std::lock_guard<std::mutex> lock(lifecycle_mutex);
        bool expected = false;
        if (!started.compare_exchange_strong(expected, true)) return false;
        if (start_deadline && std::chrono::steady_clock::now() >= *start_deadline
            && !cancellation_requested.load(std::memory_order_acquire)) {
            deadline_cancellation.store(true, std::memory_order_release);
            cancellation_requested.store(true, std::memory_order_release);
        }
        return true;
    }
    void defer_start_until(std::chrono::steady_clock::time_point deadline) {
        std::lock_guard<std::mutex> lock(lifecycle_mutex);
        if (started.load(std::memory_order_acquire) || done.load(std::memory_order_acquire)) return;
        if (!start_deadline || deadline < *start_deadline) start_deadline = deadline;
    }
    bool request_cancel(bool due_to_deadline = false) {
        std::vector<std::shared_ptr<TaskControl>> owned;
        std::shared_ptr<TaskControl> linked;
        {
            std::lock_guard<std::mutex> lock(lifecycle_mutex);
            if (done.load(std::memory_order_acquire) || cancellation_requested.load(std::memory_order_acquire))
                return false;
            {
                std::lock_guard<std::mutex> failure_lock(child_failure_mutex);
                if (!child_failure.empty()) return false;
            }
            if (due_to_deadline) deadline_cancellation.store(true, std::memory_order_release);
            cancellation_requested.store(true, std::memory_order_release);
            linked = cancellation_target.lock();
            std::lock_guard<std::mutex> children_lock(children_mutex);
            owned = children;
        }
        if (linked) linked->request_cancel();
        for (auto item = owned.rbegin(); item != owned.rend(); ++item) (*item)->request_cancel();
        lifecycle_changed.notify_all();
        notify_waiters();
        return true;
    }
    bool is_cancelled() const noexcept { return cancellation_requested.load(std::memory_order_acquire); }
    bool is_done() const {
        if (done.load(std::memory_order_acquire) || is_cancelled()) return true;
        std::lock_guard<std::mutex> lock(child_failure_mutex);
        return !child_failure.empty();
    }
    void add_child(const std::shared_ptr<TaskControl>& child) {
        child->owner = this;
        bool cancel_now = false;
        {
            std::lock_guard<std::mutex> lock(children_mutex);
            children.push_back(child);
            cancel_now = cancellation_requested.load(std::memory_order_acquire);
        }
        if (cancel_now) child->request_cancel();
    }
    std::vector<std::shared_ptr<TaskControl>> children_snapshot() const {
        std::lock_guard<std::mutex> lock(children_mutex);
        return children;
    }
    void cancel_children() {
        const auto owned = children_snapshot();
        for (auto item = owned.rbegin(); item != owned.rend(); ++item) (*item)->request_cancel();
    }
    void record_child_failure(const std::string& message) {
        if (message.empty()) return;
        {
            std::lock_guard<std::mutex> lock(child_failure_mutex);
            if (child_failure.empty()) child_failure = message;
        }
        cancel_children();
        lifecycle_changed.notify_all();
    }
    void check_interruption() const {
        std::string failure;
        {
            std::lock_guard<std::mutex> lock(child_failure_mutex);
            failure = child_failure;
        }
        if (!failure.empty())
            throw Failure(source_file, source_line, source_column, "owned child task failed: " + failure);
        if (is_cancelled()) throw TaskCancelled{};
    }
    void wait_for_children(bool check_owner) {
        const auto owned = children_snapshot();
        auto* execution_lock = active_execution_lock;
        const bool resume_serialized = execution_lock && execution_lock->owns_lock();
        if (resume_serialized) execution_lock->unlock();
        try {
            for (const auto& child : owned) {
                while (!child->done.load(std::memory_order_acquire)) {
                    std::unique_lock<std::mutex> lock(child->lifecycle_mutex);
                    child->lifecycle_changed.wait_for(lock, std::chrono::milliseconds(50), [&child]() {
                        return child->done.load(std::memory_order_acquire);
                    });
                    lock.unlock();
                    if (check_owner) check_interruption();
                }
                if (!check_owner) continue;
                check_interruption();
                const auto message = child->failure_message();
                if (!message.empty()) {
                    record_child_failure(message);
                    check_interruption();
                }
            }
        } catch (...) {
            if (resume_serialized && !execution_lock->owns_lock()) execution_lock->lock();
            throw;
        }
        if (resume_serialized && !execution_lock->owns_lock()) execution_lock->lock();
    }
    void notify_owner_failure() {
        if (!owner) return;
        const auto message = failure_message();
        if (!message.empty()) owner->record_child_failure(message);
    }
};
inline std::string cleanup_failure_suffix();
struct NativeScheduler {
    std::mutex mutex;
    std::mutex execution_mutex;
    std::condition_variable changed;
    std::vector<std::shared_ptr<TaskControl>> tasks;
    bool running = false;
    std::size_t completed = 0;
};
inline NativeScheduler& native_scheduler() {
    static NativeScheduler value;
    return value;
}
inline thread_local TaskControl* active_task_control = nullptr;
class NativeTaskExecution {
    std::unique_lock<std::mutex> lock_;
    std::unique_lock<std::mutex>* previous_;
    TaskControl* previous_task_;
public:
    explicit NativeTaskExecution(TaskControl& task)
        : lock_(native_scheduler().execution_mutex, std::defer_lock), previous_(active_execution_lock),
          previous_task_(active_task_control) {
        active_task_control = &task;
        if (task.serialized) {
            lock_.lock();
            active_execution_lock = &lock_;
        } else {
            active_execution_lock = nullptr;
        }
    }
    ~NativeTaskExecution() { active_execution_lock = previous_; active_task_control = previous_task_; }
};
inline void task_checkpoint() {
    if (active_task_control) active_task_control->check_interruption();
    auto* execution_lock = active_execution_lock;
    if (!execution_lock || !execution_lock->owns_lock()) return;
    execution_lock->unlock();
    std::this_thread::yield();
    execution_lock->lock();
    if (active_task_control) active_task_control->check_interruption();
}
inline void register_native_task(const std::shared_ptr<TaskControl>& task,
                                 const std::string& file, int line, int column) {
    auto& scheduler = native_scheduler();
    bool start_now = false;
    {
        std::lock_guard<std::mutex> lock(scheduler.mutex);
        if (scheduler.tasks.size() >= MAX_NATIVE_TASKS_PER_RUN)
            throw Failure(file, line, column, "native task capacity exceeded (maximum 64 tasks per run)");
        if (active_task_control) active_task_control->add_child(task);
        scheduler.tasks.push_back(task);
        start_now = scheduler.running;
    }
    scheduler.changed.notify_all();
    if (start_now) task->start();
}

template <typename T> struct TaskState final : TaskControl, std::enable_shared_from_this<TaskState<T>> {
    std::function<T()> work;
    mutable std::mutex mutex;
    std::condition_variable ready;
    std::thread worker;
    std::optional<T> result;
    std::exception_ptr error;
    std::string cleanup_failure;
    explicit TaskState(std::function<T()> operation, bool serialized_execution,
                       std::string source_file, int source_line, int source_column)
        : work(std::move(operation)) {
        serialized = serialized_execution;
        this->source_file = std::move(source_file);
        this->source_line = source_line;
        this->source_column = source_column;
    }
    void start() override {
        if (!prepare_start()) return;
        auto self = this->shared_from_this();
        try {
            worker = std::thread([self]() {
                std::exception_ptr failure;
                std::optional<T> value;
                try {
                    NativeTaskExecution execution(*self);
                    self->check_interruption();
                    value.emplace(self->work());
                    self->check_interruption();
                    self->wait_for_children(true);
                } catch (...) {
                    failure = std::current_exception();
                }
                self->work = nullptr;
                if (failure) {
                    value.reset();
                    self->cancel_children();
                    self->wait_for_children(false);
                }
                std::string cleanup;
                try { cleanup = cleanup_failure_suffix(); } catch (...) {}
                {
                    std::unique_lock<std::mutex> lifecycle_lock(self->lifecycle_mutex);
                    if (self->cancellation_requested.load(std::memory_order_acquire)) {
                        lifecycle_lock.unlock();
                        self->cancel_children();
                        self->wait_for_children(false);
                        lifecycle_lock.lock();
                        failure = std::make_exception_ptr(TaskCancelled{});
                        if (value) {
                            value.reset();
                            try { cleanup += cleanup_failure_suffix(); } catch (...) {}
                        }
                    }
                    std::lock_guard<std::mutex> lock(self->mutex);
                    self->error = failure;
                    self->cleanup_failure = std::move(cleanup);
                    if (value) {
                        try { self->result.emplace(std::move(*value)); }
                        catch (...) { self->error = std::current_exception(); }
                    }
                    self->done.store(true, std::memory_order_release);
                }
                self->lifecycle_changed.notify_all();
                auto& scheduler = native_scheduler();
                {
                    std::lock_guard<std::mutex> lock(scheduler.mutex);
                    ++scheduler.completed;
                }
                self->ready.notify_all();
                scheduler.changed.notify_all();
                self->notify_owner_failure();
            });
        } catch (...) {
            const auto failure = std::current_exception();
            work = nullptr;
            std::string cleanup;
            try { cleanup = cleanup_failure_suffix(); } catch (...) {}
            {
                std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex);
                std::lock_guard<std::mutex> lock(mutex);
                error = failure;
                cleanup_failure = std::move(cleanup);
                done.store(true, std::memory_order_release);
            }
            lifecycle_changed.notify_all();
            auto& scheduler = native_scheduler();
            {
                std::lock_guard<std::mutex> lock(scheduler.mutex);
                ++scheduler.completed;
            }
            ready.notify_all();
            scheduler.changed.notify_all();
            notify_owner_failure();
        }
    }
    T await(const std::string& file, int line, int column) {
        if (!started.load(std::memory_order_acquire)) start();
        std::unique_lock<std::mutex> lock(mutex);
        while (!done.load(std::memory_order_acquire)) {
            ready.wait_for(lock, std::chrono::milliseconds(50));
            if (done.load(std::memory_order_acquire)) break;
            lock.unlock();
            if (active_task_control) active_task_control->check_interruption();
            lock.lock();
        }
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const TaskCancelled&) { throw Failure(file, line, column, "task was cancelled"); }
        }
        if (!result) throw Failure(file, line, column, "native task completed without a result");
        return *result;
    }
    T await_move(const std::string& file, int line, int column) {
        if (!started.load(std::memory_order_acquire)) start();
        std::unique_lock<std::mutex> lock(mutex);
        while (!done.load(std::memory_order_acquire)) {
            ready.wait_for(lock, std::chrono::milliseconds(50));
            if (done.load(std::memory_order_acquire)) break;
            lock.unlock();
            if (active_task_control) active_task_control->check_interruption();
            lock.lock();
        }
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const TaskCancelled&) { throw Failure(file, line, column, "task was cancelled"); }
        }
        if (!result) throw Failure(file, line, column, "native task result was already consumed");
        T value = std::move(*result);
        result.reset();
        return value;
    }
    bool wait_until(std::chrono::steady_clock::time_point deadline, const TaskControl* waiter) {
        if (!started.load(std::memory_order_acquire)) start();
        std::unique_lock<std::mutex> lock(mutex);
        ready.wait_until(lock, deadline, [this, waiter]() {
            return done.load(std::memory_order_acquire) || is_cancelled()
                || (waiter && waiter->is_cancelled());
        });
        return done.load(std::memory_order_acquire);
    }
    void notify_waiters() noexcept override { ready.notify_all(); }
    void join() override { if (worker.joinable()) worker.join(); }
    std::string failure_message() const override {
        std::lock_guard<std::mutex> lock(mutex);
        std::string message;
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const Failure& failure) { message = failure.what(); }
            catch (const TaskCancelled&) {}
            catch (...) { message = "native task failed"; }
        }
        return message + cleanup_failure;
    }
};

template <> struct TaskState<void> final : TaskControl, std::enable_shared_from_this<TaskState<void>> {
    std::function<void()> work;
    mutable std::mutex mutex;
    std::condition_variable ready;
    std::thread worker;
    std::exception_ptr error;
    std::string cleanup_failure;
    explicit TaskState(std::function<void()> operation, bool serialized_execution,
                       std::string source_file, int source_line, int source_column)
        : work(std::move(operation)) {
        serialized = serialized_execution;
        this->source_file = std::move(source_file);
        this->source_line = source_line;
        this->source_column = source_column;
    }
    void start() override {
        if (!prepare_start()) return;
        auto self = this->shared_from_this();
        try {
            worker = std::thread([self]() {
                std::exception_ptr failure;
                try {
                    NativeTaskExecution execution(*self);
                    self->check_interruption();
                    self->work();
                    self->check_interruption();
                    self->wait_for_children(true);
                } catch (...) { failure = std::current_exception(); }
                self->work = nullptr;
                if (failure) {
                    self->cancel_children();
                    self->wait_for_children(false);
                }
                std::string cleanup;
                try { cleanup = cleanup_failure_suffix(); } catch (...) {}
                {
                    std::unique_lock<std::mutex> lifecycle_lock(self->lifecycle_mutex);
                    if (self->cancellation_requested.load(std::memory_order_acquire)) {
                        lifecycle_lock.unlock();
                        self->cancel_children();
                        self->wait_for_children(false);
                        lifecycle_lock.lock();
                        failure = std::make_exception_ptr(TaskCancelled{});
                        try { cleanup += cleanup_failure_suffix(); } catch (...) {}
                    }
                    std::lock_guard<std::mutex> lock(self->mutex);
                    self->error = failure;
                    self->cleanup_failure = std::move(cleanup);
                    self->done.store(true, std::memory_order_release);
                }
                self->lifecycle_changed.notify_all();
                auto& scheduler = native_scheduler();
                { std::lock_guard<std::mutex> lock(scheduler.mutex); ++scheduler.completed; }
                self->ready.notify_all();
                scheduler.changed.notify_all();
                self->notify_owner_failure();
            });
        } catch (...) {
            const auto failure = std::current_exception();
            work = nullptr;
            std::string cleanup;
            try { cleanup = cleanup_failure_suffix(); } catch (...) {}
            {
                std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex);
                std::lock_guard<std::mutex> lock(mutex);
                error = failure;
                cleanup_failure = std::move(cleanup);
                done.store(true, std::memory_order_release);
            }
            lifecycle_changed.notify_all();
            auto& scheduler = native_scheduler();
            { std::lock_guard<std::mutex> lock(scheduler.mutex); ++scheduler.completed; }
            ready.notify_all();
            scheduler.changed.notify_all();
            notify_owner_failure();
        }
    }
    void await(const std::string& file, int line, int column) {
        if (!started.load(std::memory_order_acquire)) start();
        std::unique_lock<std::mutex> lock(mutex);
        while (!done.load(std::memory_order_acquire)) {
            ready.wait_for(lock, std::chrono::milliseconds(50));
            if (done.load(std::memory_order_acquire)) break;
            lock.unlock();
            if (active_task_control) active_task_control->check_interruption();
            lock.lock();
        }
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const TaskCancelled&) { throw Failure(file, line, column, "task was cancelled"); }
        }
    }
    bool wait_until(std::chrono::steady_clock::time_point deadline, const TaskControl* waiter) {
        if (!started.load(std::memory_order_acquire)) start();
        std::unique_lock<std::mutex> lock(mutex);
        ready.wait_until(lock, deadline, [this, waiter]() {
            return done.load(std::memory_order_acquire) || is_cancelled()
                || (waiter && waiter->is_cancelled());
        });
        return done.load(std::memory_order_acquire);
    }
    void notify_waiters() noexcept override { ready.notify_all(); }
    void join() override { if (worker.joinable()) worker.join(); }
    std::string failure_message() const override {
        std::lock_guard<std::mutex> lock(mutex);
        std::string message;
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const Failure& failure) { message = failure.what(); }
            catch (const TaskCancelled&) {}
            catch (...) { message = "native task failed"; }
        }
        return message + cleanup_failure;
    }
};

template <typename T> struct Task {
    std::shared_ptr<TaskState<T>> state;
    bool cancel() const { return state && state->request_cancel(); }
    bool is_cancelled() const { return state && state->is_cancelled(); }
    bool is_done() const { return !state || state->is_done(); }
    T await(const std::string& file, int line, int column) const {
        if (!state) throw Failure(file, line, column, "cannot await an empty Task");
        auto* execution_lock = active_execution_lock;
        const bool resume_serialized = execution_lock && execution_lock->owns_lock();
        if (resume_serialized) execution_lock->unlock();
        try {
            T value = state->await(file, line, column);
            if (active_task_control && active_task_control != state.get())
                active_task_control->check_interruption();
            if (resume_serialized) execution_lock->lock();
            return value;
        } catch (...) {
            if (resume_serialized) execution_lock->lock();
            throw;
        }
    }
    T await_move(const std::string& file, int line, int column) const {
        if (!state) throw Failure(file, line, column, "cannot await an empty Task");
        auto* execution_lock = active_execution_lock;
        const bool resume_serialized = execution_lock && execution_lock->owns_lock();
        if (resume_serialized) execution_lock->unlock();
        try {
            T value = state->await_move(file, line, column);
            if (active_task_control && active_task_control != state.get())
                active_task_control->check_interruption();
            if (resume_serialized) execution_lock->lock();
            return value;
        } catch (...) {
            if (resume_serialized) execution_lock->lock();
            throw;
        }
    }
};
template <> struct Task<void> {
    std::shared_ptr<TaskState<void>> state;
    bool cancel() const { return state && state->request_cancel(); }
    bool is_cancelled() const { return state && state->is_cancelled(); }
    bool is_done() const { return !state || state->is_done(); }
    void await(const std::string& file, int line, int column) const {
        if (!state) throw Failure(file, line, column, "cannot await an empty Task");
        auto* execution_lock = active_execution_lock;
        const bool resume_serialized = execution_lock && execution_lock->owns_lock();
        if (resume_serialized) execution_lock->unlock();
        try {
            state->await(file, line, column);
            if (active_task_control && active_task_control != state.get())
                active_task_control->check_interruption();
            if (resume_serialized) execution_lock->lock();
        } catch (...) {
            if (resume_serialized) execution_lock->lock();
            throw;
        }
    }
};
template <typename T> inline Task<T> make_task_with_mode(std::function<T()> operation, bool serialized,
                                                         const std::string& file, int line, int column) {
    auto state = std::make_shared<TaskState<T>>(std::move(operation), serialized, file, line, column);
    register_native_task(state, file, line, column);
    return Task<T>{std::move(state)};
}
template <typename T> inline Task<T> make_task(std::function<T()> operation,
                                               const std::string& file, int line, int column) {
    return make_task_with_mode<T>(std::move(operation), true, file, line, column);
}
template <typename T> inline Task<T> make_io_task(std::function<T()> operation,
                                                  const std::string& file, int line, int column) {
    return make_task_with_mode<T>(std::move(operation), false, file, line, column);
}
template <typename T> inline Task<T> make_io_task_with_cancel(
    std::function<T()> operation, const std::shared_ptr<TaskControl>& cancellation_target,
    const std::string& file, int line, int column) {
    auto state = std::make_shared<TaskState<T>>(std::move(operation), false, file, line, column);
    state->cancellation_target = cancellation_target;
    register_native_task(state, file, line, column);
    return Task<T>{std::move(state)};
}
template <typename T> inline Task<T> task_with_timeout(
    Task<T> source, Duration duration, bool move_result,
    const std::string& file, int line, int column) {
    if (!source.state) throw Failure(file, line, column, "cannot time out an empty Task");
    const auto target = source.state;
    const auto deadline = duration_deadline(duration);
    if (!target->done.load(std::memory_order_acquire)) {
        if (duration.milliseconds <= 0) target->request_cancel(true);
        else target->defer_start_until(deadline);
    }
    auto operation = [source = std::move(source), target, deadline, move_result, file, line, column]() mutable -> T {
        if (!target->wait_until(deadline, active_task_control)) {
            if (active_task_control) active_task_control->check_interruption();
            if (!target->done.load(std::memory_order_acquire)
                && !target->is_cancelled()
                && std::chrono::steady_clock::now() >= deadline)
                target->request_cancel(true);
        }
        if (active_task_control) active_task_control->check_interruption();
        try {
            if constexpr (std::is_void_v<T>) {
                source.await(file, line, column);
                return;
            } else if (move_result) {
                return source.await_move(file, line, column);
            } else {
                return source.await(file, line, column);
            }
        } catch (const Failure&) {
            if (target->deadline_cancellation.load(std::memory_order_acquire))
                throw Failure(file, line, column, "task timed out");
            if (target->is_cancelled())
                throw Failure(file, line, column, "timed task was cancelled");
            throw;
        }
    };
    return make_io_task_with_cancel<T>(std::move(operation), target, file, line, column);
}

struct CleanupFailure { std::string file; int line; int column; std::string message; };
inline std::vector<CleanupFailure>& cleanup_failures() {
    static thread_local std::vector<CleanupFailure> errors;
    return errors;
}
inline void record_cleanup_failure(const std::string& file, int line, int column, const std::string& message) noexcept {
    try { cleanup_failures().push_back({file, line, column, message}); } catch (...) {}
}
inline std::string cleanup_failure_suffix() {
    auto& errors = cleanup_failures();
    if (errors.empty()) return {};
    std::string result = "; resource cleanup also failed: ";
    for (std::size_t index = 0; index < errors.size(); ++index) {
        if (index) result += "; ";
        const auto& error = errors[index];
        result += error.message;
    }
    errors.clear();
    return result;
}
class CleanupScope {
    std::size_t start_;
    int exceptions_;
    std::string file_;
    int line_;
    int column_;
public:
    CleanupScope(std::string file, int line, int column)
        : start_(cleanup_failures().size()), exceptions_(std::uncaught_exceptions()),
          file_(std::move(file)), line_(line), column_(column) {}
    ~CleanupScope() noexcept(false) {
        auto& errors = cleanup_failures();
        if (std::uncaught_exceptions() > exceptions_ || errors.size() <= start_) return;
        const auto first = errors[start_];
        std::string message = "resource cleanup failed: " + first.message;
        for (std::size_t index = start_ + 1; index < errors.size(); ++index)
            message += "; additional cleanup failures: " + errors[index].message;
        errors.erase(errors.begin() + static_cast<std::ptrdiff_t>(start_), errors.end());
        throw Failure(first.file.empty() ? file_ : first.file, first.line ? first.line : line_,
                      first.column ? first.column : column_, std::move(message));
    }
};
inline bool report_cleanup_failures() {
    auto& errors = cleanup_failures();
    if (errors.empty()) return false;
    for (std::size_t index = 0; index < errors.size(); ++index) {
        const auto& error = errors[index];
        std::cerr << error.file << ':' << error.line << ':' << error.column
                  << ": runtime error: resource cleanup failed: " << error.message << '\n';
    }
    errors.clear();
    return true;
}

[[noreturn]] inline void fail(const std::string& file, int line, int column, const char* message) {
    throw Failure(file, line, column, message);
}
[[noreturn]] inline void fail(const std::string& file, int line, int column, const std::string& message) {
    throw Failure(file, line, column, message);
}

inline Duration duration_milliseconds(std::int64_t value) noexcept { return Duration{value}; }
inline Duration duration_seconds(std::int64_t value, const std::string& file, int line, int column) {
    constexpr auto minimum = std::numeric_limits<std::int64_t>::min();
    constexpr auto maximum = std::numeric_limits<std::int64_t>::max();
    if (value > maximum / 1000 || value < minimum / 1000)
        fail(file, line, column, "Duration overflow: value is outside signed 64-bit milliseconds");
    return Duration{value * 1000};
}
inline Duration duration_add(Duration left, Duration right, const std::string& file, int line, int column) {
    const auto minimum = std::numeric_limits<std::int64_t>::min();
    const auto maximum = std::numeric_limits<std::int64_t>::max();
    if ((right.milliseconds > 0 && left.milliseconds > maximum - right.milliseconds)
        || (right.milliseconds < 0 && left.milliseconds < minimum - right.milliseconds))
        fail(file, line, column, "Duration overflow: result is outside signed 64-bit milliseconds");
    return Duration{left.milliseconds + right.milliseconds};
}
inline Duration duration_sub(Duration left, Duration right, const std::string& file, int line, int column) {
    const auto minimum = std::numeric_limits<std::int64_t>::min();
    const auto maximum = std::numeric_limits<std::int64_t>::max();
    if ((right.milliseconds < 0 && left.milliseconds > maximum + right.milliseconds)
        || (right.milliseconds > 0 && left.milliseconds < minimum + right.milliseconds))
        fail(file, line, column, "Duration overflow: result is outside signed 64-bit milliseconds");
    return Duration{left.milliseconds - right.milliseconds};
}
inline std::int64_t checked_add(std::int64_t left, std::int64_t right, const std::string& file, int line, int column) {
    const auto minimum = std::numeric_limits<std::int64_t>::min();
    const auto maximum = std::numeric_limits<std::int64_t>::max();
    if ((right > 0 && left > maximum - right) || (right < 0 && left < minimum - right))
        fail(file, line, column, "Int overflow: result is outside the signed 64-bit range");
    return left + right;
}
inline std::int64_t checked_sub(std::int64_t left, std::int64_t right, const std::string& file, int line, int column) {
    const auto minimum = std::numeric_limits<std::int64_t>::min();
    const auto maximum = std::numeric_limits<std::int64_t>::max();
    if ((right < 0 && left > maximum + right) || (right > 0 && left < minimum + right))
        fail(file, line, column, "Int overflow: result is outside the signed 64-bit range");
    return left - right;
}
inline std::int64_t checked_mul(std::int64_t left, std::int64_t right, const std::string& file, int line, int column) {
    const auto minimum = std::numeric_limits<std::int64_t>::min();
    const auto maximum = std::numeric_limits<std::int64_t>::max();
    bool overflow = false;
    if (left != 0 && right != 0) {
        if (left == -1) overflow = right == minimum;
        else if (right == -1) overflow = left == minimum;
        else if (left > 0) overflow = right > 0 ? left > maximum / right : right < minimum / left;
        else overflow = right > 0 ? left < minimum / right : left < maximum / right;
    }
    if (overflow) fail(file, line, column, "Int overflow: result is outside the signed 64-bit range");
    return left * right;
}
inline std::int64_t checked_neg(std::int64_t value, const std::string& file, int line, int column) {
    if (value == std::numeric_limits<std::int64_t>::min())
        fail(file, line, column, "Int overflow: result is outside the signed 64-bit range");
    return -value;
}
inline std::int64_t checked_remainder(std::int64_t left, std::int64_t right, const std::string& file, int line, int column) {
    if (right == 0) fail(file, line, column, "remainder by zero");
    if (left == std::numeric_limits<std::int64_t>::min() && right == -1) return 0;
    return left % right;
}
inline double checked_float(double value, const std::string& file, int line, int column) {
    if (!std::isfinite(value)) fail(file, line, column, "Float overflow: result is outside the supported range");
    return value;
}
inline double checked_add_float(double left, double right, const std::string& file, int line, int column) {
    return checked_float(left + right, file, line, column);
}
inline double checked_sub_float(double left, double right, const std::string& file, int line, int column) {
    return checked_float(left - right, file, line, column);
}
inline double checked_mul_float(double left, double right, const std::string& file, int line, int column) {
    return checked_float(left * right, file, line, column);
}
inline double checked_div_float(double left, double right, const std::string& file, int line, int column) {
    if (right == 0.0) fail(file, line, column, "division by zero");
    return checked_float(left / right, file, line, column);
}
inline double checked_remainder_float(double left, double right, const std::string& file, int line, int column) {
    if (right == 0.0) fail(file, line, column, "remainder by zero");
    return checked_float(std::fmod(left, right), file, line, column);
}

template <typename T>
struct ListData { std::vector<T> values; };

template <typename K, typename V>
struct MapData { std::vector<std::pair<K, V>> entries; };

template <typename T> inline bool equal(const T& left, const T& right);
template <typename T> inline bool equal(const std::optional<T>& left, const std::optional<T>& right);
template <typename T, typename E> inline bool equal(const std::variant<T, E>& left, const std::variant<T, E>& right);
template <typename T> inline bool equal(const std::shared_ptr<ListData<T>>& left, const std::shared_ptr<ListData<T>>& right);
template <typename K, typename V> inline bool equal(const std::shared_ptr<MapData<K, V>>& left, const std::shared_ptr<MapData<K, V>>& right);

template <typename T>
inline bool equal(const T& left, const T& right) { return left == right; }

template <typename T>
inline bool equal(const std::optional<T>& left, const std::optional<T>& right) {
    if (left.has_value() != right.has_value()) return false;
    return !left.has_value() || equal(left.value(), right.value());
}

template <typename T, typename E>
inline bool equal(const std::variant<T, E>& left, const std::variant<T, E>& right) {
    if (left.index() != right.index()) return false;
    if (left.index() == 0) return equal(std::get<0>(left), std::get<0>(right));
    return equal(std::get<1>(left), std::get<1>(right));
}

template <typename T>
inline bool equal(const std::shared_ptr<ListData<T>>& left, const std::shared_ptr<ListData<T>>& right) {
    if (left == right) return true;
    if (!left || !right || left->values.size() != right->values.size()) return false;
    for (std::size_t index = 0; index < left->values.size(); ++index)
        if (!equal(left->values[index], right->values[index])) return false;
    return true;
}

template <typename K, typename V>
inline bool equal(const std::shared_ptr<MapData<K, V>>& left, const std::shared_ptr<MapData<K, V>>& right) {
    if (left == right) return true;
    if (!left || !right || left->entries.size() != right->entries.size()) return false;
    for (const auto& entry : left->entries) {
        bool found = false;
        for (const auto& candidate : right->entries) {
            if (equal(entry.first, candidate.first)) {
                if (!equal(entry.second, candidate.second)) return false;
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    return true;
}

template <typename T>
inline std::shared_ptr<ListData<T>> make_list() { return std::make_shared<ListData<T>>(); }

template <typename K, typename V>
inline std::shared_ptr<MapData<K, V>> make_map() { return std::make_shared<MapData<K, V>>(); }

template <typename T>
inline std::vector<T> list_snapshot(const std::shared_ptr<ListData<T>>& list) { return list->values; }

template <typename K, typename V>
inline std::vector<K> map_keys_snapshot(const std::shared_ptr<MapData<K, V>>& map) {
    std::vector<K> keys;
    keys.reserve(map->entries.size());
    for (const auto& entry : map->entries) keys.push_back(entry.first);
    return keys;
}

template <typename T>
inline void list_append(const std::shared_ptr<ListData<T>>& list, const T& value) {
    list->values.push_back(value);
}

template <typename T>
inline void list_insert(const std::shared_ptr<ListData<T>>& list, std::int64_t index, const T& value,
                        const std::string& file, int line, int column) {
    if (index < 0 || static_cast<std::uint64_t>(index) > list->values.size())
        throw Failure(file, line, column, "List index " + std::to_string(index) + " is out of range");
    list->values.insert(list->values.begin() + static_cast<std::ptrdiff_t>(index), value);
}

template <typename T>
inline std::optional<T> list_get(const std::shared_ptr<ListData<T>>& list, std::int64_t index) {
    if (index < 0 || static_cast<std::uint64_t>(index) >= list->values.size()) return std::nullopt;
    return std::optional<T>{list->values[static_cast<std::size_t>(index)]};
}

template <typename T>
inline void list_set(const std::shared_ptr<ListData<T>>& list, std::int64_t index, const T& value,
                     const std::string& file, int line, int column) {
    if (index < 0 || static_cast<std::uint64_t>(index) >= list->values.size())
        throw Failure(file, line, column, "List index " + std::to_string(index) + " is out of range");
    list->values[static_cast<std::size_t>(index)] = value;
}

template <typename T>
inline std::optional<T> list_remove_at(const std::shared_ptr<ListData<T>>& list, std::int64_t index) {
    if (index < 0 || static_cast<std::uint64_t>(index) >= list->values.size()) return std::nullopt;
    const auto position = static_cast<std::size_t>(index);
    T value = list->values[position];
    list->values.erase(list->values.begin() + static_cast<std::ptrdiff_t>(position));
    return std::optional<T>{std::move(value)};
}

template <typename T>
inline bool list_contains(const std::shared_ptr<ListData<T>>& list, const T& value) {
    for (const auto& item : list->values) if (equal(item, value)) return true;
    return false;
}

template <typename T>
inline void list_clear(const std::shared_ptr<ListData<T>>& list) { list->values.clear(); }

template <typename K, typename V>
inline void map_put(const std::shared_ptr<MapData<K, V>>& map, const K& key, const V& value) {
    for (auto& entry : map->entries) {
        if (equal(entry.first, key)) { entry.second = value; return; }
    }
    map->entries.emplace_back(key, value);
}

template <typename K, typename V>
inline std::optional<V> map_get(const std::shared_ptr<MapData<K, V>>& map, const K& key) {
    for (const auto& entry : map->entries) if (equal(entry.first, key)) return std::optional<V>{entry.second};
    return std::nullopt;
}

template <typename K, typename V>
inline std::optional<V> map_remove(const std::shared_ptr<MapData<K, V>>& map, const K& key) {
    for (auto iterator = map->entries.begin(); iterator != map->entries.end(); ++iterator) {
        if (equal(iterator->first, key)) {
            V value = iterator->second;
            map->entries.erase(iterator);
            return std::optional<V>{std::move(value)};
        }
    }
    return std::nullopt;
}

template <typename K, typename V>
inline bool map_contains_key(const std::shared_ptr<MapData<K, V>>& map, const K& key) {
    for (const auto& entry : map->entries) if (equal(entry.first, key)) return true;
    return false;
}

template <typename K, typename V>
inline std::shared_ptr<ListData<K>> map_keys(const std::shared_ptr<MapData<K, V>>& map) {
    auto result = make_list<K>();
    result->values = map_keys_snapshot(map);
    return result;
}

template <typename K, typename V>
inline std::shared_ptr<ListData<V>> map_values(const std::shared_ptr<MapData<K, V>>& map) {
    auto result = make_list<V>();
    result->values.reserve(map->entries.size());
    for (const auto& entry : map->entries) result->values.push_back(entry.second);
    return result;
}

template <typename K, typename V>
inline void map_clear(const std::shared_ptr<MapData<K, V>>& map) { map->entries.clear(); }

inline std::variant<std::uint8_t, std::string> byte_from_int(
    std::int64_t value, const std::string&, int, int) {
    if (value < 0 || value > 255)
        return std::variant<std::uint8_t, std::string>{std::in_place_index<1>, "Byte value must be between 0 and 255"};
    return std::variant<std::uint8_t, std::string>{std::in_place_index<0>, static_cast<std::uint8_t>(value)};
}

inline std::vector<std::uint8_t> bytes_from_string(const std::string& value) {
    std::vector<std::uint8_t> bytes;
    bytes.reserve(value.size());
    for (unsigned char byte : value) bytes.push_back(static_cast<std::uint8_t>(byte));
    return bytes;
}

inline std::variant<std::uint8_t, std::string> bytes_get(const std::vector<std::uint8_t>& bytes, std::int64_t index) {
    if (index < 0 || static_cast<std::uint64_t>(index) >= bytes.size())
        return std::variant<std::uint8_t, std::string>{std::in_place_index<1>, "Bytes index is out of bounds"};
    return std::variant<std::uint8_t, std::string>{std::in_place_index<0>, bytes[static_cast<std::size_t>(index)]};
}

inline bool valid_utf8(const std::vector<std::uint8_t>& bytes) {
    std::size_t index = 0;
    while (index < bytes.size()) {
        const auto lead = bytes[index];
        if (lead <= 0x7f) { ++index; continue; }
        std::size_t count = 0;
        std::uint32_t value = 0;
        std::uint32_t minimum = 0;
        if (lead >= 0xc2 && lead <= 0xdf) { count = 2; value = lead & 0x1f; minimum = 0x80; }
        else if (lead >= 0xe0 && lead <= 0xef) { count = 3; value = lead & 0x0f; minimum = 0x800; }
        else if (lead >= 0xf0 && lead <= 0xf4) { count = 4; value = lead & 0x07; minimum = 0x10000; }
        else return false;
        if (bytes.size() - index < count) return false;
        for (std::size_t offset = 1; offset < count; ++offset) {
            const auto continuation = bytes[index + offset];
            if ((continuation & 0xc0) != 0x80) return false;
            value = (value << 6) | (continuation & 0x3f);
        }
        if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
        index += count;
    }
    return true;
}

inline std::variant<std::string, std::string> bytes_to_string(const std::vector<std::uint8_t>& bytes) {
    if (!valid_utf8(bytes))
        return std::variant<std::string, std::string>{std::in_place_index<1>, "bytes are not valid UTF-8"};
    std::string value;
    if (!bytes.empty())
        value.assign(reinterpret_cast<const char*>(bytes.data()), bytes.size());
    return std::variant<std::string, std::string>{std::in_place_index<0>, std::move(value)};
}

inline std::variant<std::vector<std::uint8_t>, std::string> bytes_slice(
    const std::vector<std::uint8_t>& bytes, std::int64_t start, std::int64_t length) {
    if (start < 0 || length < 0 || static_cast<std::uint64_t>(start) > bytes.size()
        || static_cast<std::uint64_t>(length) > bytes.size() - static_cast<std::size_t>(start))
        return std::variant<std::vector<std::uint8_t>, std::string>{std::in_place_index<1>, "Bytes slice is out of bounds"};
    const auto offset = static_cast<std::size_t>(start);
    const auto count = static_cast<std::size_t>(length);
    try {
        return std::variant<std::vector<std::uint8_t>, std::string>{
            std::in_place_index<0>, bytes.begin() + static_cast<std::ptrdiff_t>(offset),
            bytes.begin() + static_cast<std::ptrdiff_t>(offset + count)};
    } catch (const std::bad_alloc&) {
        return std::variant<std::vector<std::uint8_t>, std::string>{std::in_place_index<1>, "cannot allocate Bytes slice"};
    }
}

constexpr std::uint64_t MAX_BYTE_BUFFER_LENGTH = 67108864;
template <typename T> using StringResult = std::variant<T, std::string>;
template <typename T> inline StringResult<T> result_ok(T value) {
    return StringResult<T>{std::in_place_index<0>, std::move(value)};
}
template <typename T> inline StringResult<T> result_error(std::string message) {
    return StringResult<T>{std::in_place_index<1>, std::move(message)};
}
inline StringResult<Duration> duration_from_seconds(double seconds) {
    if (!std::isfinite(seconds) || seconds < 0.0)
        return result_error<Duration>("Duration seconds must be finite and non-negative");
    const double milliseconds = seconds * 1000.0;
    if (!std::isfinite(milliseconds)
        || milliseconds >= static_cast<double>(std::numeric_limits<std::int64_t>::max()))
        return result_error<Duration>("Duration seconds exceed the supported range");
    return result_ok<Duration>(Duration{static_cast<std::int64_t>(std::ceil(milliseconds))});
}

struct SocketAddress {
    std::string host;
    std::int64_t port = 0;
    std::string family;
};

#if defined(__linux__)
inline std::string network_error_from_errno(int code) {
    if (code == ECONNREFUSED) return "connection refused";
    if (code == ECONNRESET || code == EPIPE || code == ENOTCONN) return "connection reset";
#ifdef ESHUTDOWN
    if (code == ESHUTDOWN) return "connection reset";
#endif
    if (code == EADDRINUSE) return "address already in use";
    if (code == EACCES || code == EPERM) return "permission denied";
    if (code == ETIMEDOUT) return "operation timed out";
    if (code == EINVAL) return "invalid address";
    return "network operation failed";
}
#endif

struct SocketHandle {
#if defined(__linux__)
    std::atomic<int> descriptor{-1};
    mutable std::mutex mutex;
    explicit SocketHandle(int value) : descriptor(value) {}
    int duplicate() const {
        std::lock_guard<std::mutex> lock(mutex);
        const int value = descriptor.load(std::memory_order_acquire);
        if (value < 0) return -1;
        const int copy = ::dup(value);
        if (copy < 0) return -1;
        if (::fcntl(copy, F_SETFD, FD_CLOEXEC) < 0) {
            ::close(copy);
            return -1;
        }
        return copy;
    }
    bool is_open() const noexcept { return descriptor.load(std::memory_order_acquire) >= 0; }
    bool close() noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        const int value = descriptor.exchange(-1, std::memory_order_acq_rel);
        if (value < 0) return false;
        (void)::shutdown(value, SHUT_RDWR);
        return ::close(value) == 0;
    }
#else
    explicit SocketHandle(int) {}
    int duplicate() const noexcept { return -1; }
    bool is_open() const noexcept { return false; }
    bool close() noexcept { return false; }
#endif
    ~SocketHandle() { (void)close(); }
};

struct TcpConnection {
    std::shared_ptr<SocketHandle> socket;
    SocketAddress local;
    SocketAddress remote;
    std::atomic<bool> pending_read{false};
    std::atomic<bool> pending_write{false};
    std::string source_file;
    int source_line = 1;
    int source_column = 1;
    ~TcpConnection() noexcept {
        if (socket && socket->is_open() && !socket->close())
            record_cleanup_failure(source_file, source_line, source_column, "cannot close TCP connection");
    }
};
struct TcpListener {
    std::shared_ptr<SocketHandle> socket;
    SocketAddress local;
    std::atomic<bool> pending_accept{false};
    std::string source_file;
    int source_line = 1;
    int source_column = 1;
    ~TcpListener() noexcept {
        if (socket && socket->is_open() && !socket->close())
            record_cleanup_failure(source_file, source_line, source_column, "cannot close TCP listener");
    }
};
#if defined(__linux__) && defined(TYVERA_NATIVE_TLS)
struct TlsSession {
    SSL_CTX* context = nullptr;
    SSL* ssl = nullptr;
    int descriptor = -1;
    bool closed = false;
    std::mutex mutex;
    ~TlsSession() noexcept { close(); }
    void close() noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        if (closed) return;
        closed = true;
        if (ssl) {
            (void)SSL_shutdown(ssl);
            SSL_free(ssl);
            ssl = nullptr;
        }
        if (context) {
            SSL_CTX_free(context);
            context = nullptr;
        }
        if (descriptor >= 0) {
            (void)::close(descriptor);
            descriptor = -1;
        }
    }
};
struct TlsConnection {
    std::shared_ptr<SocketHandle> socket;
    std::shared_ptr<TlsSession> session;
    SocketAddress local;
    SocketAddress remote;
    std::atomic<bool> pending_read{false};
    std::atomic<bool> pending_write{false};
    std::string source_file;
    int source_line = 1;
    int source_column = 1;
    bool close() noexcept {
        if (session) session->close();
        return socket && socket->close();
    }
    bool is_open() const noexcept { return socket && socket->is_open(); }
    ~TlsConnection() noexcept {
        if (socket && socket->is_open() && !close())
            record_cleanup_failure(source_file, source_line, source_column, "cannot close TLS connection");
        else if (session)
            session->close();
    }
};
#endif

inline void set_resource_location(const std::shared_ptr<TcpConnection>& resource,
                                  const std::string& file, int line, int column) {
    if (!resource) return;
    resource->source_file = file;
    resource->source_line = line;
    resource->source_column = column;
}
#if defined(__linux__) && defined(TYVERA_NATIVE_TLS)
inline void set_resource_location(const std::shared_ptr<TlsConnection>& resource,
                                  const std::string& file, int line, int column) {
    if (!resource) return;
    resource->source_file = file;
    resource->source_line = line;
    resource->source_column = column;
}
#endif
inline void set_resource_location(const std::shared_ptr<TcpListener>& resource,
                                  const std::string& file, int line, int column) {
    if (!resource) return;
    resource->source_file = file;
    resource->source_line = line;
    resource->source_column = column;
}

struct PendingSocketOperation {
    std::atomic<bool>* flag;
    std::shared_ptr<void> lifetime;
    bool reserved = false;
    template <typename T>
    PendingSocketOperation(std::atomic<bool>& value, std::shared_ptr<T> owner)
        : flag(&value), lifetime(std::move(owner)) {}
    PendingSocketOperation(const PendingSocketOperation&) = delete;
    PendingSocketOperation& operator=(const PendingSocketOperation&) = delete;
    bool acquire() noexcept {
        bool expected = false;
        reserved = flag->compare_exchange_strong(expected, true, std::memory_order_acq_rel);
        return reserved;
    }
    void release() noexcept {
        if (reserved) {
            flag->store(false, std::memory_order_release);
            reserved = false;
        }
    }
    ~PendingSocketOperation() { release(); }
};
struct PendingSocketOperationReset {
    std::shared_ptr<PendingSocketOperation> operation;
    ~PendingSocketOperationReset() { if (operation) operation->release(); }
};

struct UniqueSocketFd {
    int value = -1;
    explicit UniqueSocketFd(int descriptor = -1) : value(descriptor) {}
    UniqueSocketFd(const UniqueSocketFd&) = delete;
    UniqueSocketFd& operator=(const UniqueSocketFd&) = delete;
    UniqueSocketFd(UniqueSocketFd&& other) noexcept : value(other.value) { other.value = -1; }
    ~UniqueSocketFd() {
#if defined(__linux__)
        if (value >= 0) (void)::close(value);
#endif
    }
    int release() noexcept { const int result = value; value = -1; return result; }
};

#if defined(__linux__)
inline bool configure_socket_descriptor(int descriptor) {
    const int descriptor_flags = ::fcntl(descriptor, F_GETFD, 0);
    if (descriptor_flags < 0 || ::fcntl(descriptor, F_SETFD, descriptor_flags | FD_CLOEXEC) < 0)
        return false;
    const int status_flags = ::fcntl(descriptor, F_GETFL, 0);
    if (status_flags < 0 || ::fcntl(descriptor, F_SETFL, status_flags | O_NONBLOCK) < 0)
        return false;
    return true;
}

inline bool socket_address_from(const sockaddr* raw, socklen_t size, SocketAddress& output) {
    if (!raw || (raw->sa_family != AF_INET && raw->sa_family != AF_INET6)) return false;
    char host[NI_MAXHOST]{};
    if (::getnameinfo(raw, size, host, sizeof(host), nullptr, 0, NI_NUMERICHOST) != 0) return false;
    output.host = host;
    if (raw->sa_family == AF_INET) {
        output.family = "IPv4";
        output.port = ntohs(reinterpret_cast<const sockaddr_in*>(raw)->sin_port);
    } else {
        output.family = "IPv6";
        output.port = ntohs(reinterpret_cast<const sockaddr_in6*>(raw)->sin6_port);
    }
    return true;
}

inline StringResult<SocketAddress> socket_address_from_fd(
    const std::shared_ptr<SocketHandle>& handle, bool peer) {
    if (!handle) return result_error<SocketAddress>("resource is closed");
    UniqueSocketFd duplicate(handle->duplicate());
    if (duplicate.value < 0) return result_error<SocketAddress>("resource is closed");
    sockaddr_storage raw{};
    socklen_t size = sizeof(raw);
    const int status = peer
        ? ::getpeername(duplicate.value, reinterpret_cast<sockaddr*>(&raw), &size)
        : ::getsockname(duplicate.value, reinterpret_cast<sockaddr*>(&raw), &size);
    if (status != 0) return result_error<SocketAddress>(network_error_from_errno(errno));
    SocketAddress result;
    if (!socket_address_from(reinterpret_cast<const sockaddr*>(&raw), size, result))
        return result_error<SocketAddress>("network operation failed");
    return result_ok<SocketAddress>(std::move(result));
}

inline int poll_socket(const std::shared_ptr<SocketHandle>& handle, int descriptor, short events,
                       const std::string& file, int line, int column) {
    for (;;) {
        task_checkpoint();
        check_shutdown(file, line, column);
        if (!handle || !handle->is_open()) return -2;
        pollfd item{descriptor, events, 0};
        const int ready = ::poll(&item, 1, 100);
        if (ready > 0) return item.revents & POLLNVAL ? -1 : ready;
        if (ready == 0 || errno == EINTR) continue;
        return -1;
    }
}
inline int poll_descriptor(int descriptor, short events,
                           const std::string& file, int line, int column) {
    for (;;) {
        task_checkpoint();
        check_shutdown(file, line, column);
        pollfd item{descriptor, events, 0};
        const int ready = ::poll(&item, 1, 100);
        if (ready > 0) return item.revents & POLLNVAL ? -1 : ready;
        if (ready == 0 || errno == EINTR) continue;
        return -1;
    }
}

#endif

inline Task<StringResult<std::shared_ptr<ListData<SocketAddress>>>> resolve_host(
    std::string host, std::int64_t port, const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<ListData<SocketAddress>>>;
    return make_io_task<Output>([host = std::move(host), port]() -> Output {
        task_checkpoint();
        if (host.find_first_not_of(" \t\r\n") == std::string::npos)
            return result_error<std::shared_ptr<ListData<SocketAddress>>>("invalid address");
        if (port < 0 || port > 65535)
            return result_error<std::shared_ptr<ListData<SocketAddress>>>("port must be between 0 and 65535");
#if defined(__linux__)
        int status = 0;
        addrinfo* addresses = nullptr;
        addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;
        const auto service = std::to_string(port);
        status = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses);
        if (status != 0) return result_error<std::shared_ptr<ListData<SocketAddress>>>("DNS resolution failed");
        std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> owner(addresses, ::freeaddrinfo);
        task_checkpoint();
        auto result = make_list<SocketAddress>();
        try {
            for (auto* current = addresses; current; current = current->ai_next) {
                SocketAddress item;
                if (!socket_address_from(current->ai_addr, static_cast<socklen_t>(current->ai_addrlen), item)) continue;
                const auto duplicate = std::find_if(result->values.begin(), result->values.end(), [&](const auto& value) {
                    return value.host == item.host && value.port == item.port && value.family == item.family;
                });
                if (duplicate == result->values.end()) result->values.push_back(std::move(item));
            }
        } catch (...) { return result_error<std::shared_ptr<ListData<SocketAddress>>>("DNS resolution failed"); }
        if (result->values.empty()) return result_error<std::shared_ptr<ListData<SocketAddress>>>("DNS resolution failed");
        return result_ok<std::shared_ptr<ListData<SocketAddress>>>(std::move(result));
#else
        return result_error<std::shared_ptr<ListData<SocketAddress>>>("networking is not supported by this native platform");
#endif
    }, file, line, column);
}

inline Task<StringResult<std::shared_ptr<TcpConnection>>> tcp_connect(
    std::string host, std::int64_t port, const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<TcpConnection>>;
    return make_io_task<Output>([host = std::move(host), port, file, line, column]() -> Output {
        task_checkpoint();
        if (host.find_first_not_of(" \t\r\n") == std::string::npos)
            return result_error<std::shared_ptr<TcpConnection>>("invalid address");
        if (port < 1 || port > 65535)
            return result_error<std::shared_ptr<TcpConnection>>("port must be between 1 and 65535");
#if defined(__linux__)
        addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;
        const auto service = std::to_string(port);
        addrinfo* addresses = nullptr;
        const int resolved = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses);
        if (resolved != 0) return result_error<std::shared_ptr<TcpConnection>>("DNS resolution failed");
        std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> owner(addresses, ::freeaddrinfo);
        task_checkpoint();
        int last_error = 0;
        for (auto* current = addresses; current; current = current->ai_next) {
            const int descriptor = ::socket(current->ai_family, current->ai_socktype, current->ai_protocol);
            if (descriptor < 0) { last_error = errno; continue; }
            UniqueSocketFd socket_fd(descriptor);
            if (!configure_socket_descriptor(descriptor)) {
                last_error = errno;
                continue;
            }
            if (::connect(descriptor, current->ai_addr, static_cast<socklen_t>(current->ai_addrlen)) != 0) {
                last_error = errno;
                if (last_error != EINPROGRESS && last_error != EALREADY &&
                    last_error != EWOULDBLOCK && last_error != EINTR) continue;
                if (poll_descriptor(descriptor, POLLOUT, file, line, column) < 0) {
                    last_error = errno ? errno : EIO;
                    continue;
                }
                int connect_error = 0;
                socklen_t connect_error_size = sizeof(connect_error);
                if (::getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &connect_error, &connect_error_size) != 0)
                    connect_error = errno ? errno : EIO;
                if (connect_error != 0) { last_error = connect_error; continue; }
            }
            SocketAddress local;
            sockaddr_storage local_raw{};
            socklen_t local_size = sizeof(local_raw);
            if (::getsockname(descriptor, reinterpret_cast<sockaddr*>(&local_raw), &local_size) != 0 ||
                !socket_address_from(reinterpret_cast<const sockaddr*>(&local_raw), local_size, local)) {
                last_error = errno ? errno : EIO;
                continue;
            }
            SocketAddress remote;
            if (!socket_address_from(current->ai_addr, static_cast<socklen_t>(current->ai_addrlen), remote)) {
                last_error = EIO;
                continue;
            }
            auto handle = std::make_shared<SocketHandle>(descriptor);
            socket_fd.release();
            auto result = std::make_shared<TcpConnection>();
            result->socket = std::move(handle);
            set_resource_location(result, file, line, column);
            result->local = std::move(local);
            result->remote = std::move(remote);
            return result_ok<std::shared_ptr<TcpConnection>>(std::move(result));
        }
        return result_error<std::shared_ptr<TcpConnection>>(last_error ? network_error_from_errno(last_error) : "connection failed");
#else
        return result_error<std::shared_ptr<TcpConnection>>("networking is not supported by this native platform");
#endif
    }, file, line, column);
}

#if defined(__linux__) && defined(TYVERA_NATIVE_TLS)
inline Task<StringResult<std::shared_ptr<TlsConnection>>> tls_connect(
    std::string host, std::int64_t port, const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<TlsConnection>>;
    return make_io_task<Output>([host = std::move(host), port, file, line, column]() -> Output {
        try {
            task_checkpoint();
            auto connected = tcp_connect(host, port, file, line, column).await(file, line, column);
            if (std::holds_alternative<std::string>(connected))
                return result_error<std::shared_ptr<TlsConnection>>(std::get<1>(std::move(connected)));
            auto tcp = std::move(std::get<0>(connected));
            if (!tcp || !tcp->socket || !tcp->socket->is_open())
                return result_error<std::shared_ptr<TlsConnection>>("resource closed");

            std::unique_ptr<SSL_CTX, decltype(&::SSL_CTX_free)> context(
                SSL_CTX_new(TLS_client_method()), ::SSL_CTX_free);
            if (!context || SSL_CTX_set_default_verify_paths(context.get()) != 1 ||
                SSL_CTX_set_min_proto_version(context.get(), TLS1_2_VERSION) != 1)
                return result_error<std::shared_ptr<TlsConnection>>("TLS connection setup failed");
            SSL_CTX_set_verify(context.get(), SSL_VERIFY_PEER, nullptr);
            std::unique_ptr<SSL, decltype(&::SSL_free)> ssl(SSL_new(context.get()), ::SSL_free);
            if (!ssl) return result_error<std::shared_ptr<TlsConnection>>("TLS connection setup failed");

            UniqueSocketFd tls_descriptor(tcp->socket->duplicate());
            if (tls_descriptor.value < 0)
                return result_error<std::shared_ptr<TlsConnection>>("resource closed");
            BIO* bio = BIO_new_socket(tls_descriptor.value, BIO_NOCLOSE);
            if (!bio) return result_error<std::shared_ptr<TlsConnection>>("TLS connection setup failed");
            SSL_set_bio(ssl.get(), bio, bio);

            std::string verification_host = host;
            const auto scope = verification_host.find('%');
            if (scope != std::string::npos) verification_host.resize(scope);
            in_addr ipv4{};
            in6_addr ipv6{};
            const bool is_ip =
                ::inet_pton(AF_INET, verification_host.c_str(), &ipv4) == 1 ||
                ::inet_pton(AF_INET6, verification_host.c_str(), &ipv6) == 1;
            auto* verification = SSL_get0_param(ssl.get());
            if (is_ip) {
                if (X509_VERIFY_PARAM_set1_ip_asc(verification, verification_host.c_str()) != 1)
                    return result_error<std::shared_ptr<TlsConnection>>("TLS connection setup failed");
            } else {
                if (!std::all_of(host.begin(), host.end(), [](unsigned char value) { return value < 0x80; }))
                    return result_error<std::shared_ptr<TlsConnection>>("TLS host name must be ASCII");
                if (SSL_set_tlsext_host_name(ssl.get(), host.c_str()) != 1 ||
                    X509_VERIFY_PARAM_set1_host(verification, host.c_str(), 0) != 1)
                    return result_error<std::shared_ptr<TlsConnection>>("TLS connection setup failed");
            }

            for (;;) {
                task_checkpoint();
                errno = 0;
                const int status = SSL_connect(ssl.get());
                if (status == 1) break;
                const int ssl_error = SSL_get_error(ssl.get(), status);
                const int system_error = errno;
                if (ssl_error == SSL_ERROR_WANT_READ || ssl_error == SSL_ERROR_WANT_WRITE) {
                    const short event = ssl_error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT;
                    if (poll_descriptor(tls_descriptor.value, event, file, line, column) >= 0) continue;
                    if (system_error != 0)
                        return result_error<std::shared_ptr<TlsConnection>>(network_error_from_errno(system_error));
                    return result_error<std::shared_ptr<TlsConnection>>("TLS handshake failed");
                }
                if (SSL_get_verify_result(ssl.get()) != X509_V_OK)
                    return result_error<std::shared_ptr<TlsConnection>>("TLS certificate verification failed");
                return result_error<std::shared_ptr<TlsConnection>>("TLS handshake failed");
            }

            auto session = std::make_shared<TlsSession>();
            session->context = context.release();
            session->ssl = ssl.release();
            session->descriptor = tls_descriptor.release();
            auto result = std::make_shared<TlsConnection>();
            result->socket = std::move(tcp->socket);
            result->session = std::move(session);
            result->local = std::move(tcp->local);
            result->remote = std::move(tcp->remote);
            result->source_file = file;
            result->source_line = line;
            result->source_column = column;
            tcp->socket.reset();
            return result_ok<std::shared_ptr<TlsConnection>>(std::move(result));
        } catch (const TaskCancelled&) {
            throw;
        } catch (const Failure&) {
            throw;
        } catch (...) {
            return result_error<std::shared_ptr<TlsConnection>>("TLS connection setup failed");
        }
    }, file, line, column);
}

#endif

inline Task<StringResult<std::shared_ptr<TcpListener>>> tcp_listen(
    std::string host, std::int64_t port, std::int64_t backlog,
    const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<TcpListener>>;
    return make_io_task<Output>([host = std::move(host), port, backlog, file, line, column]() -> Output {
        task_checkpoint();
        if (host.find_first_not_of(" \t\r\n") == std::string::npos)
            return result_error<std::shared_ptr<TcpListener>>("invalid address");
        if (port < 0 || port > 65535)
            return result_error<std::shared_ptr<TcpListener>>("port must be between 0 and 65535");
        if (backlog < 1 || backlog > 4096)
            return result_error<std::shared_ptr<TcpListener>>("backlog must be between 1 and 4096");
#if defined(__linux__)
        addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;
        hints.ai_flags = AI_PASSIVE;
        const auto service = std::to_string(port);
        addrinfo* addresses = nullptr;
        const int resolved = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses);
        if (resolved != 0) return result_error<std::shared_ptr<TcpListener>>("DNS resolution failed");
        std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> owner(addresses, ::freeaddrinfo);
        task_checkpoint();
        int last_error = 0;
        for (auto* current = addresses; current; current = current->ai_next) {
            const int descriptor = ::socket(current->ai_family, current->ai_socktype, current->ai_protocol);
            if (descriptor < 0) { last_error = errno; continue; }
            UniqueSocketFd socket_fd(descriptor);
            if (!configure_socket_descriptor(descriptor)) {
                last_error = errno;
                continue;
            }
            int reuse = 1;
            if (::setsockopt(descriptor, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) != 0) {
                last_error = errno;
                continue;
            }
            if (current->ai_family == AF_INET6) {
                int v6_only = 1;
                if (::setsockopt(descriptor, IPPROTO_IPV6, IPV6_V6ONLY, &v6_only, sizeof(v6_only)) != 0) {
                    last_error = errno;
                    continue;
                }
            }
            if (::bind(descriptor, current->ai_addr, static_cast<socklen_t>(current->ai_addrlen)) != 0 ||
                ::listen(descriptor, static_cast<int>(backlog)) != 0) {
                last_error = errno;
                continue;
            }
            sockaddr_storage local_raw{};
            socklen_t local_size = sizeof(local_raw);
            SocketAddress local;
            if (::getsockname(descriptor, reinterpret_cast<sockaddr*>(&local_raw), &local_size) != 0 ||
                !socket_address_from(reinterpret_cast<const sockaddr*>(&local_raw), local_size, local)) {
                last_error = errno ? errno : EIO;
                continue;
            }
            auto result = std::make_shared<TcpListener>();
            result->socket = std::make_shared<SocketHandle>(descriptor);
            socket_fd.release();
            set_resource_location(result, file, line, column);
            result->local = std::move(local);
            return result_ok<std::shared_ptr<TcpListener>>(std::move(result));
        }
        return result_error<std::shared_ptr<TcpListener>>(last_error ? network_error_from_errno(last_error) : "network operation failed");
#else
        return result_error<std::shared_ptr<TcpListener>>("networking is not supported by this native platform");
#endif
    }, file, line, column);
}

inline std::string socket_address_host(const SocketAddress& value) { return value.host; }
inline std::int64_t socket_address_port(const SocketAddress& value) { return value.port; }
inline std::string socket_address_family(const SocketAddress& value) { return value.family; }
inline std::string socket_address_to_string(const SocketAddress& value) {
    return (value.family == "IPv6" ? "[" + value.host + "]" : value.host) + ":" + std::to_string(value.port);
}
inline StringResult<SocketAddress> tcp_local_address(const std::shared_ptr<TcpConnection>& value,
    const std::string& file, int line, int column) {
    if (!value || !value->socket || !value->socket->is_open()) fail(file, line, column, "TcpConnection is closed");
#if defined(__linux__)
    return socket_address_from_fd(value->socket, false);
#else
    return result_error<SocketAddress>("networking is not supported by this native platform");
#endif
}
inline StringResult<SocketAddress> tcp_remote_address(const std::shared_ptr<TcpConnection>& value,
    const std::string& file, int line, int column) {
    if (!value || !value->socket || !value->socket->is_open()) fail(file, line, column, "TcpConnection is closed");
#if defined(__linux__)
    return socket_address_from_fd(value->socket, true);
#else
    return result_error<SocketAddress>("networking is not supported by this native platform");
#endif
}
inline StringResult<SocketAddress> listener_local_address(const std::shared_ptr<TcpListener>& value,
    const std::string& file, int line, int column) {
    if (!value || !value->socket || !value->socket->is_open()) fail(file, line, column, "TcpListener is closed");
#if defined(__linux__)
    return socket_address_from_fd(value->socket, false);
#else
    return result_error<SocketAddress>("networking is not supported by this native platform");
#endif
}
inline bool tcp_close(const std::shared_ptr<TcpConnection>& value) { return value && value->socket && value->socket->close(); }
inline bool tcp_is_open(const std::shared_ptr<TcpConnection>& value) { return value && value->socket && value->socket->is_open(); }
inline bool listener_close(const std::shared_ptr<TcpListener>& value) { return value && value->socket && value->socket->close(); }
inline bool listener_is_open(const std::shared_ptr<TcpListener>& value) { return value && value->socket && value->socket->is_open(); }

inline Task<StringResult<std::shared_ptr<TcpConnection>>> tcp_accept(
    const std::shared_ptr<TcpListener>& listener, const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<TcpConnection>>;
    if (!listener || !listener->socket || !listener->socket->is_open())
        fail(file, line, column, "TcpListener is closed");
    auto pending = std::make_shared<PendingSocketOperation>(listener->pending_accept, listener);
    if (!pending->acquire()) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::shared_ptr<TcpConnection>>("listener already has a pending accept");
        }, file, line, column);
    }
    return make_io_task<Output>([listener, pending, file, line, column]() -> Output {
        PendingSocketOperationReset pending_reset{pending};
        if (!listener || !listener->socket || !listener->socket->is_open()) return result_error<std::shared_ptr<TcpConnection>>("TcpListener is closed");
#if defined(__linux__)
        UniqueSocketFd listening(listener->socket->duplicate());
        if (listening.value < 0) return result_error<std::shared_ptr<TcpConnection>>("TcpListener is closed");
        for (;;) {
            const int ready = poll_socket(listener->socket, listening.value, POLLIN, file, line, column);
            if (ready == -2) return result_error<std::shared_ptr<TcpConnection>>("resource closed");
            if (ready < 0) return result_error<std::shared_ptr<TcpConnection>>("network operation failed");
            sockaddr_storage remote_raw{};
            socklen_t remote_size = sizeof(remote_raw);
            const int accepted = ::accept(listening.value, reinterpret_cast<sockaddr*>(&remote_raw), &remote_size);
            if (accepted < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                return result_error<std::shared_ptr<TcpConnection>>(network_error_from_errno(errno));
            }
            UniqueSocketFd accepted_fd(accepted);
            if (!configure_socket_descriptor(accepted))
                return result_error<std::shared_ptr<TcpConnection>>(network_error_from_errno(errno));
            sockaddr_storage local_raw{};
            socklen_t local_size = sizeof(local_raw);
            SocketAddress local, remote;
            if (::getsockname(accepted, reinterpret_cast<sockaddr*>(&local_raw), &local_size) != 0 ||
                !socket_address_from(reinterpret_cast<const sockaddr*>(&local_raw), local_size, local) ||
                !socket_address_from(reinterpret_cast<const sockaddr*>(&remote_raw), remote_size, remote))
                return result_error<std::shared_ptr<TcpConnection>>("network operation failed");
            auto result = std::make_shared<TcpConnection>();
            result->socket = std::make_shared<SocketHandle>(accepted);
            accepted_fd.release();
            set_resource_location(result, file, line, column);
            result->local = std::move(local);
            result->remote = std::move(remote);
            return result_ok<std::shared_ptr<TcpConnection>>(std::move(result));
        }
#else
        return result_error<std::shared_ptr<TcpConnection>>("networking is not supported by this native platform");
#endif
    }, file, line, column);
}

inline Task<StringResult<std::optional<std::vector<std::uint8_t>>>> tcp_read(
    const std::shared_ptr<TcpConnection>& connection, std::int64_t max_bytes,
    const std::string& file, int line, int column) {
    using Output = StringResult<std::optional<std::vector<std::uint8_t>>>;
    if (!connection || !connection->socket || !connection->socket->is_open())
        fail(file, line, column, "TcpConnection is closed");
    if (max_bytes < 1 || max_bytes > 1048576) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::optional<std::vector<std::uint8_t>>>("read size must be between 1 and 1048576 bytes");
        }, file, line, column);
    }
    auto pending = std::make_shared<PendingSocketOperation>(connection->pending_read, connection);
    if (!pending->acquire()) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::optional<std::vector<std::uint8_t>>>("connection already has a pending read");
        }, file, line, column);
    }
    return make_io_task<Output>([connection, pending, max_bytes, file, line, column]() -> Output {
        PendingSocketOperationReset pending_reset{pending};
        if (!connection || !connection->socket || !connection->socket->is_open()) return result_error<std::optional<std::vector<std::uint8_t>>>("TcpConnection is closed");
#if defined(__linux__)
        UniqueSocketFd descriptor(connection->socket->duplicate());
        if (descriptor.value < 0) return result_error<std::optional<std::vector<std::uint8_t>>>("TcpConnection is closed");
        try {
            std::vector<std::uint8_t> bytes(static_cast<std::size_t>(max_bytes));
            for (;;) {
                const int ready = poll_socket(connection->socket, descriptor.value, POLLIN, file, line, column);
                if (ready == -2) return result_error<std::optional<std::vector<std::uint8_t>>>("resource closed");
                if (ready < 0) return result_error<std::optional<std::vector<std::uint8_t>>>("network operation failed");
                const auto count = ::recv(descriptor.value, bytes.data(), bytes.size(), 0);
                if (count > 0) { bytes.resize(static_cast<std::size_t>(count)); return result_ok<std::optional<std::vector<std::uint8_t>>>(std::move(bytes)); }
                if (count == 0) return result_ok<std::optional<std::vector<std::uint8_t>>>(std::nullopt);
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                if (!connection->socket->is_open()) return result_error<std::optional<std::vector<std::uint8_t>>>("resource closed");
                return result_error<std::optional<std::vector<std::uint8_t>>>(network_error_from_errno(errno));
            }
        } catch (...) { return result_error<std::optional<std::vector<std::uint8_t>>>("network operation failed"); }
#else
        return result_error<std::optional<std::vector<std::uint8_t>>>("networking is not supported by this native platform");
#endif
    }, file, line, column);
}

inline Task<StringResult<std::int64_t>> tcp_write(
    const std::shared_ptr<TcpConnection>& connection, std::vector<std::uint8_t> bytes, bool write_all,
    const std::string& file, int line, int column) {
    using Output = StringResult<std::int64_t>;
    if (!connection || !connection->socket || !connection->socket->is_open())
        fail(file, line, column, "TcpConnection is closed");
    if (bytes.empty()) {
        return make_io_task<Output>([]() -> Output { return result_ok<std::int64_t>(0); }, file, line, column);
    }
    auto pending = std::make_shared<PendingSocketOperation>(connection->pending_write, connection);
    if (!pending->acquire()) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::int64_t>("connection already has a pending write");
        }, file, line, column);
    }
    return make_io_task<Output>([connection, pending, bytes = std::move(bytes), write_all, file, line, column]() -> Output {
        PendingSocketOperationReset pending_reset{pending};
        if (!connection || !connection->socket || !connection->socket->is_open()) return result_error<std::int64_t>("TcpConnection is closed");
#if defined(__linux__)
        UniqueSocketFd descriptor(connection->socket->duplicate());
        if (descriptor.value < 0) return result_error<std::int64_t>("TcpConnection is closed");
        std::size_t offset = 0;
        while (offset < bytes.size()) {
            const int ready = poll_socket(connection->socket, descriptor.value, POLLOUT, file, line, column);
            if (ready == -2) return result_error<std::int64_t>("resource closed");
            if (ready < 0) return result_error<std::int64_t>("network operation failed");
            const auto count = ::send(descriptor.value, bytes.data() + offset, bytes.size() - offset, MSG_NOSIGNAL);
            if (count < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                if (!connection->socket->is_open()) return result_error<std::int64_t>("resource closed");
                return result_error<std::int64_t>(network_error_from_errno(errno));
            }
            if (count == 0) return result_error<std::int64_t>("connection reset");
            offset += static_cast<std::size_t>(count);
            if (!write_all) break;
        }
        return result_ok<std::int64_t>(static_cast<std::int64_t>(offset));
#else
        return result_error<std::int64_t>("networking is not supported by this native platform");
#endif
    }, file, line, column);
}
#if defined(__linux__) && defined(TYVERA_NATIVE_TLS)
inline StringResult<SocketAddress> tls_local_address(
    const std::shared_ptr<TlsConnection>& connection, const std::string&, int, int) {
    if (!connection || !connection->is_open()) return result_error<SocketAddress>("resource is closed");
    return result_ok<SocketAddress>(connection->local);
}
inline StringResult<SocketAddress> tls_remote_address(
    const std::shared_ptr<TlsConnection>& connection, const std::string&, int, int) {
    if (!connection || !connection->is_open()) return result_error<SocketAddress>("resource is closed");
    return result_ok<SocketAddress>(connection->remote);
}
inline bool tls_close(const std::shared_ptr<TlsConnection>& connection) {
    return connection && connection->close();
}
inline bool tls_is_open(const std::shared_ptr<TlsConnection>& connection) {
    return connection && connection->is_open();
}
inline Task<StringResult<std::optional<std::vector<std::uint8_t>>>> tls_read(
    const std::shared_ptr<TlsConnection>& connection, std::int64_t max_bytes,
    const std::string& file, int line, int column) {
    using Output = StringResult<std::optional<std::vector<std::uint8_t>>>;
    if (!connection || !connection->is_open() || !connection->session)
        fail(file, line, column, "TlsConnection is closed");
    if (max_bytes < 1 || max_bytes > 1048576) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::optional<std::vector<std::uint8_t>>>("read size must be between 1 and 1048576 bytes");
        }, file, line, column);
    }
    auto pending = std::make_shared<PendingSocketOperation>(connection->pending_read, connection);
    if (!pending->acquire()) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::optional<std::vector<std::uint8_t>>>("connection already has a pending read");
        }, file, line, column);
    }
    return make_io_task<Output>([connection, pending, max_bytes, file, line, column]() -> Output {
        PendingSocketOperationReset pending_reset{pending};
        if (!connection || !connection->is_open() || !connection->session)
            return result_error<std::optional<std::vector<std::uint8_t>>>("resource closed");
        UniqueSocketFd readiness_descriptor(connection->socket->duplicate());
        if (readiness_descriptor.value < 0)
            return result_error<std::optional<std::vector<std::uint8_t>>>("resource closed");
        try {
            std::vector<std::uint8_t> bytes(static_cast<std::size_t>(max_bytes));
            for (;;) {
                task_checkpoint();
                int status = 0;
                int ssl_error = SSL_ERROR_NONE;
                int system_error = 0;
                {
                    std::lock_guard<std::mutex> lock(connection->session->mutex);
                    if (connection->session->closed || !connection->session->ssl)
                        return result_error<std::optional<std::vector<std::uint8_t>>>("resource closed");
                    errno = 0;
                    status = SSL_read(connection->session->ssl, bytes.data(), static_cast<int>(bytes.size()));
                    if (status <= 0) {
                        ssl_error = SSL_get_error(connection->session->ssl, status);
                        system_error = errno;
                    }
                }
                if (status > 0) {
                    bytes.resize(static_cast<std::size_t>(status));
                    return result_ok<std::optional<std::vector<std::uint8_t>>>(std::move(bytes));
                }
                if (ssl_error == SSL_ERROR_ZERO_RETURN)
                    return result_ok<std::optional<std::vector<std::uint8_t>>>(std::nullopt);
                if (ssl_error == SSL_ERROR_WANT_READ || ssl_error == SSL_ERROR_WANT_WRITE) {
                    const short event = ssl_error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT;
                    const int ready = poll_socket(connection->socket, readiness_descriptor.value, event, file, line, column);
                    if (ready == -2) return result_error<std::optional<std::vector<std::uint8_t>>>("resource closed");
                    if (ready < 0) return result_error<std::optional<std::vector<std::uint8_t>>>("network operation failed");
                    continue;
                }
                if (ssl_error == SSL_ERROR_SYSCALL && system_error != 0)
                    return result_error<std::optional<std::vector<std::uint8_t>>>(network_error_from_errno(system_error));
                return result_error<std::optional<std::vector<std::uint8_t>>>("TLS operation failed");
            }
        } catch (const TaskCancelled&) {
            throw;
        } catch (const Failure&) {
            throw;
        } catch (...) {
            return result_error<std::optional<std::vector<std::uint8_t>>>("TLS operation failed");
        }
    }, file, line, column);
}
inline Task<StringResult<std::int64_t>> tls_write(
    const std::shared_ptr<TlsConnection>& connection, std::vector<std::uint8_t> bytes, bool write_all,
    const std::string& file, int line, int column) {
    using Output = StringResult<std::int64_t>;
    if (!connection || !connection->is_open() || !connection->session)
        fail(file, line, column, "TlsConnection is closed");
    if (bytes.empty())
        return make_io_task<Output>([]() -> Output { return result_ok<std::int64_t>(0); }, file, line, column);
    auto pending = std::make_shared<PendingSocketOperation>(connection->pending_write, connection);
    if (!pending->acquire()) {
        return make_io_task<Output>([]() -> Output {
            return result_error<std::int64_t>("connection already has a pending write");
        }, file, line, column);
    }
    return make_io_task<Output>([connection, pending, bytes = std::move(bytes), write_all, file, line, column]() -> Output {
        PendingSocketOperationReset pending_reset{pending};
        if (!connection || !connection->is_open() || !connection->session)
            return result_error<std::int64_t>("resource closed");
        UniqueSocketFd readiness_descriptor(connection->socket->duplicate());
        if (readiness_descriptor.value < 0) return result_error<std::int64_t>("resource closed");
        std::size_t offset = 0;
        while (offset < bytes.size()) {
            task_checkpoint();
            const auto remaining = std::min<std::size_t>(
                bytes.size() - offset, static_cast<std::size_t>(std::numeric_limits<int>::max()));
            int status = 0;
            int ssl_error = SSL_ERROR_NONE;
            int system_error = 0;
            {
                std::lock_guard<std::mutex> lock(connection->session->mutex);
                if (connection->session->closed || !connection->session->ssl)
                    return result_error<std::int64_t>("resource closed");
                errno = 0;
                status = SSL_write(connection->session->ssl, bytes.data() + offset, static_cast<int>(remaining));
                if (status <= 0) {
                    ssl_error = SSL_get_error(connection->session->ssl, status);
                    system_error = errno;
                }
            }
            if (status > 0) {
                offset += static_cast<std::size_t>(status);
                if (!write_all) break;
                continue;
            }
            if (ssl_error == SSL_ERROR_WANT_READ || ssl_error == SSL_ERROR_WANT_WRITE) {
                const short event = ssl_error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT;
                const int ready = poll_socket(connection->socket, readiness_descriptor.value, event, file, line, column);
                if (ready == -2) return result_error<std::int64_t>("resource closed");
                if (ready < 0) return result_error<std::int64_t>("network operation failed");
                continue;
            }
            if (ssl_error == SSL_ERROR_SYSCALL && system_error != 0)
                return result_error<std::int64_t>(network_error_from_errno(system_error));
            return result_error<std::int64_t>("TLS operation failed");
        }
        return result_ok<std::int64_t>(static_cast<std::int64_t>(offset));
    }, file, line, column);
}
inline Task<StringResult<std::int64_t>> tls_write_all(
    const std::shared_ptr<TlsConnection>& connection, std::vector<std::uint8_t> bytes,
    const std::string& file, int line, int column) {
    return tls_write(connection, std::move(bytes), true, file, line, column);
}

#endif

inline Task<StringResult<std::int64_t>> tcp_write_all(
    const std::shared_ptr<TcpConnection>& connection, std::vector<std::uint8_t> bytes,
    const std::string& file, int line, int column) {
    return tcp_write(connection, std::move(bytes), true, file, line, column);
}

inline StringResult<std::int64_t> run_tasks(const std::string& file, int line, int column) {
    auto& scheduler = native_scheduler();
    {
        std::lock_guard<std::mutex> lock(scheduler.mutex);
        if (scheduler.running)
            return result_error<std::int64_t>("runTasks cannot be called recursively");
        scheduler.running = true;
        scheduler.completed = 0;
    }
    std::vector<std::shared_ptr<TaskControl>> completed_tasks;
    std::size_t completed_count = 0;
    for (;;) {
        std::vector<std::shared_ptr<TaskControl>> to_start;
        bool all_done = false;
        {
            std::lock_guard<std::mutex> lock(scheduler.mutex);
            for (const auto& task : scheduler.tasks) {
                if (!task->started.load(std::memory_order_acquire)) to_start.push_back(task);
            }
            all_done = std::all_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return task->done.load(std::memory_order_acquire);
            });
            if (all_done) {
                completed_tasks = scheduler.tasks;
                completed_count = scheduler.completed;
                scheduler.tasks.clear();
                scheduler.running = false;
            }
        }
        if (all_done) break;
        for (const auto& task : to_start) task->start();
        std::unique_lock<std::mutex> lock(scheduler.mutex);
        scheduler.changed.wait(lock, [&scheduler]() {
            return std::any_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return !task->started.load(std::memory_order_acquire);
            }) || std::all_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return task->done.load(std::memory_order_acquire);
            });
        });
    }
    for (const auto& task : completed_tasks) task->join();
    if (requested_shutdown_signal != 0) check_shutdown(file, line, column);
    for (const auto& task : completed_tasks) {
        if (task->owner) continue;
        const auto failure = task->failure_message();
        if (!failure.empty()) return result_error<std::int64_t>("async task failed: " + failure);
    }
    return result_ok<std::int64_t>(static_cast<std::int64_t>(completed_count));
}

struct ByteBuffer { std::vector<std::uint8_t> values; };
inline StringResult<std::shared_ptr<ByteBuffer>> byte_buffer(
    std::int64_t length, const std::string&, int, int) {
    if (length < 0 || static_cast<std::uint64_t>(length) > MAX_BYTE_BUFFER_LENGTH)
        return result_error<std::shared_ptr<ByteBuffer>>(
            "ByteBuffer length must be between 0 and 67108864 bytes");
    try {
        auto buffer = std::make_shared<ByteBuffer>();
        buffer->values.assign(static_cast<std::size_t>(length), 0);
        return result_ok<std::shared_ptr<ByteBuffer>>(std::move(buffer));
    } catch (const std::bad_alloc&) {
        return result_error<std::shared_ptr<ByteBuffer>>("cannot allocate ByteBuffer");
    } catch (const std::length_error&) {
        return result_error<std::shared_ptr<ByteBuffer>>("cannot allocate ByteBuffer");
    }
}
inline std::int64_t byte_buffer_length(const std::shared_ptr<ByteBuffer>& buffer) {
    return buffer ? static_cast<std::int64_t>(buffer->values.size()) : 0;
}
inline StringResult<std::uint8_t> byte_buffer_get(
    const std::shared_ptr<ByteBuffer>& buffer, std::int64_t index,
    const std::string&, int, int) {
    if (!buffer || index < 0 || static_cast<std::uint64_t>(index) >= buffer->values.size())
        return result_error<std::uint8_t>("ByteBuffer index is out of bounds");
    return result_ok<std::uint8_t>(buffer->values[static_cast<std::size_t>(index)]);
}
inline StringResult<bool> byte_buffer_set(
    const std::shared_ptr<ByteBuffer>& buffer, std::int64_t index, std::uint8_t value,
    const std::string&, int, int) {
    if (!buffer || index < 0 || static_cast<std::uint64_t>(index) >= buffer->values.size())
        return result_error<bool>("ByteBuffer index is out of bounds");
    buffer->values[static_cast<std::size_t>(index)] = value;
    return result_ok<bool>(true);
}
inline StringResult<std::vector<std::uint8_t>> byte_buffer_to_bytes(
    const std::shared_ptr<ByteBuffer>& buffer, const std::string&, int, int) {
    if (!buffer) return result_error<std::vector<std::uint8_t>>("cannot allocate Bytes snapshot");
    try { return result_ok<std::vector<std::uint8_t>>(buffer->values); }
    catch (const std::bad_alloc&) { return result_error<std::vector<std::uint8_t>>("cannot allocate Bytes snapshot"); }
}

inline std::filesystem::path tyvera_path(const std::string& value) {
    auto path = std::filesystem::u8path(value);
    return path.is_absolute() ? path : (project_root() / path);
}
inline std::filesystem::path normalized_path(const std::filesystem::path& path, std::error_code& error) {
    auto result = std::filesystem::weakly_canonical(path, error);
    return error ? path.lexically_normal() : result;
}
inline std::string path_comparison_key(const std::filesystem::path& path) {
    auto key = path.generic_u8string();
#ifdef _WIN32
    for (auto& character : key) {
        if (character >= 'A' && character <= 'Z') character = static_cast<char>(character - 'A' + 'a');
    }
    // Win32 path lookup ignores trailing spaces and dots in ordinary path components.
    std::string normalized;
    std::size_t start = 0;
    while (start < key.size()) {
        const auto end = key.find('/', start);
        auto component = key.substr(start, end == std::string::npos ? end : end - start);
        while (!component.empty() && (component.back() == '.' || component.back() == ' ')) component.pop_back();
        normalized += component;
        if (end == std::string::npos) break;
        normalized.push_back('/');
        start = end + 1;
    }
    return normalized;
#else
    return key;
#endif
}
inline bool path_is_secret_or_child(const std::string& candidate, const std::string& secret) {
    if (candidate == secret) return true;
    if (candidate.size() <= secret.size() || candidate.compare(0, secret.size(), secret) != 0)
        return false;
    const auto boundary = candidate[secret.size()];
    if (boundary == '/') return true;
#ifdef _WIN32
    // NTFS alternate data stream syntax can name the protected file's default stream.
    if (boundary == ':') return true;
#endif
    return false;
}
inline bool is_secret_storage_path(const std::filesystem::path& path) noexcept {
    try {
        const auto secret_path = project_root() / ".tyvera.secrets";
        std::error_code candidate_absolute_error, secret_absolute_error;
        const auto candidate_absolute = std::filesystem::absolute(path, candidate_absolute_error).lexically_normal();
        const auto secret_absolute = std::filesystem::absolute(secret_path, secret_absolute_error).lexically_normal();
        // Lexical checks still protect direct, absolute, and traversal paths when
        // canonicalization cannot inspect an unrelated permission-restricted path.
        if (candidate_absolute_error || secret_absolute_error) return true;
        const auto candidate_lexical_key = path_comparison_key(candidate_absolute);
        const auto secret_lexical_key = path_comparison_key(secret_absolute);
        if (path_is_secret_or_child(candidate_lexical_key, secret_lexical_key)) return true;

        std::error_code candidate_error, secret_error;
        const auto candidate = normalized_path(path, candidate_error);
        const auto secret = normalized_path(secret_path, secret_error);
        if (candidate_error || secret_error) return false;
        return path_is_secret_or_child(path_comparison_key(candidate), path_comparison_key(secret));
    } catch (...) { return true; }
}
inline std::string path_join(const std::string& left, const std::string& right,
                             const std::string& file, int line, int column) {
    try { return (std::filesystem::u8path(left) / std::filesystem::u8path(right)).generic_u8string(); }
    catch (...) { fail(file, line, column, "invalid path"); }
}
inline bool path_exists(const std::string& value) {
    try {
        std::error_code error;
        return std::filesystem::exists(tyvera_path(value), error) && !error;
    } catch (...) { return false; }
}

inline bool valid_utf8_string(const std::string& value) {
    std::vector<std::uint8_t> bytes;
    try {
        bytes.reserve(value.size());
        for (unsigned char byte : value) bytes.push_back(byte);
    } catch (...) { return false; }
    return valid_utf8(bytes);
}
inline std::string universal_newlines(std::string value) {
    std::string result;
    result.reserve(value.size());
    for (std::size_t index = 0; index < value.size(); ++index) {
        if (value[index] == '\r') {
            if (index + 1 < value.size() && value[index + 1] == '\n') ++index;
            result.push_back('\n');
        } else result.push_back(value[index]);
    }
    return result;
}
inline StringResult<std::string> read_text(const std::string& name) {
    try {
        const auto path = tyvera_path(name);
        if (is_secret_storage_path(path))
            return result_error<std::string>("secret file can only be read through the secrets provider");
        std::ifstream stream(path, std::ios::binary);
        if (!stream) return result_error<std::string>("cannot read text file");
        std::string value((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
        if (stream.bad()) return result_error<std::string>("cannot read text file");
        if (!valid_utf8_string(value)) return result_error<std::string>("text file is not valid UTF-8");
        return result_ok<std::string>(universal_newlines(std::move(value)));
    } catch (...) { return result_error<std::string>("cannot read text file"); }
}
inline StringResult<bool> write_text(const std::string& name, const std::string& value) {
    try {
        const auto path = tyvera_path(name);
        if (is_secret_storage_path(path))
            return result_error<bool>("secret file cannot be changed by the text-file API");
        std::ofstream stream(path, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!stream) return result_error<bool>("cannot write text file");
        stream.write(value.data(), static_cast<std::streamsize>(value.size()));
        stream.close();
        return stream ? result_ok<bool>(true) : result_error<bool>("cannot write text file");
    } catch (...) { return result_error<bool>("cannot write text file"); }
}
inline StringResult<std::vector<std::uint8_t>> read_bytes(const std::string& name) {
    try {
        const auto path = tyvera_path(name);
        if (is_secret_storage_path(path))
            return result_error<std::vector<std::uint8_t>>("secret file can only be read through the secrets provider");
        std::ifstream stream(path, std::ios::binary);
        if (!stream) return result_error<std::vector<std::uint8_t>>("cannot read binary file");
        std::vector<std::uint8_t> bytes;
        std::array<char, 8192> buffer{};
        while (stream) {
            stream.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
            const auto count = static_cast<std::size_t>(stream.gcount());
            if (count > MAX_BYTE_BUFFER_LENGTH - bytes.size())
                return result_error<std::vector<std::uint8_t>>(
                    "binary file exceeds the 67108864-byte limit");
            for (std::size_t index = 0; index < count; ++index)
                bytes.push_back(static_cast<std::uint8_t>(static_cast<unsigned char>(buffer[index])));
            if (stream.bad()) return result_error<std::vector<std::uint8_t>>("cannot read binary file");
            if (stream.fail() && !stream.eof())
                return result_error<std::vector<std::uint8_t>>("cannot read binary file");
        }
        return result_ok<std::vector<std::uint8_t>>(std::move(bytes));
    } catch (...) { return result_error<std::vector<std::uint8_t>>("cannot read binary file"); }
}
inline StringResult<bool> write_bytes(const std::string& name, const std::vector<std::uint8_t>& bytes) {
    if (bytes.size() > MAX_BYTE_BUFFER_LENGTH)
        return result_error<bool>("binary data exceeds the 67108864-byte limit");
    try {
        const auto path = tyvera_path(name);
        if (is_secret_storage_path(path))
            return result_error<bool>("secret file cannot be changed by the binary-file API");
        std::ofstream stream(path, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!stream) return result_error<bool>("cannot write binary file");
        if (!bytes.empty())
            stream.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
        stream.close();
        return stream ? result_ok<bool>(true) : result_error<bool>("cannot write binary file");
    } catch (...) { return result_error<bool>("cannot write binary file"); }
}

struct TextFile {
    std::fstream stream;
    std::string mode;
    std::string resource_type;
    std::string source_file;
    int source_line = 1;
    int source_column = 1;
    bool closed = false;
    bool close_now() noexcept {
        if (closed) return false;
        closed = true;
        bool ok = true;
        if (mode != "read") { stream.flush(); if (!stream) ok = false; }
        stream.close();
        if (stream.fail()) ok = false;
        return ok;
    }
    ~TextFile() noexcept {
        if (!closed && !close_now())
            record_cleanup_failure(source_file, source_line, source_column, "cannot close text file");
    }
};
inline StringResult<std::shared_ptr<TextFile>> open_text_resource(
    const std::string& path_text, const std::string& mode, const std::string& resource_type,
    const std::string& file, int line, int column) {
    const auto invalid_mode = resource_type == "TextWriter"
        ? "openTextWriter mode must be 'write' or 'append'"
        : "openText mode must be 'read', 'write', or 'append'";
    if ((resource_type == "TextReader" && mode != "read")
        || (resource_type == "TextWriter" && mode != "write" && mode != "append")
        || (resource_type == "TextFile" && mode != "read" && mode != "write" && mode != "append"))
        return result_error<std::shared_ptr<TextFile>>(invalid_mode);
    std::shared_ptr<TextFile> resource;
    try {
        const auto path = tyvera_path(path_text);
        if (is_secret_storage_path(path))
            return result_error<std::shared_ptr<TextFile>>("secret file can only be accessed through the secrets provider");
        resource = std::make_shared<TextFile>();
        resource->mode = mode;
        resource->resource_type = resource_type;
        resource->source_file = file;
        resource->source_line = line;
        resource->source_column = column;
        auto flags = std::ios::binary;
        if (mode == "read") flags |= std::ios::in;
        else if (mode == "write") flags |= std::ios::out | std::ios::trunc;
        else flags |= std::ios::out | std::ios::app;
        resource->stream.open(path, flags);
        if (!resource->stream) {
            resource->closed = true;
            return result_error<std::shared_ptr<TextFile>>("cannot open text file");
        }
    } catch (...) {
        // A failed open never acquired a Tyvera resource and must not produce a
        // second, misleading cleanup failure while its temporary handle dies.
        if (resource) resource->closed = true;
        return result_error<std::shared_ptr<TextFile>>("cannot open text file");
    }
    return result_ok<std::shared_ptr<TextFile>>(std::move(resource));
}
inline StringResult<std::shared_ptr<TextFile>> open_text(
    const std::string& path, const std::string& mode, const std::string& resource_type,
    const std::string& file, int line, int column) {
    return open_text_resource(path, mode, resource_type, file, line, column);
}
inline StringResult<std::shared_ptr<TextFile>> open_text_reader(
    const std::string& path, const std::string& file, int line, int column) {
    return open_text_resource(path, "read", "TextReader", file, line, column);
}
inline StringResult<std::shared_ptr<TextFile>> open_text_writer(
    const std::string& path, const std::string& mode, const std::string& file, int line, int column) {
    return open_text_resource(path, mode, "TextWriter", file, line, column);
}
inline bool text_is_open(const std::shared_ptr<TextFile>& resource, const std::string&, int, int) {
    return resource && !resource->closed && resource->stream.is_open();
}
inline void set_resource_location(const std::shared_ptr<TextFile>& resource,
                                  const std::string& file, int line, int column) {
    if (resource) {
        resource->source_file = file;
        resource->source_line = line;
        resource->source_column = column;
    }
}
inline bool text_close(const std::shared_ptr<TextFile>& resource, const std::string&, int, int) {
    return resource && resource->close_now();
}
inline void require_open_text(const std::shared_ptr<TextFile>& resource,
                              const std::string& file, int line, int column) {
    if (!resource || resource->closed || !resource->stream.is_open())
        fail(file, line, column, resource ? resource->resource_type + " is closed" : "text resource is unavailable");
}
inline StringResult<std::string> text_read_all(
    const std::shared_ptr<TextFile>& resource, const std::string& file, int line, int column) {
    require_open_text(resource, file, line, column);
    if (resource->mode != "read") return result_error<std::string>("text file is not open for reading");
    try {
        std::string value((std::istreambuf_iterator<char>(resource->stream)), std::istreambuf_iterator<char>());
        if (resource->stream.bad() || !valid_utf8_string(value))
            return result_error<std::string>("cannot read text file");
        return result_ok<std::string>(std::move(value));
    } catch (...) { return result_error<std::string>("cannot read text file"); }
}
inline StringResult<bool> text_write(
    const std::shared_ptr<TextFile>& resource, const std::string& value,
    const std::string& file, int line, int column) {
    require_open_text(resource, file, line, column);
    if (resource->mode == "read") return result_error<bool>("text file is not open for writing");
    resource->stream.write(value.data(), static_cast<std::streamsize>(value.size()));
    return resource->stream ? result_ok<bool>(true) : result_error<bool>("cannot write text file");
}
inline StringResult<bool> text_append(
    const std::shared_ptr<TextFile>& resource, const std::string& value,
    const std::string& file, int line, int column) {
    require_open_text(resource, file, line, column);
    if (resource->mode != "append") return result_error<bool>("text file is not open for appending");
    resource->stream.write(value.data(), static_cast<std::streamsize>(value.size()));
    return resource->stream ? result_ok<bool>(true) : result_error<bool>("cannot append to text file");
}
inline StringResult<bool> text_flush(
    const std::shared_ptr<TextFile>& resource, const std::string& file, int line, int column) {
    require_open_text(resource, file, line, column);
    resource->stream.flush();
    return resource->stream ? result_ok<bool>(true) : result_error<bool>("cannot flush text file");
}

inline int compare_int_float(std::int64_t integer, double floating) {
    const double minimum = -9223372036854775808.0;
    const double limit = 9223372036854775808.0;
    if (floating < minimum) return 1;
    if (floating >= limit) return -1;
    double integral = 0.0;
    const double fraction = std::modf(floating, &integral);
    const auto truncated = static_cast<std::int64_t>(integral);
    if (integer < truncated) return -1;
    if (integer > truncated) return 1;
    if (fraction > 0.0) return -1;
    if (fraction < 0.0) return 1;
    return 0;
}

inline std::string format_float(double value) {
    char buffer[128];
    const auto converted = std::to_chars(buffer, buffer + sizeof(buffer), value, std::chars_format::general);
    if (converted.ec != std::errc()) return "<float-format-error>";
    std::string text(buffer, converted.ptr);
    const auto exponent_position = text.find_first_of("eE");
    if (exponent_position == std::string::npos) {
        if (text.find('.') == std::string::npos) text += ".0";
        return text;
    }
    const std::string mantissa = text.substr(0, exponent_position);
    int exponent = std::stoi(text.substr(exponent_position + 1));
    if (exponent < -4 || exponent >= 16) {
        const auto sign = exponent >= 0 ? '+' : '-';
        int magnitude = exponent >= 0 ? exponent : -exponent;
        std::string digits = magnitude < 10 ? "0" + std::to_string(magnitude) : std::to_string(magnitude);
        return mantissa + "e" + sign + digits;
    }
    const bool negative = !mantissa.empty() && mantissa[0] == '-';
    const std::string unsigned_mantissa = negative ? mantissa.substr(1) : mantissa;
    const auto point = unsigned_mantissa.find('.');
    const std::size_t before_point = point == std::string::npos ? unsigned_mantissa.size() : point;
    std::string digits;
    for (char character : unsigned_mantissa) if (character != '.') digits += character;
    const auto decimal_position = static_cast<long long>(before_point) + exponent;
    std::string fixed;
    if (decimal_position <= 0) {
        fixed = "0." + std::string(static_cast<std::size_t>(-decimal_position), '0') + digits;
    } else if (static_cast<std::size_t>(decimal_position) >= digits.size()) {
        fixed = digits + std::string(static_cast<std::size_t>(decimal_position) - digits.size(), '0') + ".0";
    } else {
        fixed = digits.substr(0, static_cast<std::size_t>(decimal_position)) + "." + digits.substr(static_cast<std::size_t>(decimal_position));
    }
    return negative ? "-" + fixed : fixed;
}
} // namespace tyvera_runtime
'''

