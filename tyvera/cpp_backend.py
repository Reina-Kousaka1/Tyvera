"""C++17 bootstrap code generator and host compiler for the native IR."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from .ir import (
    IRBinary,
    IRBlock,
    IRCall,
    IRExpression,
    IRExpressionStatement,
    IRFunction,
    IRIf,
    IRLet,
    IRLiteral,
    IRLocalRef,
    IRLoopControl,
    IRModule,
    IROutput,
    IRReturn,
    IRType,
    IRUnary,
    IRAssign,
    IRVariable,
    IRWhile,
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
    def _cpp_type(type_name: IRType) -> str:
        return {
            IRType.INT: "std::int64_t",
            IRType.FLOAT: "double",
            IRType.BOOL: "bool",
            IRType.STRING: "std::string",
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
        elif isinstance(statement, IRLoopControl):
            self._line(f"{statement.kind};")
        else:
            raise ValueError(f"unhandled IR statement: {type(statement).__name__}")

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
#include <cstdint>
#include <exception>
#include <iostream>
#include <limits>
#include <string>
#include <system_error>
#include <utility>

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
