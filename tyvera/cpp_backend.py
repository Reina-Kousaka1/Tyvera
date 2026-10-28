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
    IRActor,
    IRActorRefType,
    IRActorSelfRef,
    IRActorSend,
    IRActorRequest,
    IRActorSpawn,
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
    IREnum,
    IREnumCreate,
    IREnumPattern,
    IRForEach,
    IRFieldAccess,
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
    IRRecord,
    IRRecordCreate,
    IRRecordPattern,
    IRUsing,
    IRType,
    IRTypeRef,
    IRUserType,
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
            raise Diagnostic(module.functions[0].location, "backend", "native TLS, WebSocket, and HTTP clients are currently supported only on Linux")
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


def _cpp_actor_identifier(actor_name: str) -> str:
    encoded = "".join(
        character if character.isascii() and character.isalnum() else f"_x{ord(character):x}_"
        for character in actor_name
    )
    return f"tyv_actor_{encoded}"


def _cpp_user_type_identifier(type_name: str) -> str:
    encoded = "".join(
        character if character.isascii() and character.isalnum() else f"_x{ord(character):x}_"
        for character in type_name
    )
    return f"tyv_type_{encoded}"


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
        self.actor_context: IRActor | None = None

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
        for declaration in self._ordered_nominal_types():
            if isinstance(declaration, IRRecord):
                self._record_declaration(declaration)
            else:
                self._enum_declaration(declaration)
            self._line("")
        for actor in self.module.actors:
            self._actor_declaration(actor)
            self._line("")
        for function in self.module.functions:
            if function.actor_name is None:
                self._line(self._function_signature(function) + ";")
        for function in self.module.functions:
            if function.actor_name is None:
                self._line("")
                self._function(function)
        for function in self.module.functions:
            if function.actor_name is not None:
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
        self._line("tyvera_runtime::ActorRuntimeScope actor_runtime_scope;")
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

    def _ordered_nominal_types(self) -> list[IRRecord | IREnum]:
        declarations: dict[str, IRRecord | IREnum] = {
            item.name: item for item in (*self.module.records, *self.module.enums)
        }
        ordered: list[IRRecord | IREnum] = []
        visiting: set[str] = set()
        complete: set[str] = set()

        def type_dependencies(type_ref: IRTypeRef) -> set[str]:
            if isinstance(type_ref, IRUserType):
                return {type_ref.name}
            if isinstance(type_ref, IRAppliedType):
                dependencies: set[str] = set()
                for argument in type_ref.arguments:
                    dependencies.update(type_dependencies(argument))
                return dependencies
            return set()

        def visit(name: str) -> None:
            if name in complete:
                return
            declaration = declarations[name]
            if name in visiting:
                raise Diagnostic(
                    declaration.location, "backend",
                    f"native backend does not yet support recursive record or enum layout '{name}'",
                )
            visiting.add(name)
            if isinstance(declaration, IRRecord):
                referenced = set().union(*(type_dependencies(field.type) for field in declaration.fields))
            else:
                referenced = set().union(*(
                    type_dependencies(payload)
                    for variant in declaration.variants for payload in variant.payload_types
                ))
            for dependency in sorted(referenced):
                if dependency == name:
                    raise Diagnostic(
                        declaration.location, "backend",
                        f"native backend does not yet support recursive record or enum layout '{name}'",
                    )
                visit(dependency)
            visiting.remove(name)
            complete.add(name)
            ordered.append(declaration)

        for declaration in (*self.module.records, *self.module.enums):
            visit(declaration.name)
        return ordered

    def _record_declaration(self, record: IRRecord) -> None:
        type_name = _cpp_user_type_identifier(record.name)
        self._line(f"struct {type_name} {{")
        self.indent += 1
        for index, field in enumerate(record.fields):
            self._line(f"{self._cpp_type(field.type)} tyv_field_{index}{{}};")
        self._line(f"bool operator==(const {type_name}& other) const {{")
        self.indent += 1
        if not record.fields:
            self._line("return true;")
        else:
            comparisons = [
                f"tyvera_runtime::equal(tyv_field_{index}, other.tyv_field_{index})"
                for index in range(len(record.fields))
            ]
            self._line("return " + " && ".join(comparisons) + ";")
        self.indent -= 1
        self._line("}")
        self.indent -= 1
        self._line("};")

    def _enum_declaration(self, enum: IREnum) -> None:
        if not enum.variants:
            raise Diagnostic(enum.location, "backend", f"native backend does not support empty enum '{enum.name}'")
        type_name = _cpp_user_type_identifier(enum.name)
        payloads = ", ".join(
            "std::tuple<" + ", ".join(self._cpp_type(value) for value in variant.payload_types) + ">"
            for variant in enum.variants
        )
        self._line(f"struct {type_name} {{")
        self.indent += 1
        self._line(f"using Payload = std::variant<{payloads}>;")
        self._line("std::size_t tag{};")
        self._line("Payload payload{};")
        for index, variant in enumerate(enum.variants):
            parameters = ", ".join(
                f"{self._cpp_type(value)} tyv_payload_{payload_index}"
                for payload_index, value in enumerate(variant.payload_types)
            )
            tuple_values = ", ".join(f"tyv_payload_{payload_index}"
                                      for payload_index in range(len(variant.payload_types)))
            tuple_types = ", ".join(self._cpp_type(value) for value in variant.payload_types)
            self._line(f"static {type_name} tyv_make_{index}({parameters}) {{")
            self.indent += 1
            self._line(
                f"return {type_name}{{{index}, Payload{{std::in_place_index<{index}>, "
                f"std::tuple<{tuple_types}>{{{tuple_values}}}}}}};"
            )
            self.indent -= 1
            self._line("}")
        self._line(f"bool operator==(const {type_name}& other) const {{")
        self.indent += 1
        self._line("if (tag != other.tag) return false;")
        self._line("switch (tag) {")
        self.indent += 1
        for index, variant in enumerate(enum.variants):
            self._line(f"case {index}:")
            self.indent += 1
            if not variant.payload_types:
                self._line("return true;")
            else:
                comparisons = [
                    f"tyvera_runtime::equal(std::get<{payload_index}>(std::get<{index}>(payload)), "
                    f"std::get<{payload_index}>(std::get<{index}>(other.payload)))"
                    for payload_index in range(len(variant.payload_types))
                ]
                self._line("return " + " && ".join(comparisons) + ";")
            self.indent -= 1
        self._line("default: return false;")
        self.indent -= 1
        self._line("}")
        self.indent -= 1
        self._line("}")
        self.indent -= 1
        self._line("};")

    def _line(self, value: str = "") -> None:
        self.lines.append("    " * self.indent + value if value else "")

    @staticmethod
    def _cpp_type(type_name: IRTypeRef) -> str:
        if isinstance(type_name, IRActorRefType):
            return "std::shared_ptr<tyvera_runtime::ActorBase>"
        if isinstance(type_name, IRUserType):
            return f"tyvera_generated::{_cpp_user_type_identifier(type_name.name)}"
        if type_name == IRType.SUPERVISOR:
            return "std::shared_ptr<tyvera_runtime::Supervisor>"
        if type_name == IRType.TASK_GROUP:
            return "tyvera_runtime::TaskGroup"
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
            if type_name.name == "Secret":
                return "tyvera_runtime::SecretString"
            if type_name.name == "ReplySender":
                return f"tyvera_runtime::ReplySender<{arguments[0]}>"
            if type_name.name == "ReplyReceiver":
                return f"tyvera_runtime::ReplyReceiver<{arguments[0]}>"
            raise ValueError(f"unknown applied Tyvera IR type: {type_name.name}")
        return {
            IRType.INT: "std::int64_t",
            IRType.FLOAT: "double",
            IRType.BOOL: "bool",
            IRType.STRING: "std::string",
            IRType.JSON: "tyvera_runtime::JsonValue",
            IRType.BYTE: "std::uint8_t",
            IRType.WEBSOCKET_URL: "tyvera_runtime::WebSocketUrl",
            IRType.WEBSOCKET: "std::shared_ptr<tyvera_runtime::WebSocket>",
            IRType.WEBSOCKET_MESSAGE: "tyvera_runtime::WebSocketMessage",
            IRType.HTTP_METHOD: "tyvera_runtime::HttpMethod",
            IRType.HTTP_URL: "tyvera_runtime::HttpUrl",
            IRType.HTTP_HEADERS: "std::shared_ptr<tyvera_runtime::HttpHeaders>",
            IRType.HTTP_REQUEST: "std::shared_ptr<tyvera_runtime::HttpRequest>",
            IRType.HTTP_RESPONSE: "std::shared_ptr<tyvera_runtime::HttpResponse>",
            IRType.RESTART_POLICY: "tyvera_runtime::RestartPolicy",
            IRType.SUPERVISOR_BACKOFF: "tyvera_runtime::SupervisorBackoff",
            IRType.SUPERVISOR: "std::shared_ptr<tyvera_runtime::Supervisor>",
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
        if function.actor_name is not None:
            return f"void {_cpp_actor_identifier(function.actor_name)}::{function.symbol}({parameters})"
        return f"{result_type} {function.symbol}({parameters})"

    def _actor_declaration(self, actor: IRActor) -> None:
        self._line(f"class {_cpp_actor_identifier(actor.name)} final : public tyvera_runtime::ActorBase {{")
        self.indent += 1
        self._line("public:")
        self.indent += 1
        for field in actor.fields:
            self._line(f"{self._cpp_type(field.type)} tyv_field_{abs(field.identifier)}{{}};")
        self._line("void tyv_clear_state() noexcept override {")
        self.indent += 1
        for field in actor.fields:
            self._line(f"tyv_field_{abs(field.identifier)} = {{}};")
        self.indent -= 1
        self._line("}")
        methods = ([actor.initializer_symbol] if actor.initializer_symbol is not None else [])
        methods.extend(symbol for _, symbol in actor.handler_symbols)
        for symbol in methods:
            function = self.function_symbols[symbol]
            parameters = ", ".join(
                f"{self._cpp_type(parameter.type)} tyv_argument_{parameter.identifier}"
                for parameter in function.parameters
            )
            self._line(f"void {symbol}({parameters});")
        self.indent -= 1
        self.indent -= 1
        self._line("};")

    def _function(self, function: IRFunction) -> None:
        self.temporary = 0
        previous_async_context = self.async_context
        previous_actor_context = self.actor_context
        self.async_context = function.is_async
        self.actor_context = next(
            (actor for actor in self.module.actors if actor.name == function.actor_name), None,
        )
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
        self.actor_context = previous_actor_context

    def _local_name(self, identifier: int) -> str:
        if identifier < 0 and self.actor_context is not None:
            return f"this->tyv_field_{abs(identifier)}"
        return f"tyv_local_{identifier}"

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
            self._line(f"{self._local_name(statement.variable_id)} = {self._expression(statement.value)};")
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
            if task_type == IRType.TASK_GROUP:
                self._line(f"({task}).await({_location_args(statement.location)});")
            elif not isinstance(task_type, IRAppliedType):
                raise ValueError("validated IR await has a non-applied operand")
            elif task_type.name == "ReplyReceiver":
                consume = "true" if statement.result is not None else "false"
                awaited = (
                    f"tyvera_runtime::await_reply({task}, {consume}, "
                    f"{_location_args(statement.location)})"
                )
                if statement.result is None:
                    self._line(f"(void){awaited};")
                else:
                    self._line(f"auto tyv_local_{statement.result.identifier} = *{awaited};")
            elif task_type.name == "Task":
                await_method = "await_move" if _ir_contains_resource(task_type.arguments[0]) else "await"
                awaited = f"{task}.{await_method}({_location_args(statement.location)})"
                if statement.result is None:
                    self._line(f"(void){awaited};")
                else:
                    keyword = "auto" if _ir_contains_resource(statement.result.type) else "const auto"
                    self._line(
                        f"{keyword} tyv_local_{statement.result.identifier} = {awaited};"
                    )
            else:
                raise ValueError("validated IR await has an unsupported awaitable")
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
        if isinstance(pattern, IRRecordPattern):
            conditions = [
                self._pattern_condition(child, f"({value}).tyv_field_{index}")
                for index, child in enumerate(pattern.fields)
            ]
            return " && ".join(f"({condition})" for condition in conditions) if conditions else "true"
        if isinstance(pattern, IREnumPattern):
            conditions = [
                self._pattern_condition(
                    child,
                    f"std::get<{payload_index}>(std::get<{pattern.variant_index}>(({value}).payload))",
                )
                for payload_index, child in enumerate(pattern.payload_patterns)
            ]
            tag = f"({value}).tag == {pattern.variant_index}"
            return " && ".join([tag, *(f"({condition})" for condition in conditions)])
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
        elif isinstance(pattern, IRRecordPattern):
            for index, child in enumerate(pattern.fields):
                self._pattern_bindings(child, f"({value}).tyv_field_{index}")
        elif isinstance(pattern, IREnumPattern):
            for payload_index, child in enumerate(pattern.payload_patterns):
                self._pattern_bindings(
                    child,
                    f"std::get<{payload_index}>(std::get<{pattern.variant_index}>(({value}).payload))",
                )

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
            return self._local_name(expression.variable_id)
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
        if isinstance(expression, IRRecordCreate):
            values = ", ".join(self._expression(argument) for argument in expression.arguments)
            return f"{self._cpp_type(expression.type)}{{{values}}}"
        if isinstance(expression, IRFieldAccess):
            return f"({self._expression(expression.receiver)}).tyv_field_{expression.field_index}"
        if isinstance(expression, IREnumCreate):
            type_name = self._cpp_type(expression.type)
            factory = f"{type_name}::tyv_make_{expression.variant_index}"
            if not expression.arguments:
                return f"{factory}()"
            temporary = self._new_temporary()
            tuple_type = "std::tuple<" + ", ".join(
                self._cpp_type(argument.type) for argument in expression.arguments
            ) + ">"
            tuple_values = ", ".join(self._expression(argument) for argument in expression.arguments)
            parameters = ", ".join(
                f"std::get<{index}>({temporary})" for index in range(len(expression.arguments))
            )
            return "\n".join([
                "([&]() -> " + type_name + " {",
                f"    const auto {temporary} = {tuple_type}{{{tuple_values}}};",
                f"    return {factory}({parameters});",
                "}())",
            ])
        if isinstance(expression, IRBuiltinCall):
            runtime_functions = {
                "oneForOne": "supervisor_one_for_one",
                "oneForAll": "supervisor_one_for_all",
                "restForOne": "supervisor_rest_for_one",
                "permanentChildPolicy": "permanent_child_policy",
                "transientChildPolicy": "transient_child_policy",
                "temporaryChildPolicy": "temporary_child_policy",
                "exponentialBackoff": "exponential_backoff",
                "hostOS": "host_os",
                "hostArchitecture": "host_architecture",
                "environment": "environment_value",
                "monotonicMilliseconds": "monotonic_milliseconds",
                "milliseconds": "duration_milliseconds",
                "seconds": "duration_seconds",
                "durationFromSeconds": "duration_from_seconds",
                "sleepFor": "sleep_for",
                "byteFromInt": "byte_from_int",
                "parseJson": "parse_json",
                "jsonStringify": "json_stringify",
                "jsonString": "json_string",
                "jsonInt": "json_int",
                "jsonFloat": "json_float",
                "jsonBool": "json_bool",
                "jsonNull": "json_null",
                "jsonArray": "json_array",
                "jsonObject": "json_object",
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
                "taskGroup": "task_group",
                "runActors": "run_actors",
                "resolveHost": "resolve_host",
                "tcpConnect": "tcp_connect",
                "tlsConnect": "tls_connect",
                "tcpListen": "tcp_listen",
                "webSocketUrl": "websocket_url",
                "webSocketConnect": "websocket_connect",
                "httpMethod": "http_method",
                "httpUrl": "http_url",
                "httpHeaders": "http_headers",
                "httpRequest": "http_request",
                "httpSend": "http_send",
                "httpGet": "http_get",
                "httpPost": "http_post",
                "httpPut": "http_put",
                "httpDelete": "http_delete",
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
            if expression.builtin in {
                "byteFromInt", "byteBuffer", "seconds", "sleepFor", "oneForOne", "oneForAll",
                "restForOne", "exponentialBackoff", "taskGroup",
            }:
                values.append(_location_args(expression.location))
            elif expression.builtin == "openText":
                values.extend([_cpp_string_literal("TextFile"), _location_args(expression.location)])
            elif expression.builtin == "openTextReader":
                values.append(_location_args(expression.location))
            elif expression.builtin == "openTextWriter":
                values.extend([_location_args(expression.location)])
            elif expression.builtin == "pathJoin":
                values.append(_location_args(expression.location))
            elif expression.builtin in {"runTasks", "runActors", "resolveHost", "tcpConnect", "tlsConnect", "tcpListen",
                                         "webSocketConnect", "httpSend", "httpGet", "httpPost", "httpPut", "httpDelete"}:
                values.append(_location_args(expression.location))
            call = f"tyvera_runtime::{function}({', '.join(values)})"
            lines.append(f"    return {call};")
            lines.append("}())")
            return "\n".join(lines)
        if isinstance(expression, IRActorSelfRef):
            return "shared_from_this()"
        if isinstance(expression, IRActorSpawn):
            temporaries = [self._new_temporary() for _ in expression.arguments]
            result_type = self._cpp_type(expression.type)
            lines = [f"([&]() -> {result_type} {{"]
            for name, argument in zip(temporaries, expression.arguments):
                lines.append(f"    const auto {name} = {self._expression(argument)};")
            actor_type = _cpp_actor_identifier(expression.actor_name)
            lines.append(
                f"    auto tyv_actor = tyvera_runtime::spawn_actor<{actor_type}>("
                f"{_cpp_string_literal(expression.actor_name)}, {_location_args(expression.location)});"
            )
            if expression.initializer_symbol is not None:
                lines.append("    try {")
                lines.append("        tyvera_runtime::ActorHandlerScope actor_scope(tyv_actor);")
                lines.append(f"        tyv_actor->{expression.initializer_symbol}({', '.join(temporaries)});")
                lines.append("    } catch (...) {")
                lines.append("        tyvera_runtime::actor_initialization_failed(tyv_actor);")
                lines.append("        throw;")
                lines.append("    }")
            if expression.restartable:
                weak_actor = self._new_temporary()
                lines.append(f"    const std::weak_ptr<{actor_type}> {weak_actor} = tyv_actor;")
                captures = [weak_actor, *temporaries]
                capture_list = ", ".join(captures)
                lines.append(f"    tyv_actor->tyv_restart = [{capture_list}]() {{")
                lines.append(f"        auto target = {weak_actor}.lock();")
                lines.append("        if (!target) throw std::runtime_error(\"supervised actor expired\");")
                if expression.initializer_symbol is not None:
                    lines.append("        tyvera_runtime::ActorHandlerScope actor_scope(target);")
                    lines.append(f"        target->{expression.initializer_symbol}({', '.join(temporaries)});")
                lines.append("    };")
            lines.append("    tyvera_runtime::actor_started(tyv_actor);")
            lines.append("    return std::static_pointer_cast<tyvera_runtime::ActorBase>(std::move(tyv_actor));")
            lines.append("}())")
            return "\n".join(lines)
        if isinstance(expression, IRActorSend):
            target = self._new_temporary()
            typed_target = self._new_temporary()
            weak_target = self._new_temporary()
            arguments = [self._new_temporary() for _ in expression.arguments]
            actor_type = _cpp_actor_identifier(expression.actor_name)
            lines = ["([&]() -> void {"]
            lines.append(f"    const auto {target} = {self._expression(expression.receiver)};")
            for name, argument in zip(arguments, expression.arguments):
                lines.append(f"    const auto {name} = {self._expression(argument)};")
            lines.append(f"    auto {typed_target} = std::dynamic_pointer_cast<{actor_type}>({target});")
            lines.append(
                f"    if (!{typed_target}) throw tyvera_runtime::Failure({_location_args(expression.location)}, "
                f"\"actor reference type mismatch\");"
            )
            lines.append(f"    const std::weak_ptr<{actor_type}> {weak_target} = {typed_target};")
            captures = ", ".join([weak_target, *arguments])
            args = ", ".join(arguments)
            lines.append(
                f"    tyvera_runtime::enqueue_actor({target}, {_cpp_string_literal(expression.actor_name)}, "
                f"[{captures}]() {{ if (auto target = {weak_target}.lock()) target->{expression.handler_symbol}({args}); }}, "
                f"{_location_args(expression.location)});"
            )
            lines.append("}())")
            return "\n".join(lines)
        if isinstance(expression, IRActorRequest):
            target = self._new_temporary()
            typed_target = self._new_temporary()
            weak_target = self._new_temporary()
            arguments = [self._new_temporary() for _ in expression.arguments]
            actor_type = _cpp_actor_identifier(expression.actor_name)
            cpp_reply_type = self._cpp_type(expression.type.arguments[0])
            lines = [f"([&]() -> {self._cpp_type(expression.type)} {{"]
            lines.append(f"    const auto {target} = {self._expression(expression.receiver)};")
            timeout_value = "std::nullopt"
            if expression.timeout is not None:
                timeout = self._new_temporary()
                lines.append(f"    const auto {timeout} = {self._expression(expression.timeout)};")
                timeout_value = f"std::optional<std::int64_t>{{{timeout}.milliseconds}}"
            for name, argument in zip(arguments, expression.arguments):
                lines.append(f"    const auto {name} = {self._expression(argument)};")
            lines.append(f"    auto {typed_target} = std::dynamic_pointer_cast<{actor_type}>({target});")
            lines.append(
                f"    if (!{typed_target}) throw tyvera_runtime::Failure({_location_args(expression.location)}, "
                f"{_cpp_string_literal('actor reference type mismatch')});"
            )
            lines.append(f"    const std::weak_ptr<{actor_type}> {weak_target} = {typed_target};")
            captures = ", ".join([weak_target, *arguments])
            handler_arguments = ", ".join([*arguments, "reply"])
            lines.append(
                f"    return tyvera_runtime::actor_request<{cpp_reply_type}>("
                f"{target}, {_cpp_string_literal(expression.actor_name)}, {timeout_value}, "
                f"[{captures}](tyvera_runtime::ReplySender<{cpp_reply_type}> reply) {{ "
                f"return [{captures}{', ' if captures else ''}reply]() {{ "
                f"if (auto target = {weak_target}.lock()) target->{expression.handler_symbol}({handler_arguments}); "
                f"}}; }}, {_location_args(expression.location)});"
            )
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
            if operator in {"==", "!="} and (
                isinstance(left_type, IRAppliedType) or isinstance(left_type, IRUserType)
                or left_type == IRType.JSON
            ):
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
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "Secret":
            if method == "reveal":
                return f"({receiver}).reveal()"
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "ReplySender":
            if method == "send":
                return f"({receiver}).send({arguments[0]})"
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "ReplyReceiver":
            functions = {
                "isReady": "is_ready",
                "isCancelled": "is_cancelled",
                "isTimedOut": "is_timed_out",
                "cancel": "cancel",
                "demonitor": "cancel",
                "tryReceive": "try_receive",
            }
            return f"({receiver}).{functions[method]}()"
        if expression.receiver.type == IRType.JSON:
            functions = {
                "kind": "json_kind", "isNull": "json_is_null", "asString": "json_as_string",
                "asInt": "json_as_int", "asFloat": "json_as_float", "asBool": "json_as_bool",
                "arrayLength": "json_array_length", "objectLength": "json_object_length",
                "arrayAt": "json_array_at", "objectGet": "json_object_get", "objectKeys": "json_object_keys",
            }
            return f"tyvera_runtime::{functions[method]}({', '.join([receiver, *arguments])})"
        if isinstance(expression.receiver.type, IRActorRefType):
            if method == "link":
                return f"tyvera_runtime::actor_link({receiver}, {arguments[0]})"
            if method == "unlink":
                return f"tyvera_runtime::actor_unlink({receiver}, {arguments[0]})"
            if method == "monitor":
                return f"tyvera_runtime::actor_monitor({receiver}, {_location_args(expression.location)})"
            if method == "isAlive":
                return f"tyvera_runtime::actor_is_alive({receiver})"
            if method == "stop":
                return f"tyvera_runtime::actor_stop({receiver}, {_location_args(expression.location)})"
        if expression.receiver.type == IRType.TASK_GROUP:
            if method == "add":
                return f"({receiver}).add({arguments[0]}, {_location_args(expression.location)})"
            functions = {"isDone": "is_done", "cancel": "cancel"}
            return f"({receiver}).{functions[method]}({_location_args(expression.location)})"
        if expression.receiver.type == IRType.SUPERVISOR:
            if method == "isAlive":
                return f"tyvera_runtime::supervisor_is_alive({receiver})"
            if method == "stop":
                return f"tyvera_runtime::supervisor_stop({receiver}, {_location_args(expression.location)})"
            if method == "start":
                child_type = expression.arguments[0].type
                policy = (arguments[1] if len(arguments) == 2
                          else "tyvera_runtime::RestartPolicy::permanent")
                if isinstance(child_type, IRActorRefType):
                    return (f"tyvera_runtime::supervisor_start_actor({receiver}, {arguments[0]}, {policy}, "
                            f"{_location_args(expression.location)})")
                return (f"tyvera_runtime::supervisor_start_nested({receiver}, {arguments[0]}, {policy}, "
                        f"{_location_args(expression.location)})")
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
        if expression.receiver.type == IRType.HTTP_METHOD:
            functions = {"name": "http_method_name", "isStandard": "http_method_is_standard"}
            return f"tyvera_runtime::{functions[method]}({receiver})"
        if expression.receiver.type == IRType.HTTP_URL:
            functions = {
                "scheme": "http_url_scheme", "host": "http_url_host",
                "port": "http_url_port", "target": "http_url_target",
                "authority": "http_url_authority",
            }
            return f"tyvera_runtime::{functions[method]}({receiver})"
        if expression.receiver.type == IRType.HTTP_HEADERS:
            functions = {
                "add": "http_headers_add", "set": "http_headers_set",
                "remove": "http_headers_remove", "get": "http_headers_get",
                "values": "http_headers_values", "contains": "http_headers_contains",
                "length": "http_headers_length",
            }
            values = [receiver, *arguments, _location_args(expression.location)]
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if expression.receiver.type == IRType.HTTP_REQUEST:
            functions = {
                "method": "http_request_method", "target": "http_request_target",
                "headers": "http_request_headers", "body": "http_request_body",
            }
            return f"tyvera_runtime::{functions[method]}({receiver})"
        if expression.receiver.type == IRType.HTTP_RESPONSE:
            functions = {
                "status": "http_response_status", "headers": "http_response_headers",
                "body": "http_response_body",
            }
            return f"tyvera_runtime::{functions[method]}({receiver})"
        if expression.receiver.type == IRType.WEBSOCKET_URL:
            functions = {
                "scheme": "websocket_url_scheme", "host": "websocket_url_host",
                "port": "websocket_url_port", "target": "websocket_url_target",
                "hostEndsWith": "websocket_url_host_ends_with",
            }
            return f"tyvera_runtime::{functions[method]}({receiver}{', ' if arguments else ''}{', '.join(arguments)})"
        if expression.receiver.type == IRType.WEBSOCKET_MESSAGE:
            functions = {"kind": "websocket_message_kind", "bytes": "websocket_message_bytes",
                         "text": "websocket_message_text"}
            return f"tyvera_runtime::{functions[method]}({receiver})"
        if expression.receiver.type == IRType.WEBSOCKET:
            functions = {
                "sendText": "websocket_send_text", "send": "websocket_send",
                "receive": "websocket_receive", "receiveWithin": "websocket_receive_within",
                "closeCode": "websocket_close_code", "closeReason": "websocket_close_reason",
                "close": "websocket_close", "closeGracefully": "websocket_close_gracefully",
                "isOpen": "websocket_is_open",
            }
            values = [receiver, *arguments]
            if method not in {"closeCode", "closeReason", "close", "isOpen"}:
                values.append(_location_args(expression.location))
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
        IRType.TCP_CONNECTION, IRType.TLS_CONNECTION, IRType.TCP_LISTENER, IRType.WEBSOCKET,
    }:
        return True
    return isinstance(type_ref, IRAppliedType) and any(_ir_contains_resource(item) for item in type_ref.arguments)


def _module_uses_tls(module: IRModule) -> bool:
    seen: set[int] = set()

    def contains_tls(value) -> bool:
        if isinstance(value, IRType):
            return value in {IRType.TLS_CONNECTION, IRType.WEBSOCKET}
        if isinstance(value, IRBuiltinCall) and value.builtin in {
            "httpSend", "httpGet", "httpPost", "httpPut", "httpDelete",
        }:
            return True
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
#include <cctype>
#include <cerrno>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <deque>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <limits>
#include <map>
#include <locale>
#include <memory>
#include <mutex>
#include <new>
#include <optional>
#include <stdexcept>
#include <sstream>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <tuple>
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
#include <openssl/crypto.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
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
inline std::string render_task_failure(const Failure& failure) {
    return failure.file + ":" + std::to_string(failure.line) + ":" + std::to_string(failure.column)
        + ": runtime error: " + failure.message;
}
struct TaskCancelled final : std::exception {
    const char* what() const noexcept override { return "task was cancelled"; }
};

struct NativeExecutionMutex {
    std::mutex state_mutex;
    std::condition_variable changed;
    bool locked = false;

    void lock() {
        std::unique_lock<std::mutex> lock(state_mutex);
        changed.wait(lock, [this]() { return !locked; });
        locked = true;
    }
    bool try_lock() {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (locked) return false;
        locked = true;
        return true;
    }
    template <typename Cancelled>
    bool lock_or_cancel(Cancelled&& cancelled) {
        std::unique_lock<std::mutex> lock(state_mutex);
        changed.wait(lock, [this, &cancelled]() { return !locked || cancelled(); });
        if (cancelled()) return false;
        locked = true;
        return true;
    }
    void unlock() noexcept {
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            locked = false;
        }
        changed.notify_all();
    }
    void notify_waiters() noexcept { changed.notify_all(); }
};
void notify_execution_waiters() noexcept;
struct TaskControl;
inline void reacquire_execution_lock(std::unique_lock<NativeExecutionMutex>& lock, TaskControl* cancellation_owner);
inline thread_local std::unique_lock<NativeExecutionMutex>* active_execution_lock = nullptr;
constexpr std::size_t MAX_NATIVE_TASK_VALUES_PER_RUN = 4096;
constexpr std::size_t MAX_NATIVE_UNFINISHED_TASKS_PER_RUN = 64;
inline std::atomic<std::uint64_t> next_native_task_identity{1};
struct TaskControl {
    const std::uint64_t identity = next_native_task_identity.fetch_add(1, std::memory_order_relaxed);
    std::uint64_t owner_identity = 0;
    std::atomic<bool> started{false};
    std::atomic<bool> done{false};
    std::atomic<bool> cancellation_requested{false};
    std::atomic<bool> deadline_cancellation{false};
    std::atomic<bool> waiting_for_actor_reply{false};
    std::atomic<bool> actor_reply_wait_has_deadline{false};
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
    mutable std::mutex actor_reply_failure_mutex;
    std::string actor_reply_failure;
    virtual ~TaskControl() = default;
    virtual void start() = 0;
    virtual void join() = 0;
    virtual std::string failure_message() const = 0;
    virtual std::string failure_message_for_owner() const = 0;
    virtual std::string failure_message_for_run() const = 0;
    virtual bool wait_until(std::chrono::steady_clock::time_point deadline, const TaskControl* waiter) = 0;
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
        notify_execution_waiters();
        return true;
    }
    bool is_cancelled() const noexcept { return cancellation_requested.load(std::memory_order_acquire); }
    bool is_done() const {
        if (done.load(std::memory_order_acquire) || is_cancelled()) return true;
        std::lock_guard<std::mutex> lock(child_failure_mutex);
        return !child_failure.empty();
    }
    bool has_child_failure() const {
        std::lock_guard<std::mutex> lock(child_failure_mutex);
        return !child_failure.empty();
    }
    void add_child(const std::shared_ptr<TaskControl>& child) {
        child->owner = this;
        child->owner_identity = identity;
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
    void record_actor_reply_failure(const std::string& message) {
        std::lock_guard<std::mutex> lock(actor_reply_failure_mutex);
        actor_reply_failure = message;
    }
    std::string actor_reply_failure_message() const {
        std::lock_guard<std::mutex> lock(actor_reply_failure_mutex);
        return actor_reply_failure;
    }
    void record_child_failure(const std::string& message) {
        if (message.empty()) return;
        {
            // is_done() reads child failure while holding lifecycle_mutex. Publish
            // under the same mutex so a waiter cannot miss this wakeup.
            std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex);
            std::lock_guard<std::mutex> failure_lock(child_failure_mutex);
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
                const auto message = child->failure_message_for_owner();
                if (!message.empty()) {
                    record_child_failure(message);
                    check_interruption();
                }
            }
        } catch (...) {
            if (resume_serialized && !execution_lock->owns_lock() && !is_cancelled())
                reacquire_execution_lock(*execution_lock, this);
            throw;
        }
        if (resume_serialized && !execution_lock->owns_lock() && !is_cancelled())
            reacquire_execution_lock(*execution_lock, this);
    }
    void notify_owner_failure() {
        if (!owner) return;
        const auto message = failure_message_for_owner();
        if (!message.empty()) owner->record_child_failure(message);
    }
};
inline std::string cleanup_failure_suffix();
struct NativeScheduler {
    std::mutex mutex;
    NativeExecutionMutex execution_mutex;
    std::condition_variable changed;
    std::vector<std::shared_ptr<TaskControl>> tasks;
    bool running = false;
    std::size_t completed = 0;
};
inline NativeScheduler& native_scheduler() {
    static NativeScheduler value;
    return value;
}
inline void notify_execution_waiters() noexcept {
    try { native_scheduler().execution_mutex.notify_waiters(); } catch (...) {}
}
inline void notify_task_scheduler() noexcept {
    try {
        auto& scheduler = native_scheduler();
        // Synchronize with runTasks' predicate evaluation. The predicate observes
        // actor readiness and reply-wait flags while holding this same mutex.
        std::lock_guard<std::mutex> lock(scheduler.mutex);
        scheduler.changed.notify_all();
    } catch (...) {}
}
inline thread_local TaskControl* active_task_control = nullptr;
inline void reacquire_execution_lock(std::unique_lock<NativeExecutionMutex>& lock, TaskControl* cancellation_owner) {
    if (lock.owns_lock()) return;
    auto* execution = lock.mutex();
    if (!execution) return;
    if (cancellation_owner) {
        if (execution->lock_or_cancel([cancellation_owner]() { return cancellation_owner->is_cancelled(); })) {
            lock = std::unique_lock<NativeExecutionMutex>(*execution, std::adopt_lock);
            return;
        }
        cancellation_owner->check_interruption();
        return;
    }
    lock.lock();
}
class NativeTaskExecution {
    std::unique_lock<NativeExecutionMutex> lock_;
    std::unique_lock<NativeExecutionMutex>* previous_;
    TaskControl* previous_task_;
public:
    explicit NativeTaskExecution(TaskControl& task)
        : previous_(active_execution_lock), previous_task_(active_task_control) {
        active_task_control = &task;
        if (task.serialized) {
            auto& execution = native_scheduler().execution_mutex;
            if (execution.lock_or_cancel([&task]() { return task.is_cancelled(); })) {
                lock_ = std::unique_lock<NativeExecutionMutex>(execution, std::adopt_lock);
                active_execution_lock = &lock_;
            } else {
                active_execution_lock = nullptr;
            }
        } else {
            active_execution_lock = nullptr;
        }
    }
    ~NativeTaskExecution() { active_execution_lock = previous_; active_task_control = previous_task_; }
};
struct ActiveExecutionLockScope {
    std::unique_lock<NativeExecutionMutex>* previous;
    explicit ActiveExecutionLockScope(std::unique_lock<NativeExecutionMutex>& lock)
        : previous(active_execution_lock) { active_execution_lock = &lock; }
    ~ActiveExecutionLockScope() { active_execution_lock = previous; }
};
inline void task_checkpoint() {
    if (active_task_control) active_task_control->check_interruption();
    auto* execution_lock = active_execution_lock;
    if (!execution_lock || !execution_lock->owns_lock()) return;
    execution_lock->unlock();
    std::this_thread::yield();
    reacquire_execution_lock(*execution_lock, active_task_control);
    if (active_task_control) active_task_control->check_interruption();
}
inline void attach_task_to_current_actor(const std::shared_ptr<TaskControl>& task,
                                         const std::string& file, int line, int column);
