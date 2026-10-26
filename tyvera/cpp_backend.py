"""C++17 bootstrap code generator and host compiler for the native IR."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from .ir import (
    IRAppliedType,
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
    IRModule,
    IROutput,
    IRReturn,
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

    def build(self, module: IRModule, output: str | Path) -> Path:
        validate_ir(module)
        destination = Path(output).expanduser().resolve()
        if destination.exists() and destination.is_dir():
            raise Diagnostic(module.functions[0].location, "backend", "native output path names a directory")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise Diagnostic(module.functions[0].location, "backend", f"cannot prepare native output directory: {error}") from error
        compiler = _find_compiler(module.functions[0].location)
        source = emit_cpp(module)
        try:
            with tempfile.TemporaryDirectory(prefix=".tyvera-build-", dir=destination.parent) as temporary:
                directory = Path(temporary)
                source_file = directory / "program.cpp"
                candidate = directory / ("program.exe" if os.name == "nt" else "program")
                source_file.write_text(source, encoding="utf-8", newline="\n")
                command = _compiler_command(compiler, source_file, candidate)
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


def _compiler_command(compiler: tuple[str, ...], source: Path, output: Path) -> list[str]:
    command = list(compiler)
    executable = Path(command[0]).name.lower()
    if executable in {"cl", "cl.exe"}:
        return [*command, "/nologo", "/std:c++17", "/EHsc", str(source), f"/Fe:{output}"]
    return [*command, "-std=c++17", "-O2", str(source), "-o", str(output)]


def emit_cpp(module: IRModule) -> str:
    """Return complete C++17 source for a validated Tyvera IR module."""
    validate_ir(module)
    generator = _CppGenerator(module)
    return generator.generate()


class _CppGenerator:
    def __init__(self, module: IRModule) -> None:
        self.module = module
        self.function_symbols = {function.symbol: function for function in module.functions}
        self.lines: list[str] = []
        self.indent = 0
        self.temporary = 0

    def generate(self) -> str:
        self.lines.extend(_RUNTIME.splitlines())
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
        self._line("try {")
        self.indent += 1
        self._line(f"tyvera_generated::{self.module.entry_symbol}();")
        self._line("return 0;")
        self.indent -= 1
        self._line("} catch (const tyvera_runtime::Failure& error) {")
        self.indent += 1
        self._line("""std::cerr << error.file << ':' << error.line << ':' << error.column << ": runtime error: " << error.message << '\\n';""")
        self._line("return 1;")
        self.indent -= 1
        self._line("} catch (const std::exception&) {")
        self.indent += 1
        self._line('std::cerr << "<native>:1:1: runtime error: native runtime failure\\n";')
        self._line("return 1;")
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
            raise ValueError(f"unknown applied Tyvera IR type: {type_name.name}")
        return {
            IRType.INT: "std::int64_t",
            IRType.FLOAT: "double",
            IRType.BOOL: "bool",
            IRType.STRING: "std::string",
            IRType.BYTE: "std::uint8_t",
            IRType.BYTES: "std::vector<std::uint8_t>",
            IRType.VOID: "void",
        }[type_name]

    def _function_signature(self, function: IRFunction) -> str:
        parameters = ", ".join(
            f"{self._cpp_type(parameter.type)} tyv_local_{parameter.identifier}"
            for parameter in function.parameters
        )
        return f"{self._cpp_type(function.return_type)} {function.symbol}({parameters})"

    def _function(self, function: IRFunction) -> None:
        self.temporary = 0
        self._line(self._function_signature(function) + " {")
        self.indent += 1
        for statement in function.body:
            self._statement(statement)
        self.indent -= 1
        self._line("}")

    def _statement(self, statement) -> None:
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
        elif isinstance(statement, IRBlock):
            self._line("{")
            self.indent += 1
            for nested in statement.statements:
                self._statement(nested)
            self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRIf):
            self._line(f"if ({self._expression(statement.condition)}) {{")
            self.indent += 1
            for nested in statement.then_body:
                self._statement(nested)
            self.indent -= 1
            if statement.else_body:
                self._line("} else {")
                self.indent += 1
                for nested in statement.else_body:
                    self._statement(nested)
                self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRWhile):
            self._line(f"while ({self._expression(statement.condition)}) {{")
            self.indent += 1
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
            self._line(f"const auto tyv_local_{statement.variable.identifier} = {snapshot}_item;")
            for nested in statement.body:
                self._statement(nested)
            self.indent -= 1
            self._line("}")
        elif isinstance(statement, IRMatch):
            subject = self._new_temporary()
            self._line(f"const auto {subject} = {self._expression(statement.value)};")
            for index, arm in enumerate(statement.arms):
                prefix = "if" if index == 0 else "else if"
                condition = self._pattern_condition(arm.pattern, subject)
                self._line(f"{prefix} ({condition}) {{")
                self.indent += 1
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
            self._line(f"const auto tyv_local_{pattern.variable.identifier} = {value};")
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
        if isinstance(expression, IRCall):
            arguments = [self._expression(argument) for argument in expression.arguments]
            if not arguments:
                return f"{expression.function}()"
            temporary_names = [self._new_temporary() for _ in arguments]
            result_type = self._cpp_type(expression.type)
            lines = ["([&]() -> " + result_type + " {"]
            lines.extend(f"    const auto {name} = {argument};" for name, argument in zip(temporary_names, arguments))
            call = f"{expression.function}({', '.join(temporary_names)})"
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
            argument = self._expression(expression.arguments[0])
            if expression.builtin == "byteFromInt":
                return f"tyvera_runtime::byte_from_int({argument}, {_location_args(expression.location)})"
            if expression.builtin == "bytesFromString":
                return f"tyvera_runtime::bytes_from_string({argument})"
            raise ValueError(f"unknown native builtin: {expression.builtin}")
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
        lines.append(f"    const auto {receiver} = {self._expression(expression.receiver)};")
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
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "List":
            functions = {
                "append": "list_append", "insert": "list_insert", "get": "list_get",
                "set": "list_set", "removeAt": "list_remove_at", "contains": "list_contains",
                "clear": "list_clear",
            }
            values = [receiver, *arguments]
            if method in {"insert", "set"}:
                values.append(_location_args(expression.location))
            return f"tyvera_runtime::{functions[method]}({', '.join(values)})"
        if isinstance(expression.receiver.type, IRAppliedType) and expression.receiver.type.name == "Map":
            functions = {
                "put": "map_put", "get": "map_get", "remove": "map_remove",
                "containsKey": "map_contains_key", "keys": "map_keys", "values": "map_values",
                "clear": "map_clear",
            }
            return f"tyvera_runtime::{functions[method]}({', '.join([receiver, *arguments])})"
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


_RUNTIME = r'''#include <charconv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <iostream>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <string>
#include <system_error>
#include <utility>
#include <variant>
#include <vector>

namespace tyvera_runtime {
struct Failure final : std::exception {
    std::string file;
    int line;
    int column;
    std::string message;
    Failure(std::string source_file, int source_line, int source_column, std::string text)
        : file(std::move(source_file)), line(source_line), column(source_column), message(std::move(text)) {}
    const char* what() const noexcept override { return message.c_str(); }
};

[[noreturn]] inline void fail(const std::string& file, int line, int column, const char* message) {
    throw Failure(file, line, column, message);
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