inline void register_native_task(const std::shared_ptr<TaskControl>& task,
                                 const std::string& file, int line, int column) {
    attach_task_to_current_actor(task, file, line, column);
    auto& scheduler = native_scheduler();
    bool start_now = false;
    {
        std::lock_guard<std::mutex> lock(scheduler.mutex);
        if (scheduler.tasks.size() >= MAX_NATIVE_TASK_VALUES_PER_RUN)
            throw Failure(file, line, column, "native task capacity exceeded (maximum 4096 Task values per run)");
        const auto unfinished_tasks = std::count_if(
            scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& registered) {
                return !registered->done.load(std::memory_order_acquire);
            });
        if (unfinished_tasks >= MAX_NATIVE_UNFINISHED_TASKS_PER_RUN)
            throw Failure(file, line, column, "native task capacity exceeded (maximum 64 unfinished tasks per run)");
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
                self->notify_owner_failure();
                self->lifecycle_changed.notify_all();
                auto& scheduler = native_scheduler();
                {
                    std::lock_guard<std::mutex> lock(scheduler.mutex);
                    ++scheduler.completed;
                }
                self->ready.notify_all();
                scheduler.changed.notify_all();
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
            notify_owner_failure();
            lifecycle_changed.notify_all();
            auto& scheduler = native_scheduler();
            {
                std::lock_guard<std::mutex> lock(scheduler.mutex);
                ++scheduler.completed;
            }
            ready.notify_all();
            scheduler.changed.notify_all();
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
        lock.unlock();
        if (active_task_control) active_task_control->check_interruption();
        lock.lock();
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
        lock.unlock();
        if (active_task_control) active_task_control->check_interruption();
        lock.lock();
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const TaskCancelled&) { throw Failure(file, line, column, "task was cancelled"); }
        }
        if (!result) throw Failure(file, line, column, "native task result was already consumed");
        T value = std::move(*result);
        result.reset();
        return value;
    }
    bool wait_until(std::chrono::steady_clock::time_point deadline, const TaskControl* waiter) override {
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
    std::string failure_message_for_owner() const override {
        std::lock_guard<std::mutex> lock(mutex);
        std::string message;
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const Failure& failure) {
                message = has_child_failure() ? failure.what() : render_task_failure(failure);
            }
            catch (const TaskCancelled&) {}
            catch (...) { message = "native task failed"; }
        }
        return message + cleanup_failure;
    }
    std::string failure_message_for_run() const override {
        const auto reply_failure = actor_reply_failure_message();
        std::lock_guard<std::mutex> lock(mutex);
        if (!reply_failure.empty()) return reply_failure + cleanup_failure;
        std::string message;
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const Failure& failure) {
                message = has_child_failure() ? failure.what() : render_task_failure(failure);
            }
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
                self->notify_owner_failure();
                self->lifecycle_changed.notify_all();
                auto& scheduler = native_scheduler();
                { std::lock_guard<std::mutex> lock(scheduler.mutex); ++scheduler.completed; }
                self->ready.notify_all();
                scheduler.changed.notify_all();
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
            notify_owner_failure();
            lifecycle_changed.notify_all();
            auto& scheduler = native_scheduler();
            { std::lock_guard<std::mutex> lock(scheduler.mutex); ++scheduler.completed; }
            ready.notify_all();
            scheduler.changed.notify_all();
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
        lock.unlock();
        if (active_task_control) active_task_control->check_interruption();
        lock.lock();
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const TaskCancelled&) { throw Failure(file, line, column, "task was cancelled"); }
        }
    }
    bool wait_until(std::chrono::steady_clock::time_point deadline, const TaskControl* waiter) override {
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
    std::string failure_message_for_owner() const override {
        std::lock_guard<std::mutex> lock(mutex);
        std::string message;
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const Failure& failure) {
                message = has_child_failure() ? failure.what() : render_task_failure(failure);
            }
            catch (const TaskCancelled&) {}
            catch (...) { message = "native task failed"; }
        }
        return message + cleanup_failure;
    }
    std::string failure_message_for_run() const override {
        const auto reply_failure = actor_reply_failure_message();
        std::lock_guard<std::mutex> lock(mutex);
        if (!reply_failure.empty()) return reply_failure + cleanup_failure;
        std::string message;
        if (error) {
            try { std::rethrow_exception(error); }
            catch (const Failure& failure) {
                message = has_child_failure() ? failure.what() : render_task_failure(failure);
            }
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
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
            return value;
        } catch (...) {
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
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
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
            return value;
        } catch (...) {
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
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
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
        } catch (...) {
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
            throw;
        }
    }
};
struct TaskGroupState {
    const std::uint64_t owner_identity;
    mutable std::mutex mutex;
    std::vector<std::weak_ptr<TaskControl>> members;
    explicit TaskGroupState(std::uint64_t owner) : owner_identity(owner) {}
};
inline bool& actor_handler_active();
struct TaskGroup {
    std::shared_ptr<TaskGroupState> state;
    void require_owner(const std::string& file, int line, int column) const {
        if (!state) throw Failure(file, line, column, "cannot use an empty TaskGroup");
        const auto current_owner = active_task_control ? active_task_control->identity : 0;
        if (actor_handler_active() || current_owner != state->owner_identity)
            throw Failure(file, line, column, "TaskGroup may only be used by its creating task");
    }
    template <typename T> bool add(const Task<T>& task, const std::string& file, int line, int column) const {
        require_owner(file, line, column);
        if (!task.state || task.state->owner_identity != state->owner_identity)
            throw Failure(file, line, column, "TaskGroup.add accepts only a task created by the group's owner");
        std::lock_guard<std::mutex> lock(state->mutex);
        for (const auto& existing : state->members) {
            if (auto member = existing.lock(); member && member == task.state) return false;
        }
        state->members.push_back(task.state);
        return true;
    }
    bool is_done(const std::string& file, int line, int column) const {
        require_owner(file, line, column);
        std::vector<std::weak_ptr<TaskControl>> members;
        { std::lock_guard<std::mutex> lock(state->mutex); members = state->members; }
        for (const auto& weak : members) {
            if (auto member = weak.lock(); member && !member->done.load(std::memory_order_acquire)) return false;
        }
        return true;
    }
    std::int64_t cancel(const std::string& file, int line, int column) const {
        require_owner(file, line, column);
        std::vector<std::weak_ptr<TaskControl>> members;
        { std::lock_guard<std::mutex> lock(state->mutex); members = state->members; }
        std::int64_t count = 0;
        for (const auto& weak : members) {
            if (auto member = weak.lock(); member && !member->done.load(std::memory_order_acquire)
                && !member->is_cancelled()) {
                member->request_cancel();
                ++count;
            }
        }
        return count;
    }
    void await(const std::string& file, int line, int column) const {
        require_owner(file, line, column);
        std::vector<std::shared_ptr<TaskControl>> members;
        {
            std::lock_guard<std::mutex> lock(state->mutex);
            for (const auto& weak : state->members) if (auto member = weak.lock()) members.push_back(std::move(member));
        }
        auto* execution_lock = active_execution_lock;
        const bool resume_serialized = execution_lock && execution_lock->owns_lock();
        if (resume_serialized) execution_lock->unlock();
        try {
            for (const auto& member : members) {
                if (!member->started.load(std::memory_order_acquire)) member->start();
                while (!member->done.load(std::memory_order_acquire)) {
                    std::unique_lock<std::mutex> lock(member->lifecycle_mutex);
                    member->lifecycle_changed.wait_for(lock, std::chrono::milliseconds(50), [&member]() {
                        return member->done.load(std::memory_order_acquire);
                    });
                    lock.unlock();
                    if (active_task_control) active_task_control->check_interruption();
                }
                if (active_task_control) active_task_control->check_interruption();
                const auto failure = member->failure_message_for_owner();
                if (!failure.empty()) throw Failure(file, line, column, failure);
            }
            if (active_task_control) active_task_control->check_interruption();
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
        } catch (...) {
            if (resume_serialized) reacquire_execution_lock(*execution_lock, active_task_control);
            throw;
        }
    }
    bool operator==(const TaskGroup& other) const noexcept { return state == other.state; }
    bool operator!=(const TaskGroup& other) const noexcept { return !(*this == other); }
};
inline TaskGroup task_group(const std::string& file, int line, int column) {
    if (actor_handler_active()) throw Failure(file, line, column, "task groups cannot be created inside actor message handlers");
    const auto owner = active_task_control ? active_task_control->identity : 0;
    return TaskGroup{std::make_shared<TaskGroupState>(owner)};
}

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
inline Task<void> sleep_for(Duration duration, const std::string& file, int line, int column) {
    auto operation = [duration]() {
        const auto deadline = duration_deadline(duration);
        auto* control = active_task_control;
        if (control) {
            control->wait_until(deadline, nullptr);
            control->check_interruption();
        } else {
            std::this_thread::sleep_until(deadline);
        }
    };
    // Timer waits run outside the serialized execution lock so unrelated
    // native tasks can continue while this task is suspended.
    return make_io_task<void>(std::move(operation), file, line, column);
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

enum class JsonKind { null_value, boolean, integer, floating, string, array, object };
struct JsonValue {
    JsonKind kind = JsonKind::null_value;
    bool boolean = false;
    std::int64_t integer = 0;
    double floating = 0.0;
    std::string text;
    std::vector<JsonValue> array;
    std::vector<std::pair<std::string, std::shared_ptr<JsonValue>>> object;
};

class SecretString final {
    std::string value_;
public:
    explicit SecretString(std::string value) : value_(std::move(value)) {}
    std::string reveal() const { return value_; }
};
inline std::optional<SecretString> environment_value(const std::string& name) {
    if (name.empty()) return std::nullopt;
    const auto valid_first = [](unsigned char ch) {
        return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || ch == '_';
    };
    const auto valid_rest = [&valid_first](unsigned char ch) {
        return valid_first(ch) || (ch >= '0' && ch <= '9');
    };
    if (!valid_first(static_cast<unsigned char>(name.front()))) return std::nullopt;
    for (std::size_t index = 1; index < name.size(); ++index) {
        if (!valid_rest(static_cast<unsigned char>(name[index]))) return std::nullopt;
    }
    const char* value = std::getenv(name.c_str());
    if (!value) return std::nullopt;
    return SecretString(std::string(value));
}

template <typename T> inline bool equal(const T& left, const T& right);
inline bool equal(const JsonValue& left, const JsonValue& right);
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

inline bool equal(const JsonValue& left, const JsonValue& right) {
    std::vector<std::pair<const JsonValue*, const JsonValue*>> pending{{&left, &right}};
    while (!pending.empty()) {
        const auto current = pending.back();
        pending.pop_back();
        const JsonValue& a = *current.first;
        const JsonValue& b = *current.second;
        if (a.kind != b.kind) return false;
        switch (a.kind) {
            case JsonKind::null_value: break;
            case JsonKind::boolean: if (a.boolean != b.boolean) return false; break;
            case JsonKind::integer: if (a.integer != b.integer) return false; break;
            case JsonKind::floating: if (a.floating != b.floating) return false; break;
            case JsonKind::string: if (a.text != b.text) return false; break;
            case JsonKind::array:
                if (a.array.size() != b.array.size()) return false;
                for (std::size_t index = 0; index < a.array.size(); ++index)
                    pending.emplace_back(&a.array[index], &b.array[index]);
                break;
            case JsonKind::object:
                if (a.object.size() != b.object.size()) return false;
                {
                std::map<std::string, const JsonValue*> right_members;
                for (const auto& entry : b.object) {
                    if (!entry.second || !right_members.emplace(entry.first, entry.second.get()).second) return false;
                }
                for (const auto& entry : a.object) {
                    const auto found = right_members.find(entry.first);
                    if (!entry.second || found == right_members.end()) return false;
                    pending.emplace_back(entry.second.get(), found->second);
                }
                }
                break;
        }
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

struct ReplyCellBase {
    virtual ~ReplyCellBase() = default;
    virtual void fail(const std::string& message) noexcept = 0;
    virtual bool is_pending() = 0;
};
enum class ReplyState { pending, sent, failed, cancelled, timed_out, consumed };
template <typename T> struct ReplyCell final : ReplyCellBase {
    mutable std::mutex mutex;
    ReplyState state = ReplyState::pending;
    std::optional<T> value;
    std::string error;
    std::chrono::steady_clock::time_point started_at = std::chrono::steady_clock::now();
    std::optional<std::int64_t> timeout_milliseconds;
    std::vector<TaskControl*> waiters;

    explicit ReplyCell(std::optional<std::int64_t> timeout) : timeout_milliseconds(timeout) {}
    void notify_waiters_locked() noexcept {
        for (auto* waiter : waiters) {
            if (!waiter) continue;
            try {
                // Reply state is protected by this cell mutex. Pair each notification
                // with the waiter's lifecycle mutex so a reply cannot notify between
                // the predicate check and the condition-variable sleep.
                std::lock_guard<std::mutex> lock(waiter->lifecycle_mutex);
                // Once the cell leaves pending, this Task no longer blocks on an
                // unresolved actor reply. Clear the scheduler hint before waking it,
                // so runTasks cannot mistake a ready reply for a deadlock.
                waiter->waiting_for_actor_reply.store(false, std::memory_order_release);
                waiter->actor_reply_wait_has_deadline.store(false, std::memory_order_release);
                waiter->lifecycle_changed.notify_all();
            } catch (...) {}
        }
        notify_task_scheduler();
    }
    void refresh_locked() {
        if (state != ReplyState::pending || !timeout_milliseconds) return;
        const auto elapsed = std::chrono::duration<long double, std::milli>(
            std::chrono::steady_clock::now() - started_at).count();
        if (elapsed >= static_cast<long double>(*timeout_milliseconds)) {
            state = ReplyState::timed_out;
            error = "request timed out";
            notify_waiters_locked();
        }
    }
    void fail(const std::string& message) noexcept override {
        std::lock_guard<std::mutex> lock(mutex);
        try {
            refresh_locked();
            if (state != ReplyState::pending) return;
            state = ReplyState::failed;
            error = message;
            notify_waiters_locked();
        } catch (...) {
            state = ReplyState::failed;
            error.clear();
            notify_waiters_locked();
        }
    }
    bool is_pending() override {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        return state == ReplyState::pending;
    }
    bool send(T next) {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        if (state != ReplyState::pending) return false;
        value.emplace(std::move(next));
        state = ReplyState::sent;
        notify_waiters_locked();
        return true;
    }
    bool is_ready() {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        return state != ReplyState::pending;
    }
    bool is_cancelled() {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        return state == ReplyState::cancelled;
    }
    bool is_timed_out() {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        return state == ReplyState::timed_out;
    }
    bool cancel() {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        if (state != ReplyState::pending) return false;
        state = ReplyState::cancelled;
        notify_waiters_locked();
        return true;
    }
    StringResult<std::optional<T>> try_receive() {
        std::lock_guard<std::mutex> lock(mutex);
        refresh_locked();
        if (state == ReplyState::sent) {
            std::optional<T> received{std::move(*value)};
            value.reset();
            state = ReplyState::consumed;
            notify_waiters_locked();
            return result_ok<std::optional<T>>(std::move(received));
        }
        if (state == ReplyState::failed || state == ReplyState::timed_out) {
            return result_error<std::optional<T>>(error.empty() ? "actor failed before sending a reply" : error);
        }
        return result_ok<std::optional<T>>(std::nullopt);
    }
};
template <typename T> struct ReplySender {
    std::shared_ptr<ReplyCell<T>> cell;
    bool send(T value) const { return cell && cell->send(std::move(value)); }
};
template <typename T> struct ReplyReceiver {
    std::shared_ptr<ReplyCell<T>> cell;
    bool is_ready() const { return cell && cell->is_ready(); }
    bool is_cancelled() const { return cell && cell->is_cancelled(); }
    bool is_timed_out() const { return cell && cell->is_timed_out(); }
    bool cancel() const { return cell && cell->cancel(); }
    StringResult<std::optional<T>> try_receive() const {
        return cell ? cell->try_receive() : result_error<std::optional<T>>("reply receiver is unavailable");
    }
};

template <typename T> inline std::optional<T> await_reply(
    const ReplyReceiver<T>& receiver, bool consume, const std::string& file, int line, int column) {
    const auto cell = receiver.cell;
    if (!cell) throw Failure(file, line, column, "reply receiver is unavailable");
    auto* waiter = active_task_control;
    if (!waiter) throw Failure(file, line, column, "awaiting a ReplyReceiver requires an async Task");

    auto* execution_lock = active_execution_lock;
    const bool resume_serialized_execution = execution_lock && execution_lock->owns_lock();
    if (resume_serialized_execution) execution_lock->unlock();

    bool registered = false;
    std::optional<T> received;
    std::string error;
    const auto unregister = [&]() {
        if (!registered) return;
        {
            std::lock_guard<std::mutex> lock(cell->mutex);
            const auto end = std::remove(cell->waiters.begin(), cell->waiters.end(), waiter);
            cell->waiters.erase(end, cell->waiters.end());
            waiter->waiting_for_actor_reply.store(false, std::memory_order_release);
            waiter->actor_reply_wait_has_deadline.store(false, std::memory_order_release);
        }
        registered = false;
        notify_task_scheduler();
    };
    try {
        waiter->check_interruption();
        {
            std::lock_guard<std::mutex> lock(cell->mutex);
            cell->refresh_locked();
            if (cell->state == ReplyState::pending) {
                cell->waiters.push_back(waiter);
                waiter->actor_reply_wait_has_deadline.store(
                    cell->timeout_milliseconds.has_value(), std::memory_order_release);
                waiter->waiting_for_actor_reply.store(true, std::memory_order_release);
                registered = true;
            }
        }
        notify_task_scheduler();

        for (;;) {
            waiter->check_interruption();
            {
                // Lock in the same cell -> lifecycle order used by reply send/fail.
                // The cell lock guards the reply predicate; the lifecycle lock makes
                // condition-variable notification atomic with entering the wait.
                std::unique_lock<std::mutex> cell_lock(cell->mutex);
                cell->refresh_locked();
                if (cell->state == ReplyState::pending) {
                    std::unique_lock<std::mutex> lifecycle_lock(waiter->lifecycle_mutex);
                    if (!waiter->is_done()) {
                        std::optional<std::chrono::steady_clock::time_point> deadline;
                        if (cell->timeout_milliseconds) {
                            const auto maximum = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::time_point::max() - cell->started_at).count();
                            if (*cell->timeout_milliseconds > maximum)
                                deadline = std::chrono::steady_clock::time_point::max();
                            else
                                deadline = cell->started_at
                                    + std::chrono::milliseconds(*cell->timeout_milliseconds);
                        }
                        cell_lock.unlock();
                        if (deadline)
                            waiter->lifecycle_changed.wait_until(lifecycle_lock, *deadline);
                        else
                            waiter->lifecycle_changed.wait(lifecycle_lock);
                    }
                }
            }
            waiter->check_interruption();
            {
                std::lock_guard<std::mutex> lock(cell->mutex);
                cell->refresh_locked();
                if (cell->state == ReplyState::pending) continue;
                if (cell->state == ReplyState::sent) {
                    if (consume) {
                        if (!cell->value) error = "reply was already consumed";
                        else {
                            received.emplace(std::move(*cell->value));
                            cell->value.reset();
                            cell->state = ReplyState::consumed;
                            cell->notify_waiters_locked();
                        }
                    }
                    break;
                }
                if (!cell->error.empty()) error = cell->error;
                else if (cell->state == ReplyState::timed_out) error = "request timed out";
                else if (cell->state == ReplyState::consumed) error = "reply was already consumed";
                else error = "actor request did not complete";
                break;
            }
        }
    } catch (...) {
        unregister();
        if (resume_serialized_execution) reacquire_execution_lock(*execution_lock, waiter);
        throw;
    }
    unregister();
    if (resume_serialized_execution) reacquire_execution_lock(*execution_lock, waiter);
    if (!error.empty()) {
        waiter->record_actor_reply_failure(error);
        throw Failure(file, line, column, std::move(error));
    }
    return received;
}

inline bool valid_utf8_string(const std::string& value);
inline constexpr std::size_t MAX_JSON_DOCUMENT_BYTES = 16 * 1024 * 1024;
inline constexpr std::size_t MAX_JSON_NESTING = 128;

inline std::int64_t json_decimal_order(const char* first, const char* last) {
    const char* cursor = first;
    if (cursor != last && (*cursor == '-' || *cursor == '+')) ++cursor;
    const char* exponent_marker = last;
    for (const char* current = cursor; current != last; ++current) {
        if (*current == 'e' || *current == 'E') { exponent_marker = current; break; }
    }
    std::int64_t explicit_exponent = 0;
    if (exponent_marker != last) {
        const char* current = exponent_marker + 1;
        bool negative = false;
        if (current != last && (*current == '+' || *current == '-')) { negative = *current == '-'; ++current; }
        for (; current != last; ++current) {
            explicit_exponent = std::min<std::int64_t>(1000000000, explicit_exponent * 10 + (*current - '0'));
        }
        if (negative) explicit_exponent = -explicit_exponent;
    }
    std::int64_t integer_digits = 0;
    std::int64_t digit_index = 0;
    std::int64_t first_nonzero = -1;
    bool after_decimal = false;
    for (const char* current = cursor; current != exponent_marker; ++current) {
        if (*current == '.') { after_decimal = true; continue; }
        if (*current >= '0' && *current <= '9') {
            if (first_nonzero < 0 && *current != '0') first_nonzero = digit_index;
            if (!after_decimal) ++integer_digits;
            ++digit_index;
        }
    }
    if (first_nonzero < 0) return 0;
    return explicit_exponent + integer_digits - first_nonzero - 1;
}

class JsonParser {
public:
    explicit JsonParser(const std::string& input) : input_(input) {}

    std::optional<JsonValue> parse() {
        auto value = parse_value(0);
        if (!value) return std::nullopt;
        skip_space();
        if (position_ != input_.size()) return std::nullopt;
        return value;
    }

private:
    const std::string& input_;
    std::size_t position_ = 0;

    void skip_space() {
        while (position_ < input_.size()) {
            const char value = input_[position_];
            if (value != ' ' && value != '\t' && value != '\n' && value != '\r') break;
            ++position_;
        }
    }

    bool consume(const char* literal) {
        const std::string_view wanted(literal);
        if (input_.size() - position_ < wanted.size()
            || input_.compare(position_, wanted.size(), wanted.data(), wanted.size()) != 0) return false;
        position_ += wanted.size();
        return true;
    }

    static void append_codepoint(std::string& output, std::uint32_t value) {
        if (value <= 0x7f) output.push_back(static_cast<char>(value));
        else if (value <= 0x7ff) {
            output.push_back(static_cast<char>(0xc0 | (value >> 6)));
            output.push_back(static_cast<char>(0x80 | (value & 0x3f)));
        } else if (value <= 0xffff) {
            output.push_back(static_cast<char>(0xe0 | (value >> 12)));
            output.push_back(static_cast<char>(0x80 | ((value >> 6) & 0x3f)));
            output.push_back(static_cast<char>(0x80 | (value & 0x3f)));
        } else {
            output.push_back(static_cast<char>(0xf0 | (value >> 18)));
            output.push_back(static_cast<char>(0x80 | ((value >> 12) & 0x3f)));
            output.push_back(static_cast<char>(0x80 | ((value >> 6) & 0x3f)));
            output.push_back(static_cast<char>(0x80 | (value & 0x3f)));
        }
    }

    std::optional<std::uint16_t> read_hex4() {
        if (input_.size() - position_ < 4) return std::nullopt;
        std::uint16_t value = 0;
        for (int index = 0; index < 4; ++index) {
            const char digit = input_[position_++];
            value = static_cast<std::uint16_t>(value << 4);
            if (digit >= '0' && digit <= '9') value = static_cast<std::uint16_t>(value | (digit - '0'));
            else if (digit >= 'a' && digit <= 'f') value = static_cast<std::uint16_t>(value | (digit - 'a' + 10));
            else if (digit >= 'A' && digit <= 'F') value = static_cast<std::uint16_t>(value | (digit - 'A' + 10));
            else return std::nullopt;
        }
        return value;
    }

    std::optional<std::string> parse_string() {
        if (position_ >= input_.size() || input_[position_++] != '"') return std::nullopt;
        std::string result;
        while (position_ < input_.size()) {
            const unsigned char current = static_cast<unsigned char>(input_[position_++]);
            if (current == '"') {
                if (!valid_utf8_string(result)) return std::nullopt;
                return result;
            }
            if (current < 0x20) return std::nullopt;
            if (current != '\\') { result.push_back(static_cast<char>(current)); continue; }
            if (position_ >= input_.size()) return std::nullopt;
            const char escape = input_[position_++];
            switch (escape) {
                case '"': result.push_back('"'); break;
                case '\\': result.push_back('\\'); break;
                case '/': result.push_back('/'); break;
                case 'b': result.push_back('\b'); break;
                case 'f': result.push_back('\f'); break;
                case 'n': result.push_back('\n'); break;
                case 'r': result.push_back('\r'); break;
                case 't': result.push_back('\t'); break;
                case 'u': {
                    auto first = read_hex4();
                    if (!first) return std::nullopt;
                    std::uint32_t codepoint = *first;
                    if (codepoint >= 0xd800 && codepoint <= 0xdbff) {
                        if (input_.size() - position_ < 6 || input_[position_] != '\\'
                            || input_[position_ + 1] != 'u') return std::nullopt;
                        position_ += 2;
                        auto second = read_hex4();
                        if (!second || *second < 0xdc00 || *second > 0xdfff) return std::nullopt;
                        codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + (*second - 0xdc00);
                    } else if (codepoint >= 0xdc00 && codepoint <= 0xdfff) return std::nullopt;
                    append_codepoint(result, codepoint);
                    break;
                }
                default: return std::nullopt;
            }
        }
        return std::nullopt;
    }

    std::optional<JsonValue> parse_number() {
        const std::size_t start = position_;
        if (position_ < input_.size() && input_[position_] == '-') ++position_;
        if (position_ >= input_.size()) return std::nullopt;
        if (input_[position_] == '0') ++position_;
        else {
            if (input_[position_] < '1' || input_[position_] > '9') return std::nullopt;
            while (position_ < input_.size() && input_[position_] >= '0' && input_[position_] <= '9') ++position_;
        }
        bool is_float = false;
        if (position_ < input_.size() && input_[position_] == '.') {
            is_float = true;
            ++position_;
            const std::size_t digits = position_;
            while (position_ < input_.size() && input_[position_] >= '0' && input_[position_] <= '9') ++position_;
            if (position_ == digits) return std::nullopt;
        }
        if (position_ < input_.size() && (input_[position_] == 'e' || input_[position_] == 'E')) {
            is_float = true;
            ++position_;
            if (position_ < input_.size() && (input_[position_] == '+' || input_[position_] == '-')) ++position_;
            const std::size_t digits = position_;
            while (position_ < input_.size() && input_[position_] >= '0' && input_[position_] <= '9') ++position_;
            if (position_ == digits) return std::nullopt;
        }
        const char* first = input_.data() + start;
        const char* last = input_.data() + position_;
        JsonValue value;
        if (!is_float) {
            std::int64_t parsed = 0;
            const auto conversion = std::from_chars(first, last, parsed, 10);
            if (conversion.ec != std::errc{} || conversion.ptr != last) return std::nullopt;
            value.kind = JsonKind::integer;
            value.integer = parsed;
            return value;
        }
        double parsed = 0.0;
        const auto conversion = std::from_chars(first, last, parsed, std::chars_format::general);
        if (conversion.ec == std::errc{} && conversion.ptr == last && std::isfinite(parsed)) {
            value.kind = JsonKind::floating;
            value.floating = parsed;
            return value;
        }
        if (conversion.ec != std::errc::result_out_of_range || conversion.ptr != last) return std::nullopt;
        // Preserve the reference runtime's finite underflow-to-zero behavior.
        std::istringstream stream(std::string(first, last));
        stream.imbue(std::locale::classic());
        long double wide = 0.0L;
        stream >> wide;
        if (!stream || !stream.eof()) {
            if (json_decimal_order(first, last) < -324) {
                value.kind = JsonKind::floating;
                value.floating = first != last && *first == '-' ? -0.0 : 0.0;
                return value;
            }
            return std::nullopt;
        }
        parsed = static_cast<double>(wide);
        if (!std::isfinite(parsed)) return std::nullopt;
        value.kind = JsonKind::floating;
        value.floating = parsed;
        return value;
    }

    std::optional<JsonValue> parse_array(std::size_t depth) {
        if (depth > MAX_JSON_NESTING) return std::nullopt;
        JsonValue result;
        result.kind = JsonKind::array;
        ++position_;
        skip_space();
        if (position_ < input_.size() && input_[position_] == ']') { ++position_; return result; }
        while (true) {
            auto item = parse_value(depth);
            if (!item) return std::nullopt;
            result.array.push_back(std::move(*item));
            skip_space();
            if (position_ >= input_.size()) return std::nullopt;
            if (input_[position_] == ']') { ++position_; return result; }
            if (input_[position_++] != ',') return std::nullopt;
            skip_space();
        }
    }

    std::optional<JsonValue> parse_object(std::size_t depth) {
        if (depth > MAX_JSON_NESTING) return std::nullopt;
        JsonValue result;
        result.kind = JsonKind::object;
        std::set<std::string> keys;
        ++position_;
        skip_space();
        if (position_ < input_.size() && input_[position_] == '}') { ++position_; return result; }
        while (true) {
            auto key = parse_string();
            if (!key) return std::nullopt;
            if (!keys.insert(*key).second) return std::nullopt;
            skip_space();
            if (position_ >= input_.size() || input_[position_++] != ':') return std::nullopt;
            skip_space();
            auto item = parse_value(depth);
            if (!item) return std::nullopt;
            result.object.emplace_back(std::move(*key), std::make_shared<JsonValue>(std::move(*item)));
            skip_space();
            if (position_ >= input_.size()) return std::nullopt;
            if (input_[position_] == '}') { ++position_; return result; }
            if (input_[position_++] != ',') return std::nullopt;
            skip_space();
        }
    }

    std::optional<JsonValue> parse_value(std::size_t depth) {
        skip_space();
        if (position_ >= input_.size()) return std::nullopt;
        const char current = input_[position_];
        if (current == 'n' && consume("null")) return JsonValue{};
        if (current == 't' && consume("true")) { JsonValue value; value.kind = JsonKind::boolean; value.boolean = true; return value; }
        if (current == 'f' && consume("false")) { JsonValue value; value.kind = JsonKind::boolean; value.boolean = false; return value; }
        if (current == '"') {
            auto text = parse_string();
            if (!text) return std::nullopt;
            JsonValue value; value.kind = JsonKind::string; value.text = std::move(*text); return value;
        }
        if (current == '[') return parse_array(depth + 1);
        if (current == '{') return parse_object(depth + 1);
        if (current == '-' || (current >= '0' && current <= '9')) return parse_number();
        return std::nullopt;
    }
};

inline JsonValue json_string(std::string value) {
    JsonValue result; result.kind = JsonKind::string; result.text = std::move(value); return result;
}
inline JsonValue json_int(std::int64_t value) {
    JsonValue result; result.kind = JsonKind::integer; result.integer = value; return result;
}
inline JsonValue json_float(double value) {
    JsonValue result; result.kind = JsonKind::floating; result.floating = value; return result;
}
inline JsonValue json_bool(bool value) {
    JsonValue result; result.kind = JsonKind::boolean; result.boolean = value; return result;
}
inline JsonValue json_null() { return JsonValue{}; }
inline JsonValue json_array(const std::shared_ptr<ListData<JsonValue>>& values) {
    JsonValue result; result.kind = JsonKind::array;
    if (values) result.array = values->values;
    return result;
}
inline JsonValue json_object(const std::shared_ptr<MapData<std::string, JsonValue>>& values) {
    JsonValue result; result.kind = JsonKind::object;
    if (values) {
        result.object.reserve(values->entries.size());
        for (const auto& entry : values->entries)
            result.object.emplace_back(entry.first, std::make_shared<JsonValue>(entry.second));
    }
    return result;
}

inline StringResult<JsonValue> parse_json(const std::string& text) {
    if (text.size() > MAX_JSON_DOCUMENT_BYTES || !valid_utf8_string(text))
        return result_error<JsonValue>("invalid JSON document");
    JsonParser parser(text);
    auto parsed = parser.parse();
    if (!parsed) return result_error<JsonValue>("invalid JSON document");
    return result_ok<JsonValue>(std::move(*parsed));
}

inline const char* json_kind(const JsonValue& value) {
    switch (value.kind) {
        case JsonKind::null_value: return "null";
        case JsonKind::boolean: return "bool";
        case JsonKind::integer: return "int";
        case JsonKind::floating: return "float";
        case JsonKind::string: return "string";
        case JsonKind::array: return "array";
        case JsonKind::object: return "object";
    }
    return "null";
}
inline bool json_is_null(const JsonValue& value) { return value.kind == JsonKind::null_value; }
inline std::optional<std::string> json_as_string(const JsonValue& value) {
    return value.kind == JsonKind::string ? std::optional<std::string>{value.text} : std::nullopt;
}
inline std::optional<std::int64_t> json_as_int(const JsonValue& value) {
    return value.kind == JsonKind::integer ? std::optional<std::int64_t>{value.integer} : std::nullopt;
}
inline std::optional<double> json_as_float(const JsonValue& value) {
    return value.kind == JsonKind::floating ? std::optional<double>{value.floating} : std::nullopt;
}
inline std::optional<bool> json_as_bool(const JsonValue& value) {
    return value.kind == JsonKind::boolean ? std::optional<bool>{value.boolean} : std::nullopt;
}
inline std::optional<std::int64_t> json_array_length(const JsonValue& value) {
    if (value.kind != JsonKind::array || value.array.size() > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return std::nullopt;
    return static_cast<std::int64_t>(value.array.size());
}
inline std::optional<std::int64_t> json_object_length(const JsonValue& value) {
    if (value.kind != JsonKind::object || value.object.size() > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return std::nullopt;
    return static_cast<std::int64_t>(value.object.size());
}
inline std::optional<JsonValue> json_array_at(const JsonValue& value, std::int64_t index) {
    if (value.kind != JsonKind::array || index < 0 || static_cast<std::uint64_t>(index) >= value.array.size()) return std::nullopt;
    return value.array[static_cast<std::size_t>(index)];
}
inline std::optional<JsonValue> json_object_get(const JsonValue& value, const std::string& key) {
    if (value.kind != JsonKind::object) return std::nullopt;
    const auto found = std::find_if(value.object.begin(), value.object.end(), [&key](const auto& entry) {
        return entry.first == key;
    });
    return found == value.object.end() || !found->second ? std::nullopt : std::optional<JsonValue>{*found->second};
}
inline std::optional<std::shared_ptr<ListData<std::string>>> json_object_keys(const JsonValue& value) {
    if (value.kind != JsonKind::object) return std::nullopt;
    auto keys = make_list<std::string>();
    keys->values.reserve(value.object.size());
    for (const auto& entry : value.object) keys->values.push_back(entry.first);
    return keys;
}

inline bool json_append(std::string& output, const std::string& value) {
    if (value.size() > MAX_JSON_DOCUMENT_BYTES - output.size()) return false;
    output.append(value);
    return true;
}
inline bool json_append_char(std::string& output, char value) {
    if (output.size() >= MAX_JSON_DOCUMENT_BYTES) return false;
    output.push_back(value);
    return true;
}
inline bool json_write_string(const std::string& value, std::string& output) {
    if (!valid_utf8_string(value) || !json_append_char(output, '"')) return false;
    static constexpr char hex[] = "0123456789abcdef";
    for (unsigned char current : value) {
        switch (current) {
            case '"': if (!json_append(output, "\\\"")) return false; break;
            case '\\': if (!json_append(output, "\\\\")) return false; break;
            case '\b': if (!json_append(output, "\\b")) return false; break;
            case '\f': if (!json_append(output, "\\f")) return false; break;
            case '\n': if (!json_append(output, "\\n")) return false; break;
            case '\r': if (!json_append(output, "\\r")) return false; break;
            case '\t': if (!json_append(output, "\\t")) return false; break;
            default:
                if (current < 0x20) {
                    char escape[6] = {'\\', 'u', '0', '0', hex[current >> 4], hex[current & 0x0f]};
                    if (MAX_JSON_DOCUMENT_BYTES - output.size() < sizeof(escape)) return false;
                    output.append(escape, sizeof(escape));
                } else if (!json_append_char(output, static_cast<char>(current))) return false;
        }
    }
    return json_append_char(output, '"');
}
inline std::optional<std::string> json_format_float(double value) {
    if (!std::isfinite(value)) return std::nullopt;
    std::array<char, 128> buffer{};
    const auto converted = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value, std::chars_format::general);
    if (converted.ec != std::errc{}) return std::nullopt;
    std::string raw(buffer.data(), converted.ptr);
    const std::size_t exponent_marker = raw.find_first_of("eE");
    if (exponent_marker == std::string::npos) {
        if (raw.find('.') == std::string::npos) raw += ".0";
        return raw;
    }
    int exponent = 0;
    const char* exponent_first = raw.data() + exponent_marker + 1;
    const char* exponent_last = raw.data() + raw.size();
    bool exponent_negative = false;
    if (exponent_first != exponent_last && (*exponent_first == '+' || *exponent_first == '-')) {
        exponent_negative = *exponent_first == '-';
        ++exponent_first;
    }
    const auto exponent_result = std::from_chars(exponent_first, exponent_last, exponent);
    if (exponent_result.ec != std::errc{} || exponent_result.ptr != exponent_last) return std::nullopt;
    if (exponent_negative) exponent = -exponent;
    std::string mantissa = raw.substr(0, exponent_marker);
    if (exponent >= -4 && exponent < 16) {
        const bool negative = !mantissa.empty() && mantissa.front() == '-';
        if (negative) mantissa.erase(mantissa.begin());
        const std::size_t point = mantissa.find('.');
        const std::size_t digits_before_point = point == std::string::npos ? mantissa.size() : point;
        std::string digits = mantissa;
        if (point != std::string::npos) digits.erase(point, 1);
        const auto decimal_point = static_cast<std::int64_t>(digits_before_point) + exponent;
        std::string result = negative ? "-" : "";
        if (decimal_point <= 0) result += "0." + std::string(static_cast<std::size_t>(-decimal_point), '0') + digits;
        else if (static_cast<std::uint64_t>(decimal_point) >= digits.size())
            result += digits + std::string(static_cast<std::size_t>(decimal_point) - digits.size(), '0') + ".0";
        else result += digits.substr(0, static_cast<std::size_t>(decimal_point)) + "."
            + digits.substr(static_cast<std::size_t>(decimal_point));
        return result;
    }
    std::string exponent_text = std::to_string(exponent);
    const bool negative_exponent = exponent < 0;
    const unsigned int magnitude = negative_exponent
        ? static_cast<unsigned int>(-(static_cast<std::int64_t>(exponent)))
        : static_cast<unsigned int>(exponent);
    exponent_text = std::string(negative_exponent ? "-" : "+")
        + (magnitude < 10 ? "0" : "") + std::to_string(magnitude);
    return mantissa + "e" + exponent_text;
}
inline bool json_write_value(const JsonValue& value, std::string& output, std::size_t depth) {
    switch (value.kind) {
        case JsonKind::null_value: return json_append(output, "null");
        case JsonKind::boolean: return json_append(output, value.boolean ? "true" : "false");
        case JsonKind::integer: {
            const std::string text = std::to_string(value.integer);
            return json_append(output, text);
        }
        case JsonKind::floating: {
            auto text = json_format_float(value.floating);
            return text && json_append(output, *text);
        }
        case JsonKind::string: return json_write_string(value.text, output);
        case JsonKind::array:
            if (depth >= MAX_JSON_NESTING || !json_append_char(output, '[')) return false;
            for (std::size_t index = 0; index < value.array.size(); ++index) {
                if (index && !json_append_char(output, ',')) return false;
                if (!json_write_value(value.array[index], output, depth + 1)) return false;
            }
            return json_append_char(output, ']');
        case JsonKind::object:
            if (depth >= MAX_JSON_NESTING || !json_append_char(output, '{')) return false;
            for (std::size_t index = 0; index < value.object.size(); ++index) {
                const auto& entry = value.object[index];
                if (!entry.second || (index && !json_append_char(output, ','))
                    || !json_write_string(entry.first, output) || !json_append_char(output, ':')
                    || !json_write_value(*entry.second, output, depth + 1)) return false;
            }
            return json_append_char(output, '}');
    }
    return false;
}
inline StringResult<std::string> json_stringify(const JsonValue& value) {
    std::string output;
    if (!json_write_value(value, output, 0)) return result_error<std::string>("JSON encoding failed");
    return result_ok<std::string>(std::move(output));
}

enum class RestartStrategy { one_for_one, one_for_all, rest_for_one };
enum class RestartPolicy { permanent, transient, temporary };
struct SupervisorBackoff {
    std::int64_t base_milliseconds = 0;
    std::int64_t maximum_milliseconds = 0;
};
struct Supervisor;
struct ActorBase : std::enable_shared_from_this<ActorBase> {
    std::string actor_name;
    std::string status = "starting";
    std::deque<std::function<void()>> mailbox;
    std::vector<std::weak_ptr<ReplyCellBase>> pending_replies;
    std::vector<std::weak_ptr<ReplyCell<std::string>>> pending_monitors;
    std::vector<std::weak_ptr<ActorBase>> links;
    std::string last_exit_reason;
    bool queued = false;
    std::function<void()> tyv_restart;
    std::weak_ptr<Supervisor> supervisor;
    RestartPolicy restart_policy = RestartPolicy::permanent;
    std::mutex owned_tasks_mutex;
    std::vector<std::weak_ptr<TaskControl>> owned_tasks;
    virtual void tyv_clear_state() noexcept {}
    virtual ~ActorBase() = default;
};
struct SupervisorChild {
    std::shared_ptr<ActorBase> actor;
    std::shared_ptr<Supervisor> supervisor;
    RestartPolicy policy = RestartPolicy::permanent;
};
struct Supervisor : std::enable_shared_from_this<Supervisor> {
    RestartStrategy strategy = RestartStrategy::one_for_one;
    std::int64_t max_restarts = 0;
    std::int64_t window_milliseconds = 0;
    std::optional<SupervisorBackoff> backoff;
    std::string status = "alive";
    std::weak_ptr<Supervisor> parent;
    RestartPolicy restart_policy = RestartPolicy::permanent;
    std::vector<SupervisorChild> children;
    std::deque<std::chrono::steady_clock::time_point> restart_times;
};
struct PendingSupervisorRestart {
    std::shared_ptr<Supervisor> owner;
    std::vector<SupervisorChild> children;
    std::shared_ptr<ActorBase> trigger_actor;
    std::shared_ptr<Supervisor> trigger_supervisor;
    std::string failure;
    std::string file;
    int line = 1;
    int column = 1;
    std::chrono::steady_clock::time_point due;
};
struct ActorRuntimeState {
    std::mutex mutex;
    std::recursive_mutex supervision_mutex;
    std::condition_variable changed;
    std::deque<std::shared_ptr<ActorBase>> ready;
    std::vector<std::shared_ptr<ActorBase>> actors;
    std::vector<std::shared_ptr<Supervisor>> supervisors;
    std::vector<PendingSupervisorRestart> pending_restarts;
};
inline ActorRuntimeState& actor_runtime_state() {
    static ActorRuntimeState state;
    return state;
}
inline constexpr std::size_t MAX_ACTOR_MONITORS = 4096;
inline constexpr std::size_t MAX_ACTOR_LINKS = 4096;
inline std::vector<std::shared_ptr<ActorBase>> detach_actor_links_locked(
    const std::shared_ptr<ActorBase>& actor) {
    std::vector<std::shared_ptr<ActorBase>> peers;
    if (!actor) return peers;
    for (const auto& weak_peer : actor->links) {
        const auto peer = weak_peer.lock();
        if (!peer || peer == actor) continue;
        peers.push_back(peer);
        auto& reciprocal = peer->links;
        reciprocal.erase(std::remove_if(reciprocal.begin(), reciprocal.end(),
            [&actor](const auto& candidate) {
                const auto linked = candidate.lock();
                return !linked || linked == actor;
            }), reciprocal.end());
    }
    actor->links.clear();
    return peers;
}
inline void unlink_actor_links_locked(const std::shared_ptr<ActorBase>& actor) noexcept {
    if (!actor) return;
    for (const auto& weak_peer : actor->links) {
        const auto peer = weak_peer.lock();
        if (!peer || peer == actor) continue;
        auto& reciprocal = peer->links;
        reciprocal.erase(std::remove_if(reciprocal.begin(), reciprocal.end(),
            [&actor](const auto& candidate) {
                const auto linked = candidate.lock();
                return !linked || linked == actor;
            }), reciprocal.end());
    }
    actor->links.clear();
}
inline bool actor_link(
    const std::shared_ptr<ActorBase>& actor, const std::shared_ptr<ActorBase>& peer) {
    if (!actor || !peer || actor == peer) return false;
    auto& state = actor_runtime_state();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (actor->status != "alive" || peer->status != "alive") return false;
    auto has_link = [](const auto& source, const auto& target) {
        return std::any_of(source->links.begin(), source->links.end(),
            [&target](const auto& weak) { return weak.lock() == target; });
    };
    const bool already_linked = has_link(actor, peer) || has_link(peer, actor);
    if (already_linked) {
        try {
            if (!has_link(actor, peer)) actor->links.emplace_back(peer);
            if (!has_link(peer, actor)) peer->links.emplace_back(actor);
        } catch (...) {
            return false;
        }
        return true;
    }
    if (actor->links.size() >= MAX_ACTOR_LINKS || peer->links.size() >= MAX_ACTOR_LINKS) return false;
    try {
        actor->links.emplace_back(peer);
        peer->links.emplace_back(actor);
    } catch (...) {
        actor->links.erase(std::remove_if(actor->links.begin(), actor->links.end(),
            [&peer](const auto& weak) { return weak.lock() == peer; }), actor->links.end());
        peer->links.erase(std::remove_if(peer->links.begin(), peer->links.end(),
            [&actor](const auto& weak) { return weak.lock() == actor; }), peer->links.end());
        return false;
    }
    return true;
}
inline bool actor_unlink(
    const std::shared_ptr<ActorBase>& actor, const std::shared_ptr<ActorBase>& peer) {
    if (!actor || !peer || actor == peer) return false;
    auto& state = actor_runtime_state();
    std::lock_guard<std::mutex> lock(state.mutex);
    const bool existed = std::any_of(actor->links.begin(), actor->links.end(),
        [&peer](const auto& weak) { return weak.lock() == peer; })
        || std::any_of(peer->links.begin(), peer->links.end(),
            [&actor](const auto& weak) { return weak.lock() == actor; });
    actor->links.erase(std::remove_if(actor->links.begin(), actor->links.end(),
        [&peer](const auto& weak) { const auto linked = weak.lock(); return !linked || linked == peer; }), actor->links.end());
    peer->links.erase(std::remove_if(peer->links.begin(), peer->links.end(),
        [&actor](const auto& weak) { const auto linked = weak.lock(); return !linked || linked == actor; }), peer->links.end());
    return existed;
}
inline void notify_actor_monitors(
    const std::shared_ptr<ActorBase>& actor, const std::string& reason) noexcept {
    if (!actor) return;
    try {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->last_exit_reason = reason;
        for (const auto& pending : actor->pending_monitors)
            if (const auto cell = pending.lock()) cell->send(reason);
        actor->pending_monitors.clear();
    } catch (...) {}
}
inline ReplyReceiver<std::string> actor_monitor(
    const std::shared_ptr<ActorBase>& actor, const std::string& file, int line, int column) {
    auto cell = std::make_shared<ReplyCell<std::string>>(std::nullopt);
    if (!actor) {
        cell->send("actor reference unavailable");
        return ReplyReceiver<std::string>{std::move(cell)};
    }
    std::string terminal_reason;
    {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        if (actor->status == "alive" || actor->status == "starting" || actor->status == "restarting") {
            auto& monitors = actor->pending_monitors;
            monitors.erase(std::remove_if(monitors.begin(), monitors.end(), [](const auto& pending) {
                const auto current = pending.lock();
                return !current || !current->is_pending();
            }), monitors.end());
            if (monitors.size() >= MAX_ACTOR_MONITORS)
                fail(file, line, column, "actor monitor capacity (maximum 4096) exceeded");
            monitors.emplace_back(cell);
        } else {
            terminal_reason = actor->last_exit_reason.empty() ? actor->status : actor->last_exit_reason;
        }
    }
    if (!terminal_reason.empty()) cell->send(std::move(terminal_reason));
    return ReplyReceiver<std::string>{std::move(cell)};
}
inline bool& actor_handler_active() {
    static thread_local bool active = false;
    return active;
}
inline std::weak_ptr<ActorBase>& active_actor_context() {
    static thread_local std::weak_ptr<ActorBase> actor;
    return actor;
}
struct ActorHandlerScope {
    bool previous = actor_handler_active();
    std::weak_ptr<ActorBase> previous_actor = active_actor_context();
    explicit ActorHandlerScope(const std::shared_ptr<ActorBase>& actor) {
        actor_handler_active() = true;
        active_actor_context() = actor;
    }
    ~ActorHandlerScope() {
        active_actor_context() = previous_actor;
        actor_handler_active() = previous;
    }
};
inline constexpr std::size_t MAX_ACTOR_OWNED_TASKS = 4096;
inline void attach_task_to_current_actor(
    const std::shared_ptr<TaskControl>& task, const std::string& file, int line, int column) {
    if (!actor_handler_active()) return;
    const auto actor = active_actor_context().lock();
    if (!actor) return;
    bool cancel_now = false;
    try {
        auto& state = actor_runtime_state();
        {
            std::lock_guard<std::mutex> state_lock(state.mutex);
            if (actor->status != "alive" && actor->status != "starting"
                && actor->status != "restarting") {
                cancel_now = true;
            } else {
                std::lock_guard<std::mutex> tasks_lock(actor->owned_tasks_mutex);
                auto& tasks = actor->owned_tasks;
                tasks.erase(std::remove_if(tasks.begin(), tasks.end(), [](const auto& weak) {
                    const auto current = weak.lock();
                    return !current || current->done.load(std::memory_order_acquire);
                }), tasks.end());
                if (tasks.size() >= MAX_ACTOR_OWNED_TASKS)
                    throw Failure(file, line, column, "actor-owned task capacity exceeded (maximum 4096)");
                tasks.emplace_back(task);
            }
        }
    } catch (const Failure&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw Failure(file, line, column, "cannot register actor-owned task");
    } catch (const std::length_error&) {
        throw Failure(file, line, column, "cannot register actor-owned task");
    }
    if (cancel_now) task->request_cancel();
}
inline bool task_tree_contains_active(const TaskControl& task) noexcept {
    if (&task == active_task_control) return true;
    try {
        std::lock_guard<std::mutex> lock(task.children_mutex);
        for (const auto& child : task.children)
            if (child && task_tree_contains_active(*child)) return true;
    } catch (...) {}
    return false;
}
inline void wait_for_actor_task(const std::shared_ptr<TaskControl>& task) noexcept {
    if (!task || task_tree_contains_active(*task)) return;
    try {
        if (!task->done.load(std::memory_order_acquire)
            && !task->started.load(std::memory_order_acquire))
            task->start();
        std::unique_lock<std::mutex> lock(task->lifecycle_mutex);
        task->lifecycle_changed.wait(lock, [&task]() {
            return task->done.load(std::memory_order_acquire);
        });
    } catch (...) {}
}
inline void cancel_actor_tasks(const std::shared_ptr<ActorBase>& actor) noexcept {
    if (!actor) return;
    std::vector<std::weak_ptr<TaskControl>> tasks;
    try {
        std::lock_guard<std::mutex> lock(actor->owned_tasks_mutex);
        tasks.swap(actor->owned_tasks);
    } catch (...) { return; }
    for (auto task = tasks.rbegin(); task != tasks.rend(); ++task) {
        try {
            if (const auto owned = task->lock()) owned->request_cancel();
        } catch (...) {}
    }
    for (auto task = tasks.rbegin(); task != tasks.rend(); ++task) {
        try {
            if (const auto owned = task->lock()) wait_for_actor_task(owned);
        } catch (...) {}
    }
    notify_task_scheduler();
}
inline constexpr std::size_t MAX_ACTOR_MAILBOX = 100000;
inline constexpr std::int64_t MAX_ACTOR_DELIVERIES = 100000;
inline void fail_actor_replies(const std::shared_ptr<ActorBase>& actor, const std::string& message) noexcept {
    if (!actor) return;
    for (const auto& pending : actor->pending_replies)
        if (const auto cell = pending.lock()) cell->fail(message);
    actor->pending_replies.clear();
}
inline void remove_ready_actor_locked(ActorRuntimeState& state, const ActorBase* actor) {
    state.ready.erase(std::remove_if(state.ready.begin(), state.ready.end(), [actor](const auto& candidate) {
        return candidate.get() == actor;
    }), state.ready.end());
}
inline SupervisorBackoff exponential_backoff(
    std::int64_t base, std::int64_t maximum, const std::string& file, int line, int column) {
    if (base <= 0 || maximum < base)
        fail(file, line, column, "exponentialBackoff requires 0 < baseMilliseconds <= maximumMilliseconds");
    return SupervisorBackoff{base, maximum};
}
inline RestartPolicy permanent_child_policy() noexcept { return RestartPolicy::permanent; }
inline RestartPolicy transient_child_policy() noexcept { return RestartPolicy::transient; }
inline RestartPolicy temporary_child_policy() noexcept { return RestartPolicy::temporary; }
inline std::shared_ptr<Supervisor> supervisor_create(
    RestartStrategy strategy, std::int64_t max_restarts, std::int64_t window_milliseconds,
    std::optional<SupervisorBackoff> backoff, const std::string& file, int line, int column) {
    if (max_restarts < 0 || max_restarts > 1000)
        fail(file, line, column, "supervisor maxRestarts must be between zero and 1000");
    if (window_milliseconds <= 0)
        fail(file, line, column, "supervisor windowMilliseconds must be greater than zero");
    try {
        auto supervisor = std::make_shared<Supervisor>();
        supervisor->strategy = strategy;
        supervisor->max_restarts = max_restarts;
        supervisor->window_milliseconds = window_milliseconds;
        supervisor->backoff = backoff;
        auto& state = actor_runtime_state();
        std::lock_guard<std::recursive_mutex> supervision_lock(state.supervision_mutex);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.supervisors.push_back(supervisor);
        return supervisor;
    } catch (const std::bad_alloc&) {
        fail(file, line, column, "cannot allocate Supervisor state");
    }
    return {};
}
inline std::shared_ptr<Supervisor> supervisor_one_for_one(
    std::int64_t maximum, std::int64_t window, const std::string& file, int line, int column) {
    return supervisor_create(RestartStrategy::one_for_one, maximum, window, {}, file, line, column);
}
inline std::shared_ptr<Supervisor> supervisor_one_for_one(
    std::int64_t maximum, std::int64_t window, SupervisorBackoff backoff,
    const std::string& file, int line, int column) {
    return supervisor_create(RestartStrategy::one_for_one, maximum, window, backoff, file, line, column);
}
inline std::shared_ptr<Supervisor> supervisor_one_for_all(
    std::int64_t maximum, std::int64_t window, const std::string& file, int line, int column) {
    return supervisor_create(RestartStrategy::one_for_all, maximum, window, {}, file, line, column);
}
inline std::shared_ptr<Supervisor> supervisor_one_for_all(
    std::int64_t maximum, std::int64_t window, SupervisorBackoff backoff,
    const std::string& file, int line, int column) {
    return supervisor_create(RestartStrategy::one_for_all, maximum, window, backoff, file, line, column);
}
inline std::shared_ptr<Supervisor> supervisor_rest_for_one(
    std::int64_t maximum, std::int64_t window, const std::string& file, int line, int column) {
    return supervisor_create(RestartStrategy::rest_for_one, maximum, window, {}, file, line, column);
}
inline std::shared_ptr<Supervisor> supervisor_rest_for_one(
    std::int64_t maximum, std::int64_t window, SupervisorBackoff backoff,
    const std::string& file, int line, int column) {
    return supervisor_create(RestartStrategy::rest_for_one, maximum, window, backoff, file, line, column);
}
inline bool supervisor_is_alive(const std::shared_ptr<Supervisor>& supervisor) noexcept {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    return supervisor && supervisor->status == "alive";
}
inline void stop_actor_for_supervisor(const std::shared_ptr<ActorBase>& actor) noexcept {
    if (!actor) return;
    {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->status = "stopped";
        actor->mailbox.clear();
        actor->queued = false;
        remove_ready_actor_locked(state, actor.get());
        unlink_actor_links_locked(actor);
    }
    cancel_actor_tasks(actor);
    fail_actor_replies(actor, "actor stopped before replying");
    notify_actor_monitors(actor, "normal stop");
    actor->tyv_clear_state();
    actor_runtime_state().changed.notify_all();
}
inline void stop_supervisor_children(const std::shared_ptr<Supervisor>& supervisor) noexcept;
inline void stop_supervisor_tree(const std::shared_ptr<Supervisor>& supervisor) noexcept {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (!supervisor || supervisor->status == "stopped") return;
    state.pending_restarts.erase(std::remove_if(
        state.pending_restarts.begin(), state.pending_restarts.end(),
        [&supervisor](const PendingSupervisorRestart& pending) { return pending.owner == supervisor; }),
        state.pending_restarts.end());
    stop_supervisor_children(supervisor);
    supervisor->status = "stopped";
}
inline void stop_supervisor_children(const std::shared_ptr<Supervisor>& supervisor) noexcept {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (!supervisor) return;
    for (auto child = supervisor->children.rbegin(); child != supervisor->children.rend(); ++child) {
        if (child->actor) stop_actor_for_supervisor(child->actor);
        else stop_supervisor_tree(child->supervisor);
    }
}
inline std::string restart_actor_for_supervisor(
    const std::shared_ptr<ActorBase>& actor, const std::string& file, int line, int column) {
    auto& actor_state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> supervision_lock(actor_state.supervision_mutex);
    if (!actor || !actor->tyv_restart)
        return "native supervised actor has no restart initializer";
    {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->status = "restarting";
        actor->mailbox.clear();
        actor->queued = false;
        remove_ready_actor_locked(state, actor.get());
    }
    cancel_actor_tasks(actor);
    fail_actor_replies(actor, "actor restarted before replying");
    notify_actor_monitors(actor, "actor restarted");
    actor->tyv_clear_state();
    try {
        check_shutdown(file, line, column);
        actor->tyv_restart();
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->status = "alive";
        actor->last_exit_reason.clear();
        return {};
    } catch (const SignalShutdown&) {
        throw;
    } catch (const Failure& error) {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->status = "crashed";
        return "actor '" + actor->actor_name + "' crashed at " + error.file + ":" +
            std::to_string(error.line) + ":" + std::to_string(error.column) + ": " + error.message;
    } catch (...) {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->status = "crashed";
        return "actor '" + actor->actor_name + "' crashed: native runtime failure";
    }
}
inline std::string restart_supervisor_tree(
    const std::shared_ptr<Supervisor>& supervisor, const std::string& file, int line, int column) {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (!supervisor) return "native nested Supervisor is unavailable";
    supervisor->status = "alive";
    supervisor->restart_times.clear();
    stop_supervisor_children(supervisor);
    for (const auto& child : supervisor->children) {
        if (child.policy == RestartPolicy::temporary) continue;
        std::string failure;
        if (child.actor) failure = restart_actor_for_supervisor(child.actor, file, line, column);
        else if (child.supervisor) failure = restart_supervisor_tree(child.supervisor, file, line, column);
        if (!failure.empty()) return failure;
    }
    return {};
}
inline std::string supervisor_handle_failure(
    const std::shared_ptr<Supervisor>& supervisor, const std::shared_ptr<ActorBase>& actor,
    const std::shared_ptr<Supervisor>& nested, const std::string& failure, bool normal_stop,
    const std::string& file, int line, int column,
    const std::vector<std::shared_ptr<ActorBase>>& linked_triggers = {}) {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (!supervisor || supervisor->status != "alive") return failure;
    const auto found = std::find_if(supervisor->children.begin(), supervisor->children.end(),
        [&actor, &nested](const SupervisorChild& child) {
            return actor ? child.actor == actor : child.supervisor == nested;
        });
    if (found == supervisor->children.end()) return failure;
    const auto policy = found->policy;
    if (normal_stop && policy != RestartPolicy::permanent) return {};
    if (!normal_stop && policy == RestartPolicy::temporary) return failure;
    std::vector<std::size_t> trigger_indexes{
        static_cast<std::size_t>(std::distance(supervisor->children.begin(), found)),
    };
    for (const auto& linked : linked_triggers) {
        if (!linked || linked == actor || linked->supervisor.lock() != supervisor) continue;
        const auto linked_child = std::find_if(supervisor->children.begin(), supervisor->children.end(),
            [&linked](const SupervisorChild& child) { return child.actor == linked; });
        if (linked_child == supervisor->children.end()) continue;
        const auto linked_policy = linked_child->policy;
        if (normal_stop && linked_policy != RestartPolicy::permanent) continue;
        if (!normal_stop && linked_policy == RestartPolicy::temporary) continue;
        const auto linked_index = static_cast<std::size_t>(std::distance(supervisor->children.begin(), linked_child));
        if (std::find(trigger_indexes.begin(), trigger_indexes.end(), linked_index) == trigger_indexes.end())
            trigger_indexes.push_back(linked_index);
    }
    std::vector<bool> affected_flags(supervisor->children.size(), false);
    if (supervisor->strategy == RestartStrategy::one_for_all) {
        std::fill(affected_flags.begin(), affected_flags.end(), true);
    } else if (supervisor->strategy == RestartStrategy::rest_for_one) {
        const auto begin = *std::min_element(trigger_indexes.begin(), trigger_indexes.end());
        std::fill(affected_flags.begin() + static_cast<std::ptrdiff_t>(begin), affected_flags.end(), true);
    } else {
        for (const auto index : trigger_indexes) affected_flags[index] = true;
    }
    for (std::size_t current = supervisor->children.size(); current > 0; --current) {
        if (!affected_flags[current - 1]) continue;
        const auto& child = supervisor->children[current - 1];
        if (child.actor) stop_actor_for_supervisor(child.actor);
        else stop_supervisor_tree(child.supervisor);
    }
    const auto now = std::chrono::steady_clock::now();
    const auto window = std::chrono::milliseconds(supervisor->window_milliseconds);
    while (!supervisor->restart_times.empty() && now - supervisor->restart_times.front() > window)
        supervisor->restart_times.pop_front();
    if (static_cast<std::int64_t>(supervisor->restart_times.size()) >= supervisor->max_restarts) {
        const std::string label = actor ? actor->actor_name : "Supervisor";
        const std::string escalation = "supervisor restart limit exceeded for actor '" + label +
            "' within " + std::to_string(supervisor->window_milliseconds) + "ms: " + failure;
        supervisor->status = "failed";
        state.pending_restarts.erase(std::remove_if(
            state.pending_restarts.begin(), state.pending_restarts.end(),
            [&supervisor](const PendingSupervisorRestart& pending) { return pending.owner == supervisor; }),
            state.pending_restarts.end());
        stop_supervisor_children(supervisor);
        const auto parent = supervisor->parent.lock();
        if (parent) return supervisor_handle_failure(parent, {}, supervisor, escalation, false, file, line, column);
        return escalation;
    }
    supervisor->restart_times.push_back(now);
    if (supervisor->backoff) {
        std::int64_t delay = supervisor->backoff->base_milliseconds;
        for (std::size_t count = 1; count < supervisor->restart_times.size(); ++count) {
            if (delay >= supervisor->backoff->maximum_milliseconds - delay) {
                delay = supervisor->backoff->maximum_milliseconds;
                break;
            }
            delay *= 2;
        }
        delay = std::min(delay, supervisor->backoff->maximum_milliseconds);
        if (delay > 0) {
            std::vector<SupervisorChild> affected;
            for (std::size_t current = 0; current < supervisor->children.size(); ++current)
                if (affected_flags[current]) affected.push_back(supervisor->children[current]);
            for (const auto& child : affected) {
                if (child.supervisor) child.supervisor->status = "restarting";
            }
            {
                std::lock_guard<std::mutex> actor_lock(state.mutex);
                for (const auto& child : affected)
                    if (child.actor) child.actor->status = "restarting";
            }
            state.pending_restarts.push_back(PendingSupervisorRestart{
                supervisor, std::move(affected), actor, nested, failure, file, line, column,
                now + std::chrono::milliseconds(delay),
            });
            state.changed.notify_all();
            return {};
        }
    }
    for (std::size_t current = 0; current < supervisor->children.size(); ++current) {
        if (!affected_flags[current]) continue;
        const auto& child = supervisor->children[current];
        if (child.policy == RestartPolicy::temporary) continue;
        std::string restart_failure;
        if (child.actor) restart_failure = restart_actor_for_supervisor(child.actor, file, line, column);
        else if (child.supervisor)
            restart_failure = restart_supervisor_tree(child.supervisor, file, line, column);
        if (!restart_failure.empty())
            return supervisor_handle_failure(supervisor, actor, nested, restart_failure, false, file, line, column);
    }
    return {};
}
inline std::optional<std::string> settle_due_supervisor_restarts() {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    bool settled_any = false;
    while (!state.pending_restarts.empty()) {
        const auto next = std::min_element(state.pending_restarts.begin(), state.pending_restarts.end(),
            [](const auto& left, const auto& right) { return left.due < right.due; });
        if (next->due > std::chrono::steady_clock::now()) break;
        PendingSupervisorRestart pending = std::move(*next);
        state.pending_restarts.erase(next);
        settled_any = true;
        if (!pending.owner || pending.owner->status != "alive") continue;
        std::string restart_failure;
        for (const auto& child : pending.children) {
            if (child.policy == RestartPolicy::temporary) continue;
            if (child.actor)
                restart_failure = restart_actor_for_supervisor(
                    child.actor, pending.file, pending.line, pending.column);
            else if (child.supervisor)
                restart_failure = restart_supervisor_tree(
                    child.supervisor, pending.file, pending.line, pending.column);
            if (!restart_failure.empty()) break;
        }
        if (!restart_failure.empty()) {
            const auto error = supervisor_handle_failure(
                pending.owner, pending.trigger_actor, pending.trigger_supervisor,
                restart_failure, false, pending.file, pending.line, pending.column);
            if (!error.empty()) return error;
        }
    }
    if (settled_any) return std::string{};
    return std::nullopt;
}
inline bool wait_for_actor_or_restart(
    const std::string& file, int line, int column, bool wait_for_pending = true) {
    auto& state = actor_runtime_state();
    std::unique_lock<std::recursive_mutex> supervision_lock(state.supervision_mutex);
    if (!state.pending_restarts.empty()) {
        if (!wait_for_pending) {
            std::lock_guard<std::mutex> actor_lock(state.mutex);
            return !state.ready.empty();
        }
        const auto next = std::min_element(state.pending_restarts.begin(), state.pending_restarts.end(),
            [](const auto& left, const auto& right) { return left.due < right.due; });
        auto wake_at = std::min(next->due, std::chrono::steady_clock::now() + std::chrono::milliseconds(100));
        std::unique_lock<std::mutex> actor_lock(state.mutex);
        if (state.ready.empty()) {
            state.changed.wait_until(actor_lock, wake_at, [&state]() {
                return !state.ready.empty() || requested_shutdown_signal != 0;
            });
        }
        actor_lock.unlock();
        supervision_lock.unlock();
        check_shutdown(file, line, column);
        return true;
    }
    std::lock_guard<std::mutex> actor_lock(state.mutex);
    return !state.ready.empty();
}
inline bool actor_messages_ready() {
    auto& state = actor_runtime_state();
    std::lock_guard<std::mutex> lock(state.mutex);
    return !state.ready.empty();
}
inline std::optional<std::chrono::steady_clock::time_point> next_supervisor_restart_deadline() {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (state.pending_restarts.empty()) return std::nullopt;
    const auto next = std::min_element(state.pending_restarts.begin(), state.pending_restarts.end(),
        [](const auto& left, const auto& right) { return left.due < right.due; });
    return next->due;
}
inline std::shared_ptr<ActorBase> supervisor_start_actor(
    const std::shared_ptr<Supervisor>& supervisor, const std::shared_ptr<ActorBase>& actor,
    RestartPolicy policy, const std::string& file, int line, int column) {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> supervision_lock(state.supervision_mutex);
    if (!supervisor || supervisor->status != "alive")
        fail(file, line, column, "cannot start a child on an inactive supervisor");
    std::lock_guard<std::mutex> actor_lock(state.mutex);
    if (!actor || actor->status != "alive" || actor->supervisor.lock() || !actor->tyv_restart)
        fail(file, line, column, "native Supervisor.start requires a fresh restartable actor");
    actor->supervisor = supervisor;
    actor->restart_policy = policy;
    supervisor->children.push_back(SupervisorChild{actor, {}, policy});
    return actor;
}
inline std::shared_ptr<Supervisor> supervisor_start_nested(
    const std::shared_ptr<Supervisor>& supervisor, const std::shared_ptr<Supervisor>& child,
    RestartPolicy policy, const std::string& file, int line, int column) {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (!supervisor || supervisor->status != "alive")
        fail(file, line, column, "cannot start a child on an inactive supervisor");
    if (!child || child == supervisor || child->status != "alive" || !child->parent.expired())
        fail(file, line, column, "a Supervisor child must be a distinct, unowned supervisor");
    for (auto ancestor = supervisor; ancestor; ancestor = ancestor->parent.lock())
        if (ancestor == child) fail(file, line, column, "Supervisor trees cannot contain cycles");
    child->parent = supervisor;
    child->restart_policy = policy;
    supervisor->children.push_back(SupervisorChild{{}, child, policy});
    return child;
}
inline std::string supervisor_actor_stopped(
    const std::shared_ptr<ActorBase>& actor, const std::string& file, int line, int column) {
    if (!actor) return {};
    const auto supervisor = actor->supervisor.lock();
    if (!supervisor) return {};
    return supervisor_handle_failure(supervisor, actor, {}, "actor stopped normally", true, file, line, column);
}
inline std::string supervisor_actor_failed(
    const std::shared_ptr<ActorBase>& actor, const std::string& failure,
    const std::string& file, int line, int column) {
    if (!actor) return failure;
    const auto supervisor = actor->supervisor.lock();
    if (!supervisor) return failure;
    return supervisor_handle_failure(supervisor, actor, {}, failure, false, file, line, column);
}
inline std::string supervisor_stopped(
    const std::shared_ptr<Supervisor>& child, const std::string& file, int line, int column) {
    if (!child) return {};
    const auto parent = child->parent.lock();
    if (!parent) return {};
    return supervisor_handle_failure(parent, {}, child, "nested supervisor stopped normally", true, file, line, column);
}
inline void supervisor_stop(
    const std::shared_ptr<Supervisor>& supervisor, const std::string& file, int line, int column) {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> lock(state.supervision_mutex);
    if (!supervisor || supervisor->status != "alive") return;
    stop_supervisor_tree(supervisor);
    const auto failure = supervisor_stopped(supervisor, file, line, column);
    if (!failure.empty()) fail(file, line, column, failure);
}
template <typename T> inline std::shared_ptr<T> spawn_actor(
    std::string actor_name, const std::string& file, int line, int column) {
    try {
        auto actor = std::make_shared<T>();
        actor->actor_name = std::move(actor_name);
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        state.actors.push_back(actor);
        return actor;
    } catch (const std::bad_alloc&) {
        throw Failure(file, line, column, "cannot allocate actor state");
    }
}
inline void actor_started(const std::shared_ptr<ActorBase>& actor) {
    if (!actor) return;
    auto& state = actor_runtime_state();
    std::lock_guard<std::mutex> lock(state.mutex);
    actor->status = "alive";
}
inline void actor_initialization_failed(const std::shared_ptr<ActorBase>& actor) noexcept {
    if (!actor) return;
    auto& state = actor_runtime_state();
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        actor->status = "crashed";
        actor->mailbox.clear();
        actor->queued = false;
        remove_ready_actor_locked(state, actor.get());
    }
    cancel_actor_tasks(actor);
    fail_actor_replies(actor, "actor initialization failed before replying");
    actor->tyv_clear_state();
    notify_task_scheduler();
}
inline void enqueue_actor(
    const std::shared_ptr<ActorBase>& actor, const std::string& actor_name,
    std::function<void()> message, const std::string& file, int line, int column) {
    auto& state = actor_runtime_state();
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        if (!actor || actor->actor_name != actor_name)
            throw Failure(file, line, column, "actor reference type mismatch");
        if (actor->status != "alive")
            throw Failure(file, line, column, "cannot send a message to a " + actor->status + " actor");
        if (actor->mailbox.size() >= MAX_ACTOR_MAILBOX)
            throw Failure(file, line, column, "actor mailbox capacity (100000) exceeded");
        try {
            actor->mailbox.push_back(std::move(message));
            if (!actor->queued) {
                try {
                    state.ready.push_back(actor);
                    actor->queued = true;
                } catch (...) {
                    actor->mailbox.pop_back();
                    throw;
                }
            }
        } catch (const std::bad_alloc&) {
            throw Failure(file, line, column, "cannot enqueue actor message");
        }
    }
    notify_task_scheduler();
    actor_runtime_state().changed.notify_all();
}
template <typename T, typename MakeMessage> inline ReplyReceiver<T> actor_request(
    const std::shared_ptr<ActorBase>& actor, const std::string& actor_name,
    std::optional<std::int64_t> timeout_milliseconds, MakeMessage&& make_message,
    const std::string& file, int line, int column) {
    std::shared_ptr<ReplyCell<T>> cell;
    std::function<void()> message;
    try {
        cell = std::make_shared<ReplyCell<T>>(timeout_milliseconds);
        message = make_message(ReplySender<T>{cell});
    } catch (const std::bad_alloc&) {
        throw Failure(file, line, column, "cannot allocate actor request");
    }
    auto& state = actor_runtime_state();
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        if (!actor || actor->actor_name != actor_name)
            throw Failure(file, line, column, "actor reference type mismatch");
        if (actor->status != "alive")
            throw Failure(file, line, column, "cannot send a message to a " + actor->status + " actor");
        if (actor->mailbox.size() >= MAX_ACTOR_MAILBOX)
            throw Failure(file, line, column, "actor mailbox capacity (100000) exceeded");
        actor->pending_replies.erase(
            std::remove_if(actor->pending_replies.begin(), actor->pending_replies.end(),
                [](const auto& pending) {
                    const auto cell = pending.lock();
                    return !cell || !cell->is_pending();
                }),
            actor->pending_replies.end());
        bool message_added = false;
        try {
            actor->pending_replies.emplace_back(cell);
            actor->mailbox.push_back(std::move(message));
            message_added = true;
            if (!actor->queued) {
                state.ready.push_back(actor);
                actor->queued = true;
            }
        } catch (const std::bad_alloc&) {
            if (message_added) actor->mailbox.pop_back();
            actor->pending_replies.erase(
                std::remove_if(actor->pending_replies.begin(), actor->pending_replies.end(),
                    [&cell](const auto& pending) {
                        const auto candidate = pending.lock();
                        return !candidate || candidate == cell;
                    }),
                actor->pending_replies.end());
            throw Failure(file, line, column, "cannot enqueue actor request");
        }
    }
    state.changed.notify_all();
    notify_task_scheduler();
    return ReplyReceiver<T>{std::move(cell)};
}
inline bool actor_is_alive(const std::shared_ptr<ActorBase>& actor) noexcept {
    if (!actor) return false;
    auto& state = actor_runtime_state();
    std::lock_guard<std::mutex> lock(state.mutex);
    return actor->status == "alive";
}
inline bool actor_stop(
    const std::shared_ptr<ActorBase>& actor, const std::string& file, int line, int column) {
    if (!actor) return false;
    {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        if (actor->status != "alive") return false;
        actor->status = "stopped";
        actor->mailbox.clear();
        actor->queued = false;
        remove_ready_actor_locked(state, actor.get());
        (void)detach_actor_links_locked(actor);
    }
    cancel_actor_tasks(actor);
    fail_actor_replies(actor, "actor stopped before replying");
    notify_actor_monitors(actor, "normal stop");
    actor_runtime_state().changed.notify_all();
    actor->tyv_clear_state();
    const auto failure = supervisor_actor_stopped(actor, file, line, column);
    if (!failure.empty()) fail(file, line, column, failure);
    return true;
}
inline std::string actor_crash_message(const ActorBase& actor, const Failure& error) {
    return "actor '" + actor.actor_name + "' crashed at " + error.file + ":" +
        std::to_string(error.line) + ":" + std::to_string(error.column) + ": " + error.message;
}
inline std::vector<std::shared_ptr<ActorBase>> mark_actor_crashed(
    const std::shared_ptr<ActorBase>& actor, const std::string& failure, bool* transitioned = nullptr) {
    if (transitioned) *transitioned = false;
    if (!actor) return {};
    std::vector<std::shared_ptr<ActorBase>> linked;
    {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        if (actor->status != "alive") return {};
        actor->status = "crashed";
        actor->mailbox.clear();
        actor->queued = false;
        remove_ready_actor_locked(state, actor.get());
        linked = detach_actor_links_locked(actor);
    }
    cancel_actor_tasks(actor);
    fail_actor_replies(actor, failure);
    notify_actor_monitors(actor, failure);
    actor->tyv_clear_state();
    if (transitioned) *transitioned = true;
    return linked;
}
inline std::vector<std::shared_ptr<ActorBase>> propagate_linked_actor_crashes(
    const std::vector<std::shared_ptr<ActorBase>>& initially_linked,
    const std::shared_ptr<ActorBase>& source, const std::string& failure) {
    std::vector<std::pair<std::shared_ptr<ActorBase>, std::shared_ptr<ActorBase>>> pending;
    for (const auto& peer : initially_linked) pending.emplace_back(peer, source);
    std::vector<ActorBase*> seen;
    if (source) seen.push_back(source.get());
    std::vector<std::shared_ptr<ActorBase>> crashed;
    while (!pending.empty()) {
        auto edge = std::move(pending.back());
        pending.pop_back();
        const auto& peer = edge.first;
        if (!peer || std::find(seen.begin(), seen.end(), peer.get()) != seen.end()) continue;
        seen.push_back(peer.get());
        const std::string from_name = source ? source->actor_name : "linked actor";
        const std::string peer_failure = "actor '" + peer->actor_name +
            "' crashed because linked actor '" + from_name + "' failed: " + failure;
        bool transitioned = false;
        const auto neighbors = mark_actor_crashed(peer, peer_failure, &transitioned);
        if (!transitioned) continue;
        crashed.push_back(peer);
        for (const auto& neighbor : neighbors) pending.emplace_back(neighbor, peer);
    }
    return crashed;
}
inline std::string apply_actor_failure(
    const std::shared_ptr<ActorBase>& actor, const std::string& failure,
    const std::string& file, int line, int column,
    const std::vector<std::shared_ptr<ActorBase>>& linked_actors = {}) {
    if (!actor) return failure;
    std::vector<std::pair<std::shared_ptr<Supervisor>, std::vector<std::shared_ptr<ActorBase>>>> groups;
    std::string first_failure;
    auto add_trigger = [&](const std::shared_ptr<ActorBase>& trigger) {
        if (!trigger) return;
        const auto supervisor = trigger->supervisor.lock();
        if (!supervisor) {
            if (first_failure.empty()) first_failure = failure;
            return;
        }
        if (trigger->restart_policy == RestartPolicy::temporary) {
            if (first_failure.empty()) first_failure = failure;
            return;
        }
        auto found = std::find_if(groups.begin(), groups.end(),
            [&supervisor](const auto& group) { return group.first == supervisor; });
        if (found == groups.end()) {
            groups.emplace_back(supervisor, std::vector<std::shared_ptr<ActorBase>>{});
            found = groups.end() - 1;
        }
        found->second.push_back(trigger);
    };
    add_trigger(actor);
    for (const auto& linked : linked_actors) add_trigger(linked);
    for (const auto& group : groups) {
        if (group.second.empty()) continue;
        std::vector<std::shared_ptr<ActorBase>> extra(group.second.begin() + 1, group.second.end());
        const auto failure_result = supervisor_handle_failure(
            group.first, group.second.front(), {}, failure, false, file, line, column, extra);
        if (!failure_result.empty() && first_failure.empty()) first_failure = failure_result;
    }
    return first_failure;
}
inline StringResult<std::int64_t> run_actors(
    const std::string& file, int line, int column, bool wait_for_pending = true,
    std::size_t delivery_limit = MAX_ACTOR_DELIVERIES, bool report_delivery_limit = true) {
    if (actor_handler_active())
        throw Failure(file, line, column, "runActors cannot be called from an actor message handler");
    std::int64_t processed = 0;
    std::string first_crash;
    while (processed < delivery_limit) {
        check_shutdown(file, line, column);
        const auto settled = settle_due_supervisor_restarts();
        if (settled && !settled->empty() && first_crash.empty()) first_crash = *settled;
        std::shared_ptr<ActorBase> actor;
        std::function<void()> message;
        for (;;) {
            auto& state = actor_runtime_state();
            {
                std::lock_guard<std::mutex> lock(state.mutex);
                if (!state.ready.empty()) {
                    actor = std::move(state.ready.front());
                    state.ready.pop_front();
                    actor->queued = false;
                    if (actor->status == "alive" && !actor->mailbox.empty()) {
                        message = std::move(actor->mailbox.front());
                        actor->mailbox.pop_front();
                    }
                }
            }
            if (actor) {
                if (message) break;
                actor.reset();
                continue;
            }
            if (!wait_for_actor_or_restart(file, line, column, wait_for_pending)) break;
            const auto due_settled = settle_due_supervisor_restarts();
            if (due_settled && !due_settled->empty() && first_crash.empty())
                first_crash = *due_settled;
        }
        if (!actor) break;
        ++processed;
        try {
            ActorHandlerScope handler_scope(actor);
            message();
        } catch (const SignalShutdown&) {
            throw;
        } catch (const Failure& error) {
            bool was_alive = false;
            {
                auto& state = actor_runtime_state();
                std::lock_guard<std::mutex> lock(state.mutex);
                was_alive = actor->status == "alive";
            }
            if (was_alive) {
                const auto crash = actor_crash_message(*actor, error);
                const auto linked = mark_actor_crashed(actor, crash);
                const auto propagated = propagate_linked_actor_crashes(linked, actor, crash);
                const auto failure = apply_actor_failure(
                    actor, crash, error.file, error.line, error.column, propagated);
                if (first_crash.empty() && !failure.empty()) first_crash = failure;
            }
        } catch (const std::exception&) {
            bool was_alive = false;
            {
                auto& state = actor_runtime_state();
                std::lock_guard<std::mutex> lock(state.mutex);
                was_alive = actor->status == "alive";
            }
            if (was_alive) {
                const std::string crash = "actor '" + actor->actor_name + "' crashed: native runtime failure";
                const auto linked = mark_actor_crashed(actor, crash);
                const auto propagated = propagate_linked_actor_crashes(linked, actor, crash);
                const auto failure = apply_actor_failure(actor, crash, "<native>", 1, 1, propagated);
                if (first_crash.empty() && !failure.empty()) first_crash = failure;
            }
        } catch (...) {
            bool was_alive = false;
            {
                auto& state = actor_runtime_state();
                std::lock_guard<std::mutex> lock(state.mutex);
                was_alive = actor->status == "alive";
            }
            if (was_alive) {
                const std::string crash = "actor '" + actor->actor_name + "' crashed: native runtime failure";
                mark_actor_crashed(actor, crash);
                const auto failure = apply_actor_failure(actor, crash, "<native>", 1, 1);
                if (first_crash.empty() && !failure.empty()) first_crash = failure;
            }
        }
        {
            auto& state = actor_runtime_state();
            std::lock_guard<std::mutex> lock(state.mutex);
            if (actor->status == "alive" && !actor->mailbox.empty() && !actor->queued) {
                try {
                    state.ready.push_back(actor);
                    actor->queued = true;
                } catch (const std::bad_alloc&) {
                    actor->status = "crashed";
                    actor->mailbox.clear();
                    if (first_crash.empty())
                        first_crash = "actor '" + actor->actor_name + "' crashed: native runtime failure";
                }
            }
        }
    }
    {
        auto& state = actor_runtime_state();
        std::lock_guard<std::mutex> lock(state.mutex);
        if (report_delivery_limit && processed >= delivery_limit
            && !state.ready.empty() && first_crash.empty())
            first_crash = "actor scheduler reached the 100000-message safety limit; messages remain queued";
    }
    if (!first_crash.empty()) return result_error<std::int64_t>(std::move(first_crash));
    return result_ok<std::int64_t>(processed);
}
inline bool native_actor_message_ready() {
    auto& state = actor_runtime_state();
    std::lock_guard<std::mutex> lock(state.mutex);
    return !state.ready.empty();
}
inline void shutdown_actors() noexcept {
    auto& state = actor_runtime_state();
    std::lock_guard<std::recursive_mutex> supervision_lock(state.supervision_mutex);
    std::vector<std::shared_ptr<ActorBase>> actors;
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        for (auto supervisor = state.supervisors.rbegin(); supervisor != state.supervisors.rend(); ++supervisor) {
            (*supervisor)->status = "stopped";
            (*supervisor)->children.clear();
        }
        actors.swap(state.actors);
        for (auto actor = actors.rbegin(); actor != actors.rend(); ++actor) {
            (*actor)->status = "stopped";
            (*actor)->mailbox.clear();
            (*actor)->queued = false;
        }
        state.ready.clear();
        state.pending_restarts.clear();
        state.supervisors.clear();
    }
    for (auto actor = actors.rbegin(); actor != actors.rend(); ++actor) {
        cancel_actor_tasks(*actor);
        fail_actor_replies(*actor, "actor stopped before replying");
        notify_actor_monitors(*actor, "runtime shutdown");
        (*actor)->tyv_restart = {};
        (*actor)->tyv_clear_state();
    }
    state.changed.notify_all();
}
struct ActorRuntimeScope {
    ~ActorRuntimeScope() { shutdown_actors(); }
};
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
struct WebSocketUrl {
    std::string scheme;
    std::string host;
    std::int64_t port = 0;
    std::string target;
};
struct WebSocketMessage {
    int opcode = 1;
    std::vector<std::uint8_t> payload;
};
struct HttpMethod {
    std::string name;
};
struct HttpUrl {
    std::string scheme;
    std::string host;
    std::int64_t port = 0;
    std::string target;
};
struct HttpHeaders {
    std::vector<std::pair<std::string, std::string>> entries;
};
struct HttpRequest {
    HttpMethod method;
    std::string target;
    std::shared_ptr<HttpHeaders> headers;
    std::vector<std::uint8_t> body;
};
struct HttpResponse {
    std::int64_t status = 0;
    std::shared_ptr<HttpHeaders> headers;
    std::vector<std::uint8_t> body;
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
struct WebSocket {
    std::shared_ptr<TcpConnection> tcp;
    std::shared_ptr<TlsConnection> tls;
    std::int64_t operation_timeout_milliseconds = 0;
    std::vector<std::uint8_t> receive_buffer;
    std::optional<int> fragment_opcode;
    std::vector<std::uint8_t> fragment;
    std::optional<std::int64_t> close_code;
    std::optional<std::string> close_reason;
    std::mutex receive_mutex;
    std::mutex send_mutex;
    std::mutex metadata_mutex;
    std::string source_file;
    int source_line = 1;
    int source_column = 1;
    bool is_open() const noexcept {
        return tls ? tls->is_open() : (tcp && tcp->socket && tcp->socket->is_open());
    }
    bool close() noexcept {
        if (tls) return tls->close();
        return tcp && tcp->socket && tcp->socket->close();
    }
    ~WebSocket() noexcept {
        if (is_open() && !close())
            record_cleanup_failure(source_file, source_line, source_column, "cannot close WebSocket");
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
inline void set_resource_location(const std::shared_ptr<WebSocket>& resource,
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


inline std::string websocket_lower_ascii(std::string value) {
    for (auto& character : value) {
        const auto byte = static_cast<unsigned char>(character);
        if (byte >= 'A' && byte <= 'Z') character = static_cast<char>(byte - 'A' + 'a');
    }
    return value;
}
inline std::string websocket_trim(std::string value) {
    const auto first = value.find_first_not_of(" \t");
    if (first == std::string::npos) return {};
    const auto last = value.find_last_not_of(" \t");
    return value.substr(first, last - first + 1);
}
inline bool websocket_ipv4_tail(const std::string& value) {
    std::size_t start = 0;
    int octets = 0;
    while (start <= value.size()) {
        const auto end = value.find('.', start);
        const auto stop = end == std::string::npos ? value.size() : end;
        const auto length = stop - start;
        if (length == 0 || length > 3 || (length > 1 && value[start] == '0')) return false;
        int octet = 0;
        for (std::size_t index = start; index < stop; ++index) {
            const auto byte = static_cast<unsigned char>(value[index]);
            if (byte < '0' || byte > '9') return false;
            octet = octet * 10 + (byte - '0');
        }
        if (octet > 255) return false;
        ++octets;
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return octets == 4;
}
inline bool websocket_valid_ipv6(const std::string& value) {
    if (value.empty() || value.find('%') != std::string::npos || value.find(":::") != std::string::npos)
        return false;
    const auto compression = value.find("::");
    const bool compressed = compression != std::string::npos;
    if (compressed && value.find("::", compression + 2) != std::string::npos) return false;
    const std::string before = compressed ? value.substr(0, compression) : value;
    const std::string after = compressed ? value.substr(compression + 2) : std::string();
    std::size_t groups = 0;
    auto count_side = [&groups](const std::string& side, bool allow_ipv4) {
        if (side.empty()) return true;
        std::size_t start = 0;
        while (start <= side.size()) {
            const auto end = side.find(':', start);
            const auto stop = end == std::string::npos ? side.size() : end;
            const auto part = side.substr(start, stop - start);
            if (part.empty()) return false;
            if (part.find('.') != std::string::npos) {
                if (!allow_ipv4 || end != std::string::npos || !websocket_ipv4_tail(part)) return false;
                groups += 2;
            } else {
                if (part.size() > 4) return false;
                for (const auto character : part) {
                    const auto byte = static_cast<unsigned char>(character);
                    if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f')
                          || (byte >= 'A' && byte <= 'F'))) return false;
                }
                ++groups;
            }
            if (end == std::string::npos) break;
            start = end + 1;
        }
        return true;
    };
    if (!count_side(before, !compressed) || !count_side(after, true) || groups > 8) return false;
    return compressed ? groups < 8 : groups == 8;
}
inline bool websocket_valid_hostname(const std::string& host) {
    if (host.empty() || host.size() > 253) return false;
    const auto checked = host.back() == '.' ? host.substr(0, host.size() - 1) : host;
    if (checked.empty()) return false;
    std::size_t start = 0;
    while (start < checked.size()) {
        const auto end = checked.find('.', start);
        const auto length = (end == std::string::npos ? checked.size() : end) - start;
        if (length == 0 || length > 63 || checked[start] == '-' || checked[start + length - 1] == '-') return false;
        for (std::size_t i = start; i < start + length; ++i) {
            const auto c = static_cast<unsigned char>(checked[i]);
            if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                  || (c >= '0' && c <= '9') || c == '-')) return false;
        }
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return true;
}
inline bool websocket_parse_port(const std::string& text, std::int64_t& port) {
    if (text.empty()) return false;
    for (const auto byte : text) if (byte < '0' || byte > '9') return false;
    std::int64_t parsed = 0;
    const auto conversion = std::from_chars(text.data(), text.data() + text.size(), parsed);
    if (conversion.ec != std::errc{} || conversion.ptr != text.data() + text.size() || parsed < 1 || parsed > 65535)
        return false;
    port = parsed;
    return true;
}
inline StringResult<WebSocketUrl> parse_websocket_url(const std::string& input) {
    const auto separator = input.find("://");
    if (separator == std::string::npos) return result_error<WebSocketUrl>("WebSocket URL scheme must be ws or wss");
    const auto scheme = websocket_lower_ascii(input.substr(0, separator));
    if (scheme != "ws" && scheme != "wss")
        return result_error<WebSocketUrl>("WebSocket URL scheme must be ws or wss");
    const auto remainder = input.substr(separator + 3);
    if (remainder.find('#') != std::string::npos)
        return result_error<WebSocketUrl>("HTTP request URL must not contain a fragment");
    const auto first_path = remainder.find('/');
    const auto first_query = remainder.find('?');
    std::size_t authority_end = remainder.size();
    if (first_path != std::string::npos) authority_end = std::min(authority_end, first_path);
    if (first_query != std::string::npos) authority_end = std::min(authority_end, first_query);
    const auto authority = remainder.substr(0, authority_end);
    const auto suffix = remainder.substr(authority_end);
    if (authority.empty() || authority.find('@') != std::string::npos)
        return result_error<WebSocketUrl>("invalid HTTP URL authority");
    WebSocketUrl output;
    output.scheme = scheme;
    output.port = scheme == "wss" ? 443 : 80;
    if (authority.front() == '[') {
        const auto closing = authority.find(']');
        if (closing == std::string::npos) return result_error<WebSocketUrl>("invalid IPv6 URL authority");
        output.host = authority.substr(1, closing - 1);
        if (output.host.find('%') != std::string::npos)
            return result_error<WebSocketUrl>("IPv6 URL scope identifiers are not supported");
        if (!websocket_valid_ipv6(output.host))
            return result_error<WebSocketUrl>("invalid IPv6 URL authority");
        const auto rest = authority.substr(closing + 1);
        if (!rest.empty()) {
            if (rest.front() != ':' || !websocket_parse_port(rest.substr(1), output.port))
                return result_error<WebSocketUrl>("invalid HTTP URL port");
        }
        output.host = websocket_lower_ascii(output.host);
    } else {
        if (std::count(authority.begin(), authority.end(), ':') > 1)
            return result_error<WebSocketUrl>("IPv6 URL hosts must be enclosed in brackets");
        const auto colon = authority.find(':');
        output.host = authority.substr(0, colon);
        if (colon != std::string::npos && !websocket_parse_port(authority.substr(colon + 1), output.port))
            return result_error<WebSocketUrl>("invalid HTTP URL port");
        if (!websocket_valid_hostname(output.host))
            return result_error<WebSocketUrl>("invalid HTTP URL hostname");
        output.host = websocket_lower_ascii(output.host);
    }
    if (suffix.empty()) output.target = "/";
    else if (suffix.front() == '?') output.target = "/" + suffix;
    else if (suffix.front() == '/') output.target = suffix;
    else return result_error<WebSocketUrl>("invalid HTTP URL target");
    if (output.target.find('#') != std::string::npos)
        return result_error<WebSocketUrl>("HTTP request target must be origin-form without a fragment");
    for (std::size_t index = 0; index < output.target.size(); ++index) {
        const auto byte = static_cast<unsigned char>(output.target[index]);
        if (byte < 33 || byte > 126)
            return result_error<WebSocketUrl>("HTTP request target must be origin-form without a fragment");
        if (byte == '%') {
            if (index + 2 >= output.target.size() || !std::isxdigit(static_cast<unsigned char>(output.target[index + 1]))
                || !std::isxdigit(static_cast<unsigned char>(output.target[index + 2])))
                return result_error<WebSocketUrl>("invalid percent escape");
            index += 2;
        }
    }
    return result_ok<WebSocketUrl>(std::move(output));
}
inline StringResult<WebSocketUrl> websocket_url(const std::string& value) {
    return parse_websocket_url(value);
}
inline std::string websocket_url_scheme(const WebSocketUrl& value) { return value.scheme; }
inline std::string websocket_url_host(const WebSocketUrl& value) { return value.host; }
inline std::int64_t websocket_url_port(const WebSocketUrl& value) { return value.port; }
inline std::string websocket_url_target(const WebSocketUrl& value) { return value.target; }
inline bool websocket_url_host_ends_with(const WebSocketUrl& value, const std::string& suffix) {
    return suffix.size() <= value.host.size()
        && value.host.compare(value.host.size() - suffix.size(), suffix.size(), suffix) == 0;
}
inline std::string websocket_message_kind(const WebSocketMessage& value) {
    return value.opcode == 1 ? "text" : "binary";
}
inline std::vector<std::uint8_t> websocket_message_bytes(const WebSocketMessage& value) {
    return value.payload;
}
inline StringResult<std::string> websocket_message_text(const WebSocketMessage& value) {
    if (value.opcode != 1) return result_error<std::string>("WebSocket message is binary");
    if (!valid_utf8(value.payload)) return result_error<std::string>("WebSocket text is not valid UTF-8");
    std::string result;
    if (!value.payload.empty())
        result.assign(reinterpret_cast<const char*>(value.payload.data()), value.payload.size());
    return result_ok<std::string>(std::move(result));
}
#if defined(__linux__) && defined(TYVERA_NATIVE_TLS)
constexpr std::size_t MAX_NATIVE_WEBSOCKET_FRAME_BYTES = 1U * 1024U * 1024U;
constexpr std::size_t MAX_NATIVE_WEBSOCKET_MESSAGE_BYTES = 4U * 1024U * 1024U;
constexpr std::size_t MAX_NATIVE_WEBSOCKET_HANDSHAKE_BYTES = 32U * 1024U;
constexpr char WEBSOCKET_GUID[] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

inline std::string websocket_base64(const unsigned char* input, std::size_t length) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string output;
    output.reserve(((length + 2) / 3) * 4);
    for (std::size_t index = 0; index < length; index += 3) {
        const auto remaining = length - index;
        const std::uint32_t a = input[index];
        const std::uint32_t b = remaining > 1 ? input[index + 1] : 0;
        const std::uint32_t c = remaining > 2 ? input[index + 2] : 0;
        output.push_back(alphabet[(a >> 2) & 0x3f]);
        output.push_back(alphabet[((a << 4) | (b >> 4)) & 0x3f]);
        output.push_back(remaining > 1 ? alphabet[((b << 2) | (c >> 6)) & 0x3f] : '=');
        output.push_back(remaining > 2 ? alphabet[c & 0x3f] : '=');
    }
    return output;
}
inline StringResult<std::vector<std::uint8_t>> websocket_encode_client_frame(
    int opcode, const std::vector<std::uint8_t>& payload) {
    if (!(opcode == 0 || opcode == 1 || opcode == 2 || opcode == 8 || opcode == 9 || opcode == 10))
        return result_error<std::vector<std::uint8_t>>("invalid WebSocket opcode");
    const bool control = opcode >= 8;
    if (control && payload.size() > 125)
        return result_error<std::vector<std::uint8_t>>("invalid WebSocket control frame");
    if (payload.size() > MAX_NATIVE_WEBSOCKET_FRAME_BYTES)
        return result_error<std::vector<std::uint8_t>>("WebSocket frame exceeds size limit");
    if (opcode == 1 && !valid_utf8(payload))
        return result_error<std::vector<std::uint8_t>>("WebSocket text message is not valid UTF-8");
    if (opcode == 8 && payload.size() == 1)
        return result_error<std::vector<std::uint8_t>>("invalid WebSocket close payload");
    if (opcode == 8 && payload.size() >= 2) {
        const auto code = static_cast<int>((static_cast<unsigned int>(payload[0]) << 8) | payload[1]);
        const bool valid_code = (code >= 1000 && code <= 1014 && code != 1004 && code != 1005 && code != 1006)
            || (code >= 3000 && code <= 4999);
        if (!valid_code) return result_error<std::vector<std::uint8_t>>("invalid WebSocket close code");
        std::vector<std::uint8_t> reason(payload.begin() + 2, payload.end());
        if (!valid_utf8(reason)) return result_error<std::vector<std::uint8_t>>("WebSocket close reason is not valid UTF-8");
    }
    std::array<unsigned char, 4> mask{};
    if (RAND_bytes(mask.data(), static_cast<int>(mask.size())) != 1)
        return result_error<std::vector<std::uint8_t>>("WebSocket secure random generation failed");
    std::vector<std::uint8_t> frame;
    frame.reserve(payload.size() + 14);
    frame.push_back(static_cast<std::uint8_t>(0x80 | opcode));
    const auto length = static_cast<std::uint64_t>(payload.size());
    if (length < 126) frame.push_back(static_cast<std::uint8_t>(0x80 | length));
    else if (length <= 0xffff) {
        frame.push_back(0x80 | 126);
        frame.push_back(static_cast<std::uint8_t>((length >> 8) & 0xff));
        frame.push_back(static_cast<std::uint8_t>(length & 0xff));
    } else {
        frame.push_back(0x80 | 127);
        for (int shift = 56; shift >= 0; shift -= 8)
            frame.push_back(static_cast<std::uint8_t>((length >> shift) & 0xff));
    }
    frame.insert(frame.end(), mask.begin(), mask.end());
    for (std::size_t index = 0; index < payload.size(); ++index)
        frame.push_back(static_cast<std::uint8_t>(payload[index] ^ mask[index & 3]));
    return result_ok<std::vector<std::uint8_t>>(std::move(frame));
}
inline std::string websocket_accept_value(const std::string& key) {
    const auto source = key + WEBSOCKET_GUID;
    std::array<unsigned char, SHA_DIGEST_LENGTH> digest{};
    SHA1(reinterpret_cast<const unsigned char*>(source.data()), source.size(), digest.data());
    return websocket_base64(digest.data(), digest.size());
}
inline std::string websocket_authority(const WebSocketUrl& url) {
    std::string host = url.host.find(':') == std::string::npos ? url.host : "[" + url.host + "]";
    const auto default_port = url.scheme == "wss" ? 443 : 80;
    if (url.port != default_port) host += ":" + std::to_string(url.port);
    return host;
}
inline std::vector<std::string> websocket_header_tokens(const std::vector<std::string>& values) {
    std::vector<std::string> tokens;
    for (const auto& value : values) {
        std::size_t start = 0;
        while (start <= value.size()) {
            const auto end = value.find(',', start);
            tokens.push_back(websocket_lower_ascii(websocket_trim(value.substr(
                start, (end == std::string::npos ? value.size() : end) - start))));
            if (end == std::string::npos) break;
            start = end + 1;
        }
    }
    return tokens;
}
inline bool websocket_header_token(const std::vector<std::string>& values, const std::string& expected) {
    const auto tokens = websocket_header_tokens(values);
    return std::find(tokens.begin(), tokens.end(), expected) != tokens.end();
}
inline bool websocket_parse_handshake(
    const std::vector<std::uint8_t>& buffer, const std::string& key, std::size_t& header_size,
    std::string& error) {
    const std::array<std::uint8_t, 4> terminator{{'\r', '\n', '\r', '\n'}};
    const auto found = std::search(buffer.begin(), buffer.end(), terminator.begin(), terminator.end());
    if (found == buffer.end()) return false;
    header_size = static_cast<std::size_t>(found - buffer.begin()) + terminator.size();
    const std::string text(buffer.begin(), buffer.begin() + static_cast<std::ptrdiff_t>(header_size));
    const auto first_end = text.find("\r\n");
    if (first_end == std::string::npos) { error = "invalid HTTP status line"; return false; }
    const auto status_line = text.substr(0, first_end);
    if (!(status_line.rfind("HTTP/1.0 ", 0) == 0 || status_line.rfind("HTTP/1.1 ", 0) == 0)
        || status_line.size() < 12 || status_line[9] < '0' || status_line[9] > '9'
        || status_line[10] < '0' || status_line[10] > '9' || status_line[11] < '0' || status_line[11] > '9') {
        error = "invalid HTTP status line"; return false;
    }
    const int status = (status_line[9] - '0') * 100 + (status_line[10] - '0') * 10 + status_line[11] - '0';
    if (status != 101) { error = "WebSocket server did not accept the Upgrade request"; return false; }
    std::vector<std::string> upgrades, connections, accepts;
    bool has_extensions = false;
    bool has_body = false;
    std::size_t content_length_count = 0;
    std::size_t line_start = first_end + 2;
    while (line_start + 2 < header_size) {
        const auto line_end = text.find("\r\n", line_start);
        if (line_end == std::string::npos || line_end == line_start) break;
        const auto line = text.substr(line_start, line_end - line_start);
        const auto colon = line.find(':');
        if (colon == std::string::npos || colon == 0) {
            error = "invalid HTTP header"; return false;
        }
        const auto name = websocket_lower_ascii(line.substr(0, colon));
        const auto value = websocket_trim(line.substr(colon + 1));
        if (name == "upgrade") upgrades.push_back(value);
        else if (name == "connection") connections.push_back(value);
        else if (name == "sec-websocket-accept") accepts.push_back(value);
        else if (name == "sec-websocket-extensions") has_extensions = true;
        else if (name == "transfer-encoding") has_body = true;
        else if (name == "content-length") {
            if (++content_length_count > 1) { error = "duplicate HTTP Content-Length"; return false; }
            if (value.empty()) { error = "invalid HTTP Content-Length"; return false; }
            std::uint64_t length = 0;
            for (const auto byte : value) {
                if (byte < '0' || byte > '9' ||
                    length > (std::numeric_limits<std::uint64_t>::max() - static_cast<unsigned int>(byte - '0')) / 10) {
                    error = "invalid HTTP Content-Length"; return false;
                }
                length = length * 10 + static_cast<unsigned int>(byte - '0');
            }
            if (length != 0) has_body = true;
        }
        line_start = line_end + 2;
    }
    if (has_body) { error = "WebSocket Upgrade response must not contain a body"; return false; }
    if (!websocket_header_token(upgrades, "websocket") || !websocket_header_token(connections, "upgrade")) {
        error = "invalid WebSocket Upgrade headers"; return false;
    }
    const auto expected = websocket_accept_value(key);
    if (accepts.size() != 1 || accepts.front().size() != expected.size()
        || CRYPTO_memcmp(accepts.front().data(), expected.data(), expected.size()) != 0) {
        error = "invalid WebSocket accept value"; return false;
    }
    if (has_extensions) { error = "WebSocket extensions are not negotiated"; return false; }
    return true;
}

using WebSocketReadResult = StringResult<std::optional<std::vector<std::uint8_t>>>;
using WebSocketWriteResult = StringResult<std::int64_t>;
using WebSocketReceiveResult = StringResult<std::optional<WebSocketMessage>>;
inline Task<WebSocketReadResult> websocket_transport_read(
    const std::shared_ptr<WebSocket>& value, std::int64_t limit,
    const std::string& file, int line, int column) {
    if (value->tls) return tls_read(value->tls, limit, file, line, column);
    return tcp_read(value->tcp, limit, file, line, column);
}
inline Task<WebSocketWriteResult> websocket_transport_write(
    const std::shared_ptr<WebSocket>& value, std::vector<std::uint8_t> bytes,
    const std::string& file, int line, int column) {
    if (value->tls) return tls_write_all(value->tls, std::move(bytes), file, line, column);
    return tcp_write_all(value->tcp, std::move(bytes), file, line, column);
}
template <typename T>
inline bool websocket_await_timed(
    Task<T> operation, std::int64_t milliseconds, const std::string& file, int line, int column,
    std::optional<T>& output) {
    if (!operation.state) throw Failure(file, line, column, "cannot await an empty Task");
    const auto target = operation.state;
    const auto deadline = duration_deadline(Duration{milliseconds});
    if (!target->done.load(std::memory_order_acquire)) {
        if (milliseconds <= 0) target->request_cancel(true);
        else target->defer_start_until(deadline);
    }
    if (!target->wait_until(deadline, active_task_control)) {
        if (active_task_control) active_task_control->check_interruption();
        if (!target->done.load(std::memory_order_acquire)
            && !target->is_cancelled()
            && std::chrono::steady_clock::now() >= deadline)
            target->request_cancel(true);
    }
    if (active_task_control) active_task_control->check_interruption();
    try {
        output.emplace(operation.await_move(file, line, column));
        return true;
    } catch (const Failure&) {
        if (target->deadline_cancellation.load(std::memory_order_acquire)) return false;
        throw;
    }
}
inline StringResult<std::int64_t> websocket_write_frame(
    const std::shared_ptr<WebSocket>& value, int opcode, const std::vector<std::uint8_t>& payload,
    const std::string& file, int line, int column, const std::string& operation_name = "write") {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    std::unique_lock<std::mutex> lock(value->send_mutex, std::try_to_lock);
    if (!lock.owns_lock()) return result_error<std::int64_t>("WebSocket already has a pending write");
    auto encoded = websocket_encode_client_frame(opcode, payload);
    if (std::holds_alternative<std::string>(encoded))
        return result_error<std::int64_t>(std::get<1>(std::move(encoded)));
    auto operation = websocket_transport_write(value, std::get<0>(std::move(encoded)), file, line, column);
    std::optional<WebSocketWriteResult> completed;
    if (!websocket_await_timed(std::move(operation), value->operation_timeout_milliseconds,
                               file, line, column, completed))
        return result_error<std::int64_t>("WebSocket " + operation_name + " timed out");
    if (!completed || std::holds_alternative<std::string>(*completed))
        return result_error<std::int64_t>("WebSocket " + operation_name + " failed: "
            + (completed ? std::get<1>(*completed) : std::string("invalid completion")));
    return result_ok<std::int64_t>(static_cast<std::int64_t>(payload.size()));
}
inline Task<StringResult<std::shared_ptr<WebSocket>>> websocket_connect(
    WebSocketUrl url, Duration timeout, const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<WebSocket>>;
    if (timeout.milliseconds <= 0)
        return make_io_task<Output>([]() -> Output {
            return result_error<std::shared_ptr<WebSocket>>("WebSocket timeout must be greater than zero");
        }, file, line, column);
    auto work = make_io_task<Output>([url = std::move(url), timeout, file, line, column]() mutable -> Output {
        try {
            const bool secure = url.scheme == "wss";
            std::shared_ptr<WebSocket> socket = std::make_shared<WebSocket>();
            socket->operation_timeout_milliseconds = timeout.milliseconds;
            if (secure) {
                auto opened = tls_connect(url.host, url.port, file, line, column).await_move(file, line, column);
                if (std::holds_alternative<std::string>(opened))
                    return result_error<std::shared_ptr<WebSocket>>("WebSocket connect failed: " + std::get<1>(std::move(opened)));
                socket->tls = std::get<0>(std::move(opened));
            } else {
                auto opened = tcp_connect(url.host, url.port, file, line, column).await_move(file, line, column);
                if (std::holds_alternative<std::string>(opened))
                    return result_error<std::shared_ptr<WebSocket>>("WebSocket connect failed: " + std::get<1>(std::move(opened)));
                socket->tcp = std::get<0>(std::move(opened));
            }
            set_resource_location(socket, file, line, column);
            std::array<unsigned char, 16> random_key{};
            if (RAND_bytes(random_key.data(), static_cast<int>(random_key.size())) != 1) {
                socket->close();
                return result_error<std::shared_ptr<WebSocket>>("invalid WebSocket key");
            }
            const auto key = websocket_base64(random_key.data(), random_key.size());
            const auto request = std::string("GET ") + url.target + " HTTP/1.1\r\nHost: "
                + websocket_authority(url)
                + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: "
                + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n";
            auto sent = websocket_transport_write(socket, bytes_from_string(request), file, line, column);
            std::optional<WebSocketWriteResult> write_result;
            if (!websocket_await_timed(std::move(sent), timeout.milliseconds, file, line, column, write_result)) {
                socket->close();
                return result_error<std::shared_ptr<WebSocket>>("WebSocket handshake-write failed: timed out");
            }
            if (!write_result || std::holds_alternative<std::string>(*write_result)) {
                const auto detail = write_result ? std::get<1>(*write_result) : std::string("invalid write completion");
                socket->close();
                return result_error<std::shared_ptr<WebSocket>>("WebSocket handshake-write failed: " + detail);
            }
            std::size_t total = 0;
            for (;;) {
                auto reading = websocket_transport_read(socket, 65536, file, line, column);
                std::optional<WebSocketReadResult> read_result;
                if (!websocket_await_timed(std::move(reading), timeout.milliseconds, file, line, column, read_result)) {
                    socket->close();
                    return result_error<std::shared_ptr<WebSocket>>("WebSocket handshake-read failed: timed out");
                }
                if (!read_result || std::holds_alternative<std::string>(*read_result)) {
                    const auto detail = read_result ? std::get<1>(*read_result) : std::string("invalid read completion");
                    socket->close();
                    return result_error<std::shared_ptr<WebSocket>>("WebSocket handshake-read failed: " + detail);
                }
                auto chunk = std::get<0>(*read_result);
                if (!chunk) {
                    socket->close();
                    return result_error<std::shared_ptr<WebSocket>>("WebSocket server closed during handshake");
                }
                total += chunk->size();
                if (total > MAX_NATIVE_WEBSOCKET_HANDSHAKE_BYTES) {
                    socket->close();
                    return result_error<std::shared_ptr<WebSocket>>("WebSocket handshake exceeds size limit");
                }
                socket->receive_buffer.insert(socket->receive_buffer.end(), chunk->begin(), chunk->end());
                std::size_t header_size = 0;
                std::string error;
                const bool valid = websocket_parse_handshake(socket->receive_buffer, key, header_size, error);
                if (!error.empty()) {
                    socket->close();
                    return result_error<std::shared_ptr<WebSocket>>(std::move(error));
                }
                if (valid) {
                    socket->receive_buffer.erase(
                        socket->receive_buffer.begin(),
                        socket->receive_buffer.begin() + static_cast<std::ptrdiff_t>(header_size));
                    if (socket->receive_buffer.size() > MAX_NATIVE_WEBSOCKET_MESSAGE_BYTES + 14) {
                        socket->close();
                        return result_error<std::shared_ptr<WebSocket>>("WebSocket receive buffer exceeds size limit");
                    }
                    return result_ok<std::shared_ptr<WebSocket>>(std::move(socket));
                }
            }
        } catch (const TaskCancelled&) {
            throw;
        } catch (const Failure&) {
            throw;
        } catch (...) {
            return result_error<std::shared_ptr<WebSocket>>("WebSocket connection failed");
        }
    }, file, line, column);
    auto bounded = task_with_timeout(std::move(work), timeout, true, file, line, column);
    return make_io_task<Output>([bounded = std::move(bounded), file, line, column]() mutable -> Output {
        try {
            return bounded.await_move(file, line, column);
        } catch (const Failure& failure) {
            if (std::string(failure.what()) == "task timed out")
                return result_error<std::shared_ptr<WebSocket>>("WebSocket connection timed out");
            throw;
        }
    }, file, line, column);
}
inline Task<StringResult<std::shared_ptr<WebSocket>>> websocket_connect(
    const std::string& address, Duration timeout, const std::string& file, int line, int column) {
    if (timeout.milliseconds <= 0)
        return make_io_task<StringResult<std::shared_ptr<WebSocket>>>(
            []() -> StringResult<std::shared_ptr<WebSocket>> {
                return result_error<std::shared_ptr<WebSocket>>("WebSocket timeout must be greater than zero");
            }, file, line, column);
    auto parsed = parse_websocket_url(address);
    if (std::holds_alternative<std::string>(parsed))
        return make_io_task<StringResult<std::shared_ptr<WebSocket>>>(
            [error = std::get<1>(std::move(parsed))]() -> StringResult<std::shared_ptr<WebSocket>> {
                return result_error<std::shared_ptr<WebSocket>>(error);
            }, file, line, column);
    return websocket_connect(std::get<0>(std::move(parsed)), timeout, file, line, column);
}
inline bool websocket_is_open(const std::shared_ptr<WebSocket>& value) {
    return value && value->is_open();
}
inline bool websocket_close(const std::shared_ptr<WebSocket>& value) {
    return value && value->is_open() && value->close();
}
inline std::optional<std::int64_t> websocket_close_code(const std::shared_ptr<WebSocket>& value) {
    if (!value) return std::nullopt;
    std::lock_guard<std::mutex> lock(value->metadata_mutex);
    return value->close_code;
}
inline std::optional<std::string> websocket_close_reason(const std::shared_ptr<WebSocket>& value) {
    if (!value) return std::nullopt;
    std::lock_guard<std::mutex> lock(value->metadata_mutex);
    return value->close_reason;
}
inline Task<StringResult<std::int64_t>> websocket_send(
    const std::shared_ptr<WebSocket>& value, std::vector<std::uint8_t> payload,
    const std::string& file, int line, int column) {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    return make_io_task<StringResult<std::int64_t>>(
        [value, payload = std::move(payload), file, line, column]() {
            return websocket_write_frame(value, 2, payload, file, line, column);
        }, file, line, column);
}
inline Task<StringResult<std::int64_t>> websocket_send_text(
    const std::shared_ptr<WebSocket>& value, std::string text,
    const std::string& file, int line, int column) {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    return make_io_task<StringResult<std::int64_t>>(
        [value, payload = bytes_from_string(text), file, line, column]() {
            return websocket_write_frame(value, 1, payload, file, line, column);
        }, file, line, column);
}

struct NativeWebSocketFrame { int opcode = 0; bool final = true; std::vector<std::uint8_t> payload; };
inline StringResult<std::optional<NativeWebSocketFrame>> websocket_next_frame(WebSocket& socket) {
    auto& data = socket.receive_buffer;
    if (data.size() < 2)
        return result_ok<std::optional<NativeWebSocketFrame>>(std::nullopt);
    const auto first = data[0], second = data[1];
    const bool final = (first & 0x80) != 0;
    if (first & 0x70)
        return result_error<std::optional<NativeWebSocketFrame>>("WebSocket reserved bits are not supported");
    const int opcode = first & 0x0f;
    if (!(opcode == 0 || opcode == 1 || opcode == 2 || opcode == 8 || opcode == 9 || opcode == 10))
        return result_error<std::optional<NativeWebSocketFrame>>("invalid WebSocket opcode");
    if (second & 0x80)
        return result_error<std::optional<NativeWebSocketFrame>>("server WebSocket frames must not be masked");
    const auto length_code = second & 0x7f;
    std::size_t offset = 2;
    std::uint64_t length = length_code;
    if (length_code == 126) {
        if (data.size() < offset + 2) return result_ok<std::optional<NativeWebSocketFrame>>(std::nullopt);
        length = (static_cast<std::uint64_t>(data[offset]) << 8) | data[offset + 1];
        if (length < 126)
            return result_error<std::optional<NativeWebSocketFrame>>("non-canonical WebSocket payload length");
        offset += 2;
    } else if (length_code == 127) {
        if (data.size() < offset + 8) return result_ok<std::optional<NativeWebSocketFrame>>(std::nullopt);
        length = 0;
        for (int index = 0; index < 8; ++index) length = (length << 8) | data[offset + index];
        if (length < 65536 || (length & (1ULL << 63)))
            return result_error<std::optional<NativeWebSocketFrame>>("invalid WebSocket payload length");
        offset += 8;
    }
    if (length > MAX_NATIVE_WEBSOCKET_FRAME_BYTES)
        return result_error<std::optional<NativeWebSocketFrame>>("WebSocket frame exceeds size limit");
    if (opcode >= 8 && (!final || length > 125))
        return result_error<std::optional<NativeWebSocketFrame>>("invalid WebSocket control frame");
    if (data.size() - offset < static_cast<std::size_t>(length))
        return result_ok<std::optional<NativeWebSocketFrame>>(std::nullopt);
    NativeWebSocketFrame frame;
    frame.opcode = opcode;
    frame.final = final;
    frame.payload.assign(data.begin() + static_cast<std::ptrdiff_t>(offset),
                         data.begin() + static_cast<std::ptrdiff_t>(offset + length));
    data.erase(data.begin(), data.begin() + static_cast<std::ptrdiff_t>(offset + length));
    return result_ok<std::optional<NativeWebSocketFrame>>(std::move(frame));
}
inline StringResult<std::optional<NativeWebSocketFrame>> websocket_next_event(WebSocket& socket) {
    for (;;) {
        auto decoded = websocket_next_frame(socket);
        if (std::holds_alternative<std::string>(decoded))
            return result_error<std::optional<NativeWebSocketFrame>>(std::get<1>(std::move(decoded)));
        auto frame = std::get<0>(std::move(decoded));
        if (!frame) return result_ok<std::optional<NativeWebSocketFrame>>(std::nullopt);
        if (frame->opcode >= 8) {
            if (frame->opcode == 8) {
                if (frame->payload.size() == 1)
                    return result_error<std::optional<NativeWebSocketFrame>>("invalid WebSocket close payload");
                if (frame->payload.size() >= 2) {
                    const int code = static_cast<int>((static_cast<unsigned int>(frame->payload[0]) << 8) | frame->payload[1]);
                    const bool valid_code = (code >= 1000 && code <= 1014 && code != 1004 && code != 1005 && code != 1006)
                        || (code >= 3000 && code <= 4999);
                    if (!valid_code)
                        return result_error<std::optional<NativeWebSocketFrame>>("invalid WebSocket close code");
                    std::vector<std::uint8_t> reason(frame->payload.begin() + 2, frame->payload.end());
                    if (!valid_utf8(reason))
                        return result_error<std::optional<NativeWebSocketFrame>>("WebSocket close reason is not valid UTF-8");
                }
            }
            return result_ok<std::optional<NativeWebSocketFrame>>(std::move(frame));
        }
        if (frame->opcode == 0) {
            if (!socket.fragment_opcode)
                return result_error<std::optional<NativeWebSocketFrame>>("unexpected WebSocket continuation frame");
            if (socket.fragment.size() + frame->payload.size() > MAX_NATIVE_WEBSOCKET_MESSAGE_BYTES)
                return result_error<std::optional<NativeWebSocketFrame>>("WebSocket message exceeds size limit");
            socket.fragment.insert(socket.fragment.end(), frame->payload.begin(), frame->payload.end());
            if (!frame->final) continue;
            frame->opcode = *socket.fragment_opcode;
            frame->payload = std::move(socket.fragment);
            socket.fragment.clear();
            socket.fragment_opcode.reset();
            if (frame->opcode == 1 && !valid_utf8(frame->payload))
                return result_error<std::optional<NativeWebSocketFrame>>("WebSocket text message is not valid UTF-8");
            return result_ok<std::optional<NativeWebSocketFrame>>(std::move(frame));
        }
        if (socket.fragment_opcode)
            return result_error<std::optional<NativeWebSocketFrame>>("new WebSocket data frame before fragmented message ended");
        if (frame->final) {
            if (frame->opcode == 1 && !valid_utf8(frame->payload))
                return result_error<std::optional<NativeWebSocketFrame>>("WebSocket text message is not valid UTF-8");
            return result_ok<std::optional<NativeWebSocketFrame>>(std::move(frame));
        }
        socket.fragment_opcode = frame->opcode;
        socket.fragment.clear();
        if (frame->payload.size() > MAX_NATIVE_WEBSOCKET_MESSAGE_BYTES)
            return result_error<std::optional<NativeWebSocketFrame>>("WebSocket message exceeds size limit");
        socket.fragment.insert(socket.fragment.end(), frame->payload.begin(), frame->payload.end());
    }
}
inline StringResult<std::optional<WebSocketMessage>> websocket_receive_impl(
    const std::shared_ptr<WebSocket>& value, std::int64_t timeout, bool keep_open_on_timeout,
    const std::string& file, int line, int column) {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    if (timeout <= 0) return result_error<std::optional<WebSocketMessage>>(
        keep_open_on_timeout ? "WebSocket receive timeout must be greater than zero"
                             : "WebSocket operation timed out");
    std::unique_lock<std::mutex> receive_lock(value->receive_mutex, std::try_to_lock);
    if (!receive_lock.owns_lock())
        return result_error<std::optional<WebSocketMessage>>("WebSocket already has a pending receive");
    for (;;) {
        auto event_result = websocket_next_event(*value);
        if (std::holds_alternative<std::string>(event_result)) {
            value->close();
            return result_error<std::optional<WebSocketMessage>>(std::get<1>(std::move(event_result)));
        }
        auto event = std::get<0>(std::move(event_result));
        if (event) {
            if (event->opcode == 1 || event->opcode == 2) {
                WebSocketMessage message{event->opcode, std::move(event->payload)};
                return result_ok<std::optional<WebSocketMessage>>(std::optional<WebSocketMessage>{std::move(message)});
            }
            if (event->opcode == 10) continue;
            if (event->opcode == 9) {
                auto pong = websocket_write_frame(value, 10, event->payload, file, line, column, "control frame write");
                if (std::holds_alternative<std::string>(pong)) {
                    value->close();
                    return result_error<std::optional<WebSocketMessage>>("WebSocket control frame write failed");
                }
                continue;
            }
            if (event->opcode == 8) {
                std::optional<std::int64_t> code;
                std::string reason;
                if (event->payload.size() >= 2) {
                    code = static_cast<std::int64_t>(
                        (static_cast<unsigned int>(event->payload[0]) << 8) | event->payload[1]);
                    std::vector<std::uint8_t> reason_bytes(event->payload.begin() + 2, event->payload.end());
                    if (!reason_bytes.empty())
                        reason.assign(reinterpret_cast<const char*>(reason_bytes.data()), reason_bytes.size());
                }
                {
                    std::lock_guard<std::mutex> lock(value->metadata_mutex);
                    value->close_code = code;
                    value->close_reason = reason;
                }
                auto echoed = websocket_write_frame(value, 8, event->payload, file, line, column, "close handshake");
                value->close();
                if (std::holds_alternative<std::string>(echoed))
                    return result_error<std::optional<WebSocketMessage>>("WebSocket close handshake failed");
                return result_ok<std::optional<WebSocketMessage>>(std::nullopt);
            }
            continue;
        }
        auto reading = websocket_transport_read(value, 65536, file, line, column);
        std::optional<WebSocketReadResult> completed;
        if (!websocket_await_timed(std::move(reading), timeout, file, line, column, completed)) {
            if (!keep_open_on_timeout) value->close();
            return result_error<std::optional<WebSocketMessage>>(
                keep_open_on_timeout ? "WebSocket receive timed out" : "WebSocket operation timed out");
        }
        if (!completed || std::holds_alternative<std::string>(*completed)) {
            value->close();
            return result_error<std::optional<WebSocketMessage>>(
                "WebSocket operation failed: " +
                (completed ? std::get<1>(*completed) : std::string("invalid read completion")));
        }
        auto bytes = std::get<0>(*completed);
        if (!bytes) {
            value->close();
            return result_error<std::optional<WebSocketMessage>>("WebSocket connection closed without a close frame");
        }
        if (value->receive_buffer.size() + bytes->size() > MAX_NATIVE_WEBSOCKET_MESSAGE_BYTES + 14) {
            value->close();
            return result_error<std::optional<WebSocketMessage>>("WebSocket receive buffer exceeds size limit");
        }
        value->receive_buffer.insert(value->receive_buffer.end(), bytes->begin(), bytes->end());
    }
}
inline Task<WebSocketReceiveResult> websocket_receive(
    const std::shared_ptr<WebSocket>& value, const std::string& file, int line, int column) {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    return make_io_task<WebSocketReceiveResult>(
        [value, file, line, column]() {
            return websocket_receive_impl(value, value->operation_timeout_milliseconds, false, file, line, column);
        }, file, line, column);
}
inline Task<WebSocketReceiveResult> websocket_receive_within(
    const std::shared_ptr<WebSocket>& value, Duration timeout,
    const std::string& file, int line, int column) {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    return make_io_task<WebSocketReceiveResult>(
        [value, timeout, file, line, column]() {
            return websocket_receive_impl(value, timeout.milliseconds, true, file, line, column);
        }, file, line, column);
}
inline Task<StringResult<bool>> websocket_close_gracefully(
    const std::shared_ptr<WebSocket>& value, const std::string& file, int line, int column) {
    if (!value || !value->is_open()) fail(file, line, column, "WebSocket is closed");
    return make_io_task<StringResult<bool>>([value, file, line, column]() -> StringResult<bool> {
        std::vector<std::uint8_t> close_payload{0x03, 0xe8};
        auto sent = websocket_write_frame(value, 8, close_payload, file, line, column, "close");
        if (std::holds_alternative<std::string>(sent)) {
            value->close();
            const auto error = std::get<1>(std::move(sent));
            return result_error<bool>(error == "WebSocket close timed out"
                ? "WebSocket close timed out" : "WebSocket close failed: " + error);
        }
        for (;;) {
            auto received = websocket_receive_impl(
                value, value->operation_timeout_milliseconds, false, file, line, column);
            if (std::holds_alternative<std::string>(received)) {
                value->close();
                const auto error = std::get<1>(std::move(received));
                if (error == "WebSocket operation timed out")
                    return result_error<bool>("WebSocket close timed out");
                return result_error<bool>("WebSocket close failed: " + error);
            }
            if (!std::get<0>(received)) return result_ok<bool>(true);
        }
    }, file, line, column);
}
#endif

inline StringResult<std::int64_t> run_tasks(const std::string& file, int line, int column) {
    if (actor_handler_active())
        throw Failure(file, line, column, "runTasks cannot be called from an actor message handler");
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
    std::string scheduler_failure;
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
            const bool unstarted = std::any_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return !task->started.load(std::memory_order_acquire);
            });
            const bool all_finished = std::all_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return task->done.load(std::memory_order_acquire);
            });
            const bool waiting_reply = std::any_of(
                scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                    return task->waiting_for_actor_reply.load(std::memory_order_acquire);
                });
            const bool all_waiting_without_deadline = std::any_of(
                scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                    return !task->done.load(std::memory_order_acquire);
                }) && std::all_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                    return task->done.load(std::memory_order_acquire)
                        || (task->waiting_for_actor_reply.load(std::memory_order_acquire)
                            && !task->actor_reply_wait_has_deadline.load(std::memory_order_acquire)
                            && !task->is_cancelled());
                });
            const bool actor_ready = native_actor_message_ready();
            return unstarted || all_finished || (waiting_reply && actor_ready)
                || (all_waiting_without_deadline && !actor_ready);
        });
        const bool actor_ready = native_actor_message_ready();
        const bool waiting_reply = std::any_of(
            scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return task->waiting_for_actor_reply.load(std::memory_order_acquire);
            });
        const bool all_waiting_without_deadline = std::any_of(
            scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return !task->done.load(std::memory_order_acquire);
            }) && std::all_of(scheduler.tasks.begin(), scheduler.tasks.end(), [](const auto& task) {
                return task->done.load(std::memory_order_acquire)
                    || (task->waiting_for_actor_reply.load(std::memory_order_acquire)
                        && !task->actor_reply_wait_has_deadline.load(std::memory_order_acquire)
                        && !task->is_cancelled());
            });
        const bool deadlocked = all_waiting_without_deadline && !actor_ready;
        const bool pump_actor = waiting_reply && actor_ready;
        const auto active_tasks = scheduler.tasks;
        lock.unlock();
        if (deadlocked) {
            if (scheduler_failure.empty())
                scheduler_failure = "task scheduler has an unresolved actor reply with no runnable work or deadline";
            for (const auto& task : active_tasks) {
                if (!task->done.load(std::memory_order_acquire)) task->request_cancel();
            }
        }
        if (pump_actor) {
            std::unique_lock<NativeExecutionMutex> actor_turn_lock(scheduler.execution_mutex);
            ActiveExecutionLockScope actor_execution_scope(actor_turn_lock);
            const auto actor_result = run_actors(file, line, column, false, 1, false);
            if (std::holds_alternative<std::string>(actor_result) && scheduler_failure.empty())
                scheduler_failure = std::get<1>(actor_result);
        }
    }
    for (const auto& task : completed_tasks) task->join();
    if (requested_shutdown_signal != 0) check_shutdown(file, line, column);
    if (!scheduler_failure.empty()) return result_error<std::int64_t>(std::move(scheduler_failure));
    for (const auto& task : completed_tasks) {
        if (task->owner) continue;
        const auto failure = task->failure_message_for_run();
        if (!failure.empty()) {
            const auto reply_failure = task->actor_reply_failure_message();
            if (!reply_failure.empty() && failure == reply_failure)
                return result_error<std::int64_t>(reply_failure);
            return result_error<std::int64_t>(failure);
        }
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

inline bool http_token_character(unsigned char value) {
    return (value >= 48 && value <= 57) || (value >= 65 && value <= 90)
        || (value >= 97 && value <= 122) || value == 33 || value == 35 || value == 36
        || value == 37 || value == 38 || value == 39 || value == 42 || value == 43
        || value == 45 || value == 46 || value == 94 || value == 95 || value == 96
        || value == 124 || value == 126;
}
inline std::string http_lower_ascii(std::string value) {
    for (auto& character : value) {
        const auto byte = static_cast<unsigned char>(character);
        if (byte >= 'A' && byte <= 'Z') character = static_cast<char>(byte - 'A' + 'a');
    }
    return value;
}
inline std::string http_trim_ows(std::string value) {
    std::size_t begin = 0, end = value.size();
    while (begin < end && (value[begin] == ' ' || value[begin] == '\t')) ++begin;
    while (end > begin && (value[end - 1] == ' ' || value[end - 1] == '\t')) --end;
    return value.substr(begin, end - begin);
}
inline bool http_header_name_valid(const std::string& name) {
    return !name.empty() && std::all_of(name.begin(), name.end(), [](unsigned char c) {
        return http_token_character(c);
    });
}
inline std::vector<std::uint32_t> http_utf8_codepoints(const std::string& value, bool& valid) {
    std::vector<std::uint8_t> bytes(value.begin(), value.end());
    std::vector<std::uint32_t> result;
    valid = valid_utf8(bytes);
    if (!valid) return result;
    for (std::size_t i = 0; i < value.size();) {
        const auto first = static_cast<unsigned char>(value[i]);
        std::uint32_t cp = 0;
        std::size_t count = 1;
        if (first < 0x80) cp = first;
        else if ((first & 0xe0) == 0xc0) { cp = first & 0x1f; count = 2; }
        else if ((first & 0xf0) == 0xe0) { cp = first & 0x0f; count = 3; }
        else { cp = first & 0x07; count = 4; }
        for (std::size_t j = 1; j < count; ++j)
            cp = (cp << 6) | (static_cast<unsigned char>(value[i + j]) & 0x3f);
        result.push_back(cp);
        i += count;
    }
    return result;
}
inline bool http_header_value_valid(const std::string& value) {
    bool valid = false;
    const auto codepoints = http_utf8_codepoints(value, valid);
    if (!valid) return false;
    for (const auto cp : codepoints)
        if (cp > 255 || cp <= 8 || cp == 10 || cp == 13 || (cp >= 14 && cp <= 31) || cp == 127)
            return false;
    return true;
}
inline std::string http_latin1_to_utf8(const std::string& value) {
    std::string result;
    result.reserve(value.size() * 2);
    for (const auto raw : value) {
        const auto byte = static_cast<unsigned char>(raw);
        if (byte < 0x80) result.push_back(static_cast<char>(byte));
        else {
            result.push_back(static_cast<char>(0xc0 | (byte >> 6)));
            result.push_back(static_cast<char>(0x80 | (byte & 0x3f)));
        }
    }
    return result;
}
inline bool http_utf8_to_latin1(const std::string& value, std::string& result, std::string& error) {
    bool valid = false;
    const auto codepoints = http_utf8_codepoints(value, valid);
    if (!valid) { error = "HTTP header value must use the ISO-8859-1 range"; return false; }
    result.clear();
    result.reserve(codepoints.size());
    for (const auto cp : codepoints) {
        if (cp > 255) { error = "HTTP header value must use the ISO-8859-1 range"; return false; }
        result.push_back(static_cast<char>(cp));
    }
    return true;
}
inline bool http_validate_header(const std::string& name, const std::string& value,
                                 std::string& normalized, std::string& error) {
    if (!http_header_name_valid(name)) { error = "invalid HTTP header name"; return false; }
    if (!http_header_value_valid(value)) {
        bool valid = false; (void)http_utf8_codepoints(value, valid);
        error = valid ? "invalid HTTP header value" : "HTTP header value must use the ISO-8859-1 range";
        return false;
    }
    normalized = http_lower_ascii(name);
    return true;
}
inline StringResult<bool> http_headers_add_internal(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name, const std::string& value) {
    if (!headers) return result_error<bool>("invalid HTTP headers");
    std::string normalized, error;
    if (!http_validate_header(name, value, normalized, error)) return result_error<bool>(std::move(error));
    headers->entries.emplace_back(std::move(normalized), http_trim_ows(value));
    return result_ok<bool>(true);
}
inline std::int64_t http_headers_remove_internal(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& key) {
    if (!headers) return 0;
    const auto old = headers->entries.size();
    headers->entries.erase(std::remove_if(headers->entries.begin(), headers->entries.end(),
        [&key](const auto& entry) { return entry.first == key; }), headers->entries.end());
    return static_cast<std::int64_t>(old - headers->entries.size());
}
inline StringResult<bool> http_headers_set_internal(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name, const std::string& value) {
    std::string key, error;
    if (!http_validate_header(name, value, key, error)) return result_error<bool>(std::move(error));
    http_headers_remove_internal(headers, key);
    return http_headers_add_internal(headers, name, value);
}
inline StringResult<bool> http_headers_add(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name, const std::string& value,
    const std::string&, int, int) {
    return http_headers_add_internal(headers, name, value);
}
inline StringResult<bool> http_headers_set(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name, const std::string& value,
    const std::string&, int, int) {
    return http_headers_set_internal(headers, name, value);
}
inline std::vector<std::string> http_headers_values_internal(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name,
    const std::string& file, int line, int column) {
    std::string key, error;
    if (!http_validate_header(name, "", key, error)) fail(file, line, column, error);
    std::vector<std::string> result;
    if (!headers) return result;
    for (const auto& entry : headers->entries)
        if (entry.first == key) result.push_back(entry.second);
    return result;
}
inline std::int64_t http_headers_remove(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name,
    const std::string& file, int line, int column) {
    std::string key, error;
    if (!http_validate_header(name, "", key, error)) fail(file, line, column, error);
    return http_headers_remove_internal(headers, key);
}
inline std::optional<std::string> http_headers_get(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name,
    const std::string& file, int line, int column) {
    const auto values = http_headers_values_internal(headers, name, file, line, column);
    if (values.empty()) return std::nullopt;
    if (http_lower_ascii(name) == "set-cookie") return values.front();
    std::string result;
    for (const auto& value : values) { if (!result.empty()) result += ", "; result += value; }
    return result;
}
inline std::shared_ptr<ListData<std::string>> http_headers_values(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name,
    const std::string& file, int line, int column) {
    auto result = make_list<std::string>();
    result->values = http_headers_values_internal(headers, name, file, line, column);
    return result;
}
inline bool http_headers_contains(
    const std::shared_ptr<HttpHeaders>& headers, const std::string& name,
    const std::string& file, int line, int column) {
    return !http_headers_values_internal(headers, name, file, line, column).empty();
}
inline std::int64_t http_headers_length(const std::shared_ptr<HttpHeaders>& headers,
                                        const std::string&, int, int) {
    return headers ? static_cast<std::int64_t>(headers->entries.size()) : 0;
}
inline std::string http_method_name(const HttpMethod& method) { return method.name; }
inline bool http_method_is_standard(const HttpMethod& method) {
    static const std::array<std::string, 9> standard{
        "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE"};
    return std::find(standard.begin(), standard.end(), method.name) != standard.end();
}
inline std::shared_ptr<HttpHeaders> http_headers() { return std::make_shared<HttpHeaders>(); }
inline StringResult<HttpMethod> http_method(const std::string& value) {
    if (value.empty() || !std::all_of(value.begin(), value.end(), [](unsigned char c) { return http_token_character(c); }))
        return result_error<HttpMethod>("invalid HTTP method");
    return result_ok<HttpMethod>(HttpMethod{value});
}
inline std::string http_url_scheme(const HttpUrl& url) { return url.scheme; }
inline std::string http_url_host(const HttpUrl& url) { return url.host; }
inline std::int64_t http_url_port(const HttpUrl& url) { return url.port; }
inline std::string http_url_target(const HttpUrl& url) { return url.target; }
inline std::string http_url_authority(const HttpUrl& url) {
    std::string value = url.host.find(':') == std::string::npos ? url.host : "[" + url.host + "]";
    if (url.port != (url.scheme == "https" ? 443 : 80)) value += ":" + std::to_string(url.port);
    return value;
}
inline bool http_valid_ipv4(const std::string& value) {
    std::size_t start = 0, groups = 0;
    while (start <= value.size()) {
        const auto end = value.find('.', start);
        const auto stop = end == std::string::npos ? value.size() : end;
        if (start == stop || stop - start > 3) return false;
        unsigned int part = 0;
        for (std::size_t i = start; i < stop; ++i) {
            const auto c = static_cast<unsigned char>(value[i]);
            if (c < '0' || c > '9') return false;
            part = part * 10 + (c - '0');
        }
        if (part > 255) return false;
        ++groups;
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return groups == 4;
}
inline bool http_valid_ipv6(const std::string& value) {
    if (value.empty() || value.find('%') != std::string::npos || value.find(":::") != std::string::npos)
        return false;
    const auto compression = value.find("::");
    const bool compressed = compression != std::string::npos;
    if (compressed && value.find("::", compression + 2) != std::string::npos) return false;
    const auto before = compressed ? value.substr(0, compression) : value;
    const auto after = compressed ? value.substr(compression + 2) : std::string();
    std::size_t groups = 0;
    auto count = [&groups](const std::string& side, bool allow_ipv4) {
        if (side.empty()) return true;
        std::size_t start = 0;
        while (start <= side.size()) {
            const auto end = side.find(':', start);
            const auto stop = end == std::string::npos ? side.size() : end;
            const auto part = side.substr(start, stop - start);
            if (part.empty()) return false;
            if (part.find('.') != std::string::npos) {
                if (!allow_ipv4 || end != std::string::npos || !http_valid_ipv4(part)) return false;
                groups += 2;
            } else {
                if (part.size() > 4) return false;
                for (const auto c : part) {
                    const auto byte = static_cast<unsigned char>(c);
                    if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f')
                          || (byte >= 'A' && byte <= 'F'))) return false;
                }
                ++groups;
            }
            if (end == std::string::npos) break;
            start = end + 1;
        }
        return true;
    };
    if (!count(before, !compressed) || !count(after, true) || groups > 8) return false;
    return compressed ? groups < 8 : groups == 8;
}
inline bool http_valid_hostname(const std::string& host) {
    if (host.empty() || host.size() > 253) return false;
    const auto checked = host.back() == '.' ? host.substr(0, host.size() - 1) : host;
    if (checked.empty()) return false;
    std::size_t start = 0;
    while (start < checked.size()) {
        const auto end = checked.find('.', start);
        const auto stop = end == std::string::npos ? checked.size() : end;
        if (stop == start || stop - start > 63 || checked[start] == '-' || checked[stop - 1] == '-') return false;
        for (std::size_t i = start; i < stop; ++i) {
            const auto c = static_cast<unsigned char>(checked[i]);
            if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                  || (c >= '0' && c <= '9') || c == '-')) return false;
        }
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return true;
}
inline bool http_parse_port(const std::string& value, std::int64_t& port) {
    if (value.empty() || !std::all_of(value.begin(), value.end(), [](unsigned char c) { return c >= '0' && c <= '9'; }))
        return false;
    std::uint64_t parsed = 0;
    const auto converted = std::from_chars(value.data(), value.data() + value.size(), parsed);
    if (converted.ec != std::errc{} || converted.ptr != value.data() + value.size() || parsed < 1 || parsed > 65535)
        return false;
    port = static_cast<std::int64_t>(parsed);
    return true;
}
inline bool http_valid_target(const std::string& target) {
    if (target == "*") return true;
    if (target.empty() || target.front() != '/' || target.find('#') != std::string::npos) return false;
    for (std::size_t i = 0; i < target.size(); ++i) {
        const auto byte = static_cast<unsigned char>(target[i]);
        if (byte < 33 || byte > 126) return false;
        if (byte == '%') {
            if (i + 2 >= target.size() || !std::isxdigit(static_cast<unsigned char>(target[i + 1]))
                || !std::isxdigit(static_cast<unsigned char>(target[i + 2]))) return false;
            i += 2;
        }
    }
    return true;
}
inline StringResult<HttpUrl> http_url(const std::string& input) {
    const auto separator = input.find("://");
    if (separator == std::string::npos)
        return result_error<HttpUrl>("HTTP URL must include a scheme and authority");
    const auto scheme = http_lower_ascii(input.substr(0, separator));
    if (scheme != "http" && scheme != "https")
        return result_error<HttpUrl>("HTTP URL scheme must be http or https");
    const auto remainder = input.substr(separator + 3);
    if (remainder.find('#') != std::string::npos)
        return result_error<HttpUrl>("HTTP request URL must not contain a fragment");
    const auto slash = remainder.find('/');
    const auto question = remainder.find('?');
    std::size_t authority_end = remainder.size();
    if (slash != std::string::npos) authority_end = std::min(authority_end, slash);
    if (question != std::string::npos) authority_end = std::min(authority_end, question);
    const auto authority = remainder.substr(0, authority_end);
    const auto suffix = remainder.substr(authority_end);
    if (authority.empty() || authority.find('@') != std::string::npos)
        return result_error<HttpUrl>("invalid HTTP URL authority");
    HttpUrl url;
    url.scheme = scheme;
    url.port = scheme == "https" ? 443 : 80;
    if (authority.front() == '[') {
        const auto close = authority.find(']');
        if (close == std::string::npos) return result_error<HttpUrl>("invalid IPv6 URL authority");
        url.host = authority.substr(1, close - 1);
        if (url.host.find('%') != std::string::npos)
            return result_error<HttpUrl>("IPv6 URL scope identifiers are not supported");
        if (!http_valid_ipv6(url.host)) return result_error<HttpUrl>("invalid IPv6 URL authority");
        const auto remainder_authority = authority.substr(close + 1);
        if (!remainder_authority.empty()
            && (remainder_authority.front() != ':' || !http_parse_port(remainder_authority.substr(1), url.port)))
            return result_error<HttpUrl>("invalid HTTP URL port");
    } else {
        if (std::count(authority.begin(), authority.end(), ':') > 1)
            return result_error<HttpUrl>("IPv6 URL hosts must be enclosed in brackets");
        const auto colon = authority.find(':');
        url.host = authority.substr(0, colon);
        if (colon != std::string::npos && !http_parse_port(authority.substr(colon + 1), url.port))
            return result_error<HttpUrl>("invalid HTTP URL port");
        if (!http_valid_hostname(url.host)) return result_error<HttpUrl>("invalid HTTP URL hostname");
    }
    url.host = http_lower_ascii(url.host);
    if (suffix.empty()) url.target = "/";
    else if (suffix.front() == '?') url.target = "/" + suffix;
    else if (suffix.front() == '/') url.target = suffix;
    else return result_error<HttpUrl>("invalid HTTP URL target");
    if (!http_valid_target(url.target))
        return result_error<HttpUrl>("HTTP request target must be origin-form without a fragment");
    return result_ok<HttpUrl>(std::move(url));
}
inline StringResult<std::shared_ptr<HttpRequest>> http_request(
    HttpMethod method, std::string target, std::shared_ptr<HttpHeaders> headers,
    std::vector<std::uint8_t> body) {
    if (!http_valid_target(target))
        return result_error<std::shared_ptr<HttpRequest>>("HTTP request target must be origin-form without a fragment");
    if (!headers) return result_error<std::shared_ptr<HttpRequest>>("invalid HTTP request values");
    auto result = std::make_shared<HttpRequest>();
    result->method = std::move(method);
    result->target = std::move(target);
    result->headers = std::move(headers);
    result->body = std::move(body);
    return result_ok<std::shared_ptr<HttpRequest>>(std::move(result));
}
inline HttpMethod http_request_method(const std::shared_ptr<HttpRequest>& value) {
    return value ? value->method : HttpMethod{};
}
inline std::string http_request_target(const std::shared_ptr<HttpRequest>& value) {
    return value ? value->target : std::string();
}
inline std::shared_ptr<HttpHeaders> http_request_headers(const std::shared_ptr<HttpRequest>& value) {
    return value ? value->headers : std::make_shared<HttpHeaders>();
}
inline std::vector<std::uint8_t> http_request_body(const std::shared_ptr<HttpRequest>& value) {
    return value ? value->body : std::vector<std::uint8_t>{};
}
inline std::int64_t http_response_status(const std::shared_ptr<HttpResponse>& value) {
    return value ? value->status : 0;
}
inline std::shared_ptr<HttpHeaders> http_response_headers(const std::shared_ptr<HttpResponse>& value) {
    return value ? value->headers : std::make_shared<HttpHeaders>();
}
inline std::vector<std::uint8_t> http_response_body(const std::shared_ptr<HttpResponse>& value) {
    return value ? value->body : std::vector<std::uint8_t>{};
}
constexpr std::size_t HTTP_REQUEST_LINE_LIMIT = 8192;
constexpr std::size_t HTTP_HEADER_BYTES_LIMIT = 32768;
constexpr std::size_t HTTP_HEADER_COUNT_LIMIT = 100;
constexpr std::size_t HTTP_BODY_BYTES_LIMIT = 1048576;
constexpr std::size_t HTTP_BUFFER_BYTES_LIMIT =
    HTTP_REQUEST_LINE_LIMIT + HTTP_HEADER_BYTES_LIMIT + HTTP_BODY_BYTES_LIMIT + HTTP_HEADER_BYTES_LIMIT + 4;
struct HttpDecodedResponse { std::shared_ptr<HttpResponse> value; std::size_t consumed = 0; };
using HttpDecodeResult = StringResult<std::optional<HttpDecodedResponse>>;
inline HttpDecodeResult http_decode_response(
    const std::vector<std::uint8_t>& input, bool eof, const std::string& method,
    const std::string& file, int line, int column) {
    const std::string bytes = input.empty()
        ? std::string() : std::string(reinterpret_cast<const char*>(input.data()), input.size());
    const auto header_end = bytes.find("\r\n\r\n");
    if (header_end == std::string::npos) {
        if (input.size() > HTTP_REQUEST_LINE_LIMIT + HTTP_HEADER_BYTES_LIMIT + 4)
            return result_error<std::optional<HttpDecodedResponse>>("HTTP headers exceed configured size limit");
        if (eof && !input.empty())
            return result_error<std::optional<HttpDecodedResponse>>("connection closed during HTTP headers");
        return result_ok<std::optional<HttpDecodedResponse>>(std::nullopt);
    }
    if (header_end + 4 > HTTP_REQUEST_LINE_LIMIT + HTTP_HEADER_BYTES_LIMIT + 4)
        return result_error<std::optional<HttpDecodedResponse>>("HTTP headers exceed configured size limit");
    const auto head = bytes.substr(0, header_end);
    std::vector<std::string> lines;
    std::size_t start = 0;
    for (;;) {
        const auto end = head.find("\r\n", start);
        lines.push_back(head.substr(start, end == std::string::npos ? std::string::npos : end - start));
        if (end == std::string::npos) break;
        start = end + 2;
    }
    if (lines.empty() || lines.front().size() > HTTP_REQUEST_LINE_LIMIT)
        return result_error<std::optional<HttpDecodedResponse>>("HTTP start line exceeds configured size limit");
    if (lines.size() - 1 > HTTP_HEADER_COUNT_LIMIT)
        return result_error<std::optional<HttpDecodedResponse>>("HTTP header count exceeds configured limit");
    const auto first_space = lines.front().find(' ');
    const auto second_space = first_space == std::string::npos ? std::string::npos : lines.front().find(' ', first_space + 1);
    if (first_space == std::string::npos
        || (lines.front().substr(0, first_space) != "HTTP/1.0"
            && lines.front().substr(0, first_space) != "HTTP/1.1")
        || !std::all_of(lines.front().begin(), lines.front().end(), [](unsigned char c) { return c < 0x80; }))
        return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP status line");
    const auto code = lines.front().substr(first_space + 1,
        (second_space == std::string::npos ? lines.front().size() : second_space) - first_space - 1);
    if (code.size() != 3 || !std::all_of(code.begin(), code.end(), [](unsigned char c) { return c >= '0' && c <= '9'; }))
        return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP status code");
    int status = 0;
    if (std::from_chars(code.data(), code.data() + code.size(), status).ec != std::errc{} || status < 100 || status > 599)
        return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP status code");
    auto headers = std::make_shared<HttpHeaders>();
    for (std::size_t index = 1; index < lines.size(); ++index) {
        const auto& field = lines[index];
        if (field.size() > HTTP_HEADER_BYTES_LIMIT)
            return result_error<std::optional<HttpDecodedResponse>>("HTTP header exceeds configured size limit");
        if (field.empty() || field.front() == ' ' || field.front() == '\t')
            return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP header line");
        const auto colon = field.find(':');
        if (colon == std::string::npos)
            return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP header line");
        const auto name = field.substr(0, colon);
        const auto raw_value = field.substr(colon + 1);
        if (!http_header_name_valid(name)
            || !std::all_of(raw_value.begin(), raw_value.end(), [](unsigned char c) {
                return !(c <= 8 || c == 10 || c == 13 || (c >= 14 && c <= 31) || c == 127);
            }))
            return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP header line");
        headers->entries.emplace_back(http_lower_ascii(name), http_trim_ows(http_latin1_to_utf8(raw_value)));
    }
    const auto lengths = http_headers_values_internal(headers, "content-length", file, line, column);
    const auto encodings = http_headers_values_internal(headers, "transfer-encoding", file, line, column);
    std::optional<std::uint64_t> content_length;
    if (!lengths.empty()) {
        if (lengths.size() != 1 || lengths.front().empty()
            || !std::all_of(lengths.front().begin(), lengths.front().end(), [](unsigned char c) { return c >= '0' && c <= '9'; }))
            return result_error<std::optional<HttpDecodedResponse>>("invalid Content-Length");
        std::uint64_t length = 0;
        const auto parsed = std::from_chars(lengths.front().data(), lengths.front().data() + lengths.front().size(), length);
        if (parsed.ec != std::errc{} || parsed.ptr != lengths.front().data() + lengths.front().size())
            return result_error<std::optional<HttpDecodedResponse>>("invalid Content-Length");
        if (length > 16'777'216)
            return result_error<std::optional<HttpDecodedResponse>>("HTTP body exceeds configured size limit");
        content_length = length;
    }
    std::vector<std::string> coding;
    for (const auto& value : encodings) {
        std::size_t pos = 0;
        for (;;) {
            const auto end = value.find(',', pos);
            coding.push_back(http_lower_ascii(http_trim_ows(value.substr(pos, end == std::string::npos ? std::string::npos : end - pos))));
            if (end == std::string::npos) break;
            pos = end + 1;
        }
    }
    if (!encodings.empty() && coding != std::vector<std::string>{"chunked"})
        return result_error<std::optional<HttpDecodedResponse>>("unsupported HTTP transfer encoding");
    const bool chunked = coding == std::vector<std::string>{"chunked"};
    if (content_length && chunked)
        return result_error<std::optional<HttpDecodedResponse>>("ambiguous HTTP message framing");
    const auto body_start = header_end + 4;
    std::vector<std::uint8_t> body;
    std::size_t message_end = body_start;
    const bool no_body = method == "HEAD" || (status >= 100 && status < 200) || status == 204 || status == 304;
    if (no_body) {
        // Empty body is determined solely by the status/method semantics.
    } else if (chunked) {
        std::size_t cursor = body_start, overhead = 0;
        for (;;) {
            const auto end = bytes.find("\r\n", cursor);
            if (end == std::string::npos) {
                if (overhead + bytes.size() - cursor > HTTP_HEADER_BYTES_LIMIT)
                    return result_error<std::optional<HttpDecodedResponse>>("HTTP chunk metadata exceeds configured size limit");
                if (eof) return result_error<std::optional<HttpDecodedResponse>>("connection closed during HTTP chunk metadata");
                return result_ok<std::optional<HttpDecodedResponse>>(std::nullopt);
            }
            const auto chunk_line = bytes.substr(cursor, end - cursor);
            cursor = end + 2;
            overhead += chunk_line.size() + 2;
            if (overhead > HTTP_HEADER_BYTES_LIMIT)
                return result_error<std::optional<HttpDecodedResponse>>("HTTP chunk metadata exceeds configured size limit");
            const auto semicolon = chunk_line.find(';');
            const auto digits = chunk_line.substr(0, semicolon);
            if (digits.empty() || digits.size() > 16
                || !std::all_of(chunk_line.begin(), chunk_line.end(), [](unsigned char c) { return c >= 32 && c <= 126; })
                || !std::all_of(digits.begin(), digits.end(), [](unsigned char c) { return std::isxdigit(c) != 0; }))
                return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP chunk size");
            std::uint64_t length = 0;
            const auto parsed = std::from_chars(digits.data(), digits.data() + digits.size(), length, 16);
            if (parsed.ec != std::errc{} || length > HTTP_BODY_BYTES_LIMIT - body.size())
                return result_error<std::optional<HttpDecodedResponse>>("HTTP body exceeds configured size limit");
            if (length == 0) {
                std::size_t trailer_count = 0;
                for (;;) {
                    const auto trailer_end = bytes.find("\r\n", cursor);
                    if (trailer_end == std::string::npos) {
                        if (overhead + bytes.size() - cursor > HTTP_HEADER_BYTES_LIMIT)
                            return result_error<std::optional<HttpDecodedResponse>>("HTTP chunk metadata exceeds configured size limit");
                        if (eof) return result_error<std::optional<HttpDecodedResponse>>("connection closed during HTTP chunk trailers");
                        return result_ok<std::optional<HttpDecodedResponse>>(std::nullopt);
                    }
                    const auto trailer = bytes.substr(cursor, trailer_end - cursor);
                    cursor = trailer_end + 2;
                    overhead += trailer.size() + 2;
                    if (overhead > HTTP_HEADER_BYTES_LIMIT)
                        return result_error<std::optional<HttpDecodedResponse>>("HTTP chunk metadata exceeds configured size limit");
                    if (trailer.empty()) break;
                    const auto colon = trailer.find(':');
                    if (trailer.front() == ' ' || trailer.front() == '\t' || colon == std::string::npos
                        || !http_header_name_valid(trailer.substr(0, colon))
                        || !http_header_value_valid(http_latin1_to_utf8(trailer.substr(colon + 1))))
                        return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP trailer line");
                    const auto name = http_lower_ascii(trailer.substr(0, colon));
                    if (name == "content-length" || name == "transfer-encoding" || name == "host")
                        return result_error<std::optional<HttpDecodedResponse>>("forbidden HTTP trailer field");
                    if (++trailer_count > HTTP_HEADER_COUNT_LIMIT)
                        return result_error<std::optional<HttpDecodedResponse>>("invalid HTTP trailer line");
                }
                message_end = cursor;
                break;
            }
            const auto end64 = static_cast<std::uint64_t>(cursor) + length;
            if (end64 > bytes.size() || end64 + 2 > bytes.size()) {
                if (eof) return result_error<std::optional<HttpDecodedResponse>>("connection closed during HTTP chunk data");
                return result_ok<std::optional<HttpDecodedResponse>>(std::nullopt);
            }
            const auto data_end = static_cast<std::size_t>(end64);
            if (bytes.compare(data_end, 2, "\r\n") != 0)
                return result_error<std::optional<HttpDecodedResponse>>("HTTP chunk data must end with CRLF");
            body.insert(body.end(), input.begin() + static_cast<std::ptrdiff_t>(cursor),
                        input.begin() + static_cast<std::ptrdiff_t>(data_end));
            overhead += 2;
            if (overhead > HTTP_HEADER_BYTES_LIMIT)
                return result_error<std::optional<HttpDecodedResponse>>("HTTP chunk metadata exceeds configured size limit");
            cursor = data_end + 2;
        }
    } else if (!content_length) {
        if (!eof) {
            if (bytes.size() - body_start > HTTP_BODY_BYTES_LIMIT)
                return result_error<std::optional<HttpDecodedResponse>>("HTTP body exceeds configured size limit");
            return result_ok<std::optional<HttpDecodedResponse>>(std::nullopt);
        }
        const auto length = bytes.size() - body_start;
        if (length > HTTP_BODY_BYTES_LIMIT)
            return result_error<std::optional<HttpDecodedResponse>>("HTTP body exceeds configured size limit");
        body.assign(input.begin() + static_cast<std::ptrdiff_t>(body_start), input.end());
        message_end = input.size();
    } else {
        if (*content_length > HTTP_BODY_BYTES_LIMIT)
            return result_error<std::optional<HttpDecodedResponse>>("HTTP body exceeds configured size limit");
        const auto end64 = static_cast<std::uint64_t>(body_start) + *content_length;
        if (end64 > bytes.size()) {
            if (eof) return result_error<std::optional<HttpDecodedResponse>>("connection closed during HTTP body");
            return result_ok<std::optional<HttpDecodedResponse>>(std::nullopt);
        }
        body.assign(input.begin() + static_cast<std::ptrdiff_t>(body_start),
                    input.begin() + static_cast<std::ptrdiff_t>(end64));
        message_end = static_cast<std::size_t>(end64);
    }
    auto response = std::make_shared<HttpResponse>();
    response->status = status;
    response->headers = std::move(headers);
    response->body = std::move(body);
    return result_ok<std::optional<HttpDecodedResponse>>(HttpDecodedResponse{std::move(response), message_end});
}
inline StringResult<std::vector<std::uint8_t>> http_encode_request(const HttpRequest& request, const HttpUrl& url) {
    if (request.method.name.empty()
        || !std::all_of(request.method.name.begin(), request.method.name.end(), [](unsigned char c) { return http_token_character(c); }))
        return result_error<std::vector<std::uint8_t>>("invalid HTTP method");
    if (!http_valid_target(request.target)) return result_error<std::vector<std::uint8_t>>("invalid HTTP request target");
    if (request.body.size() > HTTP_BODY_BYTES_LIMIT)
        return result_error<std::vector<std::uint8_t>>("HTTP body exceeds configured size limit");
    auto headers = std::make_shared<HttpHeaders>();
    if (request.headers) headers->entries = request.headers->entries;
    auto host = http_headers_set_internal(headers, "Host", http_url_authority(url));
    if (std::holds_alternative<std::string>(host)) return result_error<std::vector<std::uint8_t>>(std::get<1>(std::move(host)));
    auto connection = http_headers_set_internal(headers, "Connection", "close");
    if (std::holds_alternative<std::string>(connection)) return result_error<std::vector<std::uint8_t>>(std::get<1>(std::move(connection)));
    const auto lengths = http_headers_values_internal(headers, "content-length", "<native>", 1, 1);
    if (lengths.empty()) {
        auto set_length = http_headers_set_internal(headers, "Content-Length", std::to_string(request.body.size()));
        if (std::holds_alternative<std::string>(set_length)) return result_error<std::vector<std::uint8_t>>(std::get<1>(std::move(set_length)));
    } else if (lengths.size() != 1 || lengths.front() != std::to_string(request.body.size())) {
        return result_error<std::vector<std::uint8_t>>("Content-Length does not match body size");
    }
    if (!http_headers_values_internal(headers, "transfer-encoding", "<native>", 1, 1).empty())
        return result_error<std::vector<std::uint8_t>>("transfer-encoding is not supported");
    if (headers->entries.size() > HTTP_HEADER_COUNT_LIMIT)
        return result_error<std::vector<std::uint8_t>>("HTTP header count exceeds configured limit");
    const auto start = request.method.name + " " + request.target + " HTTP/1.1\r\n";
    if (start.size() > HTTP_REQUEST_LINE_LIMIT)
        return result_error<std::vector<std::uint8_t>>("HTTP request line exceeds configured size limit");
    std::string encoded_headers;
    for (const auto& entry : headers->entries) {
        if (!http_header_name_valid(entry.first) || !http_header_value_valid(entry.second))
            return result_error<std::vector<std::uint8_t>>("invalid HTTP header line");
        std::string latin, error;
        if (!http_utf8_to_latin1(entry.second, latin, error))
            return result_error<std::vector<std::uint8_t>>(std::move(error));
        encoded_headers += entry.first + ": " + latin + "\r\n";
    }
    if (encoded_headers.size() > HTTP_HEADER_BYTES_LIMIT)
        return result_error<std::vector<std::uint8_t>>("HTTP headers exceed configured size limit");
    std::vector<std::uint8_t> result;
    result.reserve(start.size() + encoded_headers.size() + request.body.size() + 2);
    result.insert(result.end(), start.begin(), start.end());
    result.insert(result.end(), encoded_headers.begin(), encoded_headers.end());
    result.push_back('\r'); result.push_back('\n');
    result.insert(result.end(), request.body.begin(), request.body.end());
    return result_ok<std::vector<std::uint8_t>>(std::move(result));
}
#if defined(__linux__) && defined(TYVERA_NATIVE_TLS)
struct NativeHttpTransport {
    std::shared_ptr<TcpConnection> tcp;
    std::shared_ptr<TlsConnection> tls;
    void close() noexcept {
        if (tls) (void)tls->close();
        else if (tcp && tcp->socket) (void)tcp->socket->close();
    }
};
using NativeHttpReadResult = StringResult<std::optional<std::vector<std::uint8_t>>>;
using NativeHttpWriteResult = StringResult<std::int64_t>;
inline Task<NativeHttpReadResult> http_transport_read(
    const std::shared_ptr<NativeHttpTransport>& transport, const std::string& file, int line, int column) {
    if (transport->tls) return tls_read(transport->tls, 65536, file, line, column);
    return tcp_read(transport->tcp, 65536, file, line, column);
}
inline Task<NativeHttpWriteResult> http_transport_write(
    const std::shared_ptr<NativeHttpTransport>& transport, std::vector<std::uint8_t> bytes,
    const std::string& file, int line, int column) {
    if (transport->tls) return tls_write_all(transport->tls, std::move(bytes), file, line, column);
    return tcp_write_all(transport->tcp, std::move(bytes), file, line, column);
}
inline Task<StringResult<std::shared_ptr<HttpResponse>>> http_send(
    HttpUrl url, std::shared_ptr<HttpRequest> request, Duration timeout,
    const std::string& file, int line, int column) {
    using Output = StringResult<std::shared_ptr<HttpResponse>>;
    if (timeout.milliseconds <= 0)
        return make_io_task<Output>([]() -> Output { return result_error<std::shared_ptr<HttpResponse>>("HTTP timeout must be greater than zero"); },
                                    file, line, column);
    return make_io_task<Output>(
        [url = std::move(url), request = std::move(request), timeout, file, line, column]() mutable -> Output {
            if (!request) return result_error<std::shared_ptr<HttpResponse>>("invalid HTTP request values");
            auto encoded = http_encode_request(*request, url);
            if (std::holds_alternative<std::string>(encoded))
                return result_error<std::shared_ptr<HttpResponse>>("HTTP request encoding failed: " + std::get<1>(std::move(encoded)));
            auto transport = std::make_shared<NativeHttpTransport>();
            if (url.scheme == "https") {
                auto operation = tls_connect(url.host, url.port, file, line, column);
                std::optional<StringResult<std::shared_ptr<TlsConnection>>> opened;
                if (!websocket_await_timed(std::move(operation), timeout.milliseconds, file, line, column, opened))
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP timeout");
                if (!opened || std::holds_alternative<std::string>(*opened))
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP connect failed: "
                        + (opened ? std::get<1>(*opened) : std::string("invalid connect completion")));
                transport->tls = std::get<0>(std::move(*opened));
            } else {
                auto operation = tcp_connect(url.host, url.port, file, line, column);
                std::optional<StringResult<std::shared_ptr<TcpConnection>>> opened;
                if (!websocket_await_timed(std::move(operation), timeout.milliseconds, file, line, column, opened))
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP timeout");
                if (!opened || std::holds_alternative<std::string>(*opened))
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP connect failed: "
                        + (opened ? std::get<1>(*opened) : std::string("invalid connect completion")));
                transport->tcp = std::get<0>(std::move(*opened));
            }
            auto writing = http_transport_write(transport, std::get<0>(std::move(encoded)), file, line, column);
            std::optional<NativeHttpWriteResult> write_result;
            if (!websocket_await_timed(std::move(writing), timeout.milliseconds, file, line, column, write_result)) {
                transport->close();
                return result_error<std::shared_ptr<HttpResponse>>("HTTP timeout");
            }
            if (!write_result || std::holds_alternative<std::string>(*write_result)) {
                const auto detail = write_result ? std::get<1>(*write_result) : std::string("invalid write completion");
                transport->close();
                return result_error<std::shared_ptr<HttpResponse>>("HTTP write failed: " + detail);
            }
            std::vector<std::uint8_t> receive_buffer;
            for (;;) {
                auto reading = http_transport_read(transport, file, line, column);
                std::optional<NativeHttpReadResult> read_result;
                if (!websocket_await_timed(std::move(reading), timeout.milliseconds, file, line, column, read_result)) {
                    transport->close();
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP timeout");
                }
                if (!read_result || std::holds_alternative<std::string>(*read_result)) {
                    const auto detail = read_result ? std::get<1>(*read_result) : std::string("invalid read completion");
                    transport->close();
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP read failed: " + detail);
                }
                auto chunk = std::get<0>(std::move(*read_result));
                const bool eof = !chunk.has_value();
                if (chunk) {
                    if (chunk->size() > HTTP_BUFFER_BYTES_LIMIT
                        || receive_buffer.size() > HTTP_BUFFER_BYTES_LIMIT - chunk->size()) {
                        transport->close();
                        return result_error<std::shared_ptr<HttpResponse>>("HTTP message exceeds configured size limits");
                    }
                    receive_buffer.insert(receive_buffer.end(), chunk->begin(), chunk->end());
                }
                auto decoded = http_decode_response(receive_buffer, eof, request->method.name, file, line, column);
                if (std::holds_alternative<std::string>(decoded)) {
                    transport->close();
                    return result_error<std::shared_ptr<HttpResponse>>("HTTP response parse error: " + std::get<1>(std::move(decoded)));
                }
                auto message = std::get<0>(std::move(decoded));
                if (!message) {
                    if (eof) {
                        transport->close();
                        return result_error<std::shared_ptr<HttpResponse>>("HTTP connection closed before a complete response");
                    }
                    continue;
                }
                auto response = std::move(message->value);
                if (message->consumed > receive_buffer.size()) {
                    transport->close();
                    return result_error<std::shared_ptr<HttpResponse>>("invalid HTTP response completion");
                }
                receive_buffer.erase(receive_buffer.begin(),
                    receive_buffer.begin() + static_cast<std::ptrdiff_t>(message->consumed));
                if (response->status >= 100 && response->status < 200 && response->status != 101) {
                    if (receive_buffer.empty()) {
                        if (eof) {
                            transport->close();
                            return result_error<std::shared_ptr<HttpResponse>>("HTTP connection closed before a complete response");
                        }
                        continue;
                    }
                    auto next = http_decode_response(receive_buffer, eof, request->method.name, file, line, column);
                    if (std::holds_alternative<std::string>(next)) {
                        transport->close();
                        return result_error<std::shared_ptr<HttpResponse>>("HTTP response parse error: " + std::get<1>(std::move(next)));
                    }
                    auto next_message = std::get<0>(std::move(next));
                    if (!next_message) continue;
                    response = std::move(next_message->value);
                    if (response->status >= 100 && response->status < 200 && response->status != 101) {
                        transport->close();
                        return result_error<std::shared_ptr<HttpResponse>>("too many interim HTTP responses");
                    }
                }
                transport->close();
                return result_ok<std::shared_ptr<HttpResponse>>(std::move(response));
            }
        }, file, line, column);
}
#else
inline Task<StringResult<std::shared_ptr<HttpResponse>>> http_send(
    HttpUrl, std::shared_ptr<HttpRequest>, Duration, const std::string& file, int line, int column) {
    return make_io_task<StringResult<std::shared_ptr<HttpResponse>>>([] {
        return result_error<std::shared_ptr<HttpResponse>>("HTTP client is not supported by this native platform");
    }, file, line, column);
}
#endif
inline Task<StringResult<std::shared_ptr<HttpResponse>>> http_get(
    HttpUrl url, Duration timeout, const std::string& file, int line, int column) {
    auto request = std::make_shared<HttpRequest>();
    request->method = HttpMethod{"GET"}; request->target = url.target;
    request->headers = std::make_shared<HttpHeaders>();
    return http_send(std::move(url), std::move(request), timeout, file, line, column);
}
inline Task<StringResult<std::shared_ptr<HttpResponse>>> http_delete(
    HttpUrl url, Duration timeout, const std::string& file, int line, int column) {
    auto request = std::make_shared<HttpRequest>();
    request->method = HttpMethod{"DELETE"}; request->target = url.target;
    request->headers = std::make_shared<HttpHeaders>();
    return http_send(std::move(url), std::move(request), timeout, file, line, column);
}
inline Task<StringResult<std::shared_ptr<HttpResponse>>> http_post(
    HttpUrl url, std::shared_ptr<HttpHeaders> headers, std::vector<std::uint8_t> body,
    Duration timeout, const std::string& file, int line, int column) {
    auto request = std::make_shared<HttpRequest>();
    request->method = HttpMethod{"POST"}; request->target = url.target;
    request->headers = headers ? std::move(headers) : std::make_shared<HttpHeaders>();
    request->body = std::move(body);
    return http_send(std::move(url), std::move(request), timeout, file, line, column);
}
inline Task<StringResult<std::shared_ptr<HttpResponse>>> http_put(
    HttpUrl url, std::shared_ptr<HttpHeaders> headers, std::vector<std::uint8_t> body,
    Duration timeout, const std::string& file, int line, int column) {
    auto request = std::make_shared<HttpRequest>();
    request->method = HttpMethod{"PUT"}; request->target = url.target;
    request->headers = headers ? std::move(headers) : std::make_shared<HttpHeaders>();
    request->body = std::move(body);
    return http_send(std::move(url), std::move(request), timeout, file, line, column);
}

} // namespace tyvera_runtime
'''

