from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TextIO

from .ast import Binary, Call, Expression, Function, LetStatement, Literal, Name, Program, ReturnStatement, Unary
from .source import Diagnostic, Location
from .typesys import INT_MAX, INT_MIN


@dataclass(slots=True)
class _ReturnSignal(Exception):
    value: object


class Interpreter:
    def __init__(self, program: Program, output: TextIO, max_call_depth: int = 256) -> None:
        self.functions = {function.name: function for function in program.functions}
        self.output = output
        self.max_call_depth = max_call_depth

    def run(self) -> None:
        main = self.functions["main"]
        self._call(main, [], main.location, 0)

    def _call(self, function: Function, arguments: list[object], call_location: Location, depth: int) -> object:
        if depth >= self.max_call_depth:
            raise Diagnostic(call_location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
        variables = {parameter.name: value for parameter, value in zip(function.parameters, arguments)}
        try:
            for statement in function.body:
                if isinstance(statement, LetStatement):
                    variables[statement.name] = self._evaluate(statement.value, variables, depth)
                elif isinstance(statement, ReturnStatement):
                    value = None if statement.value is None else self._evaluate(statement.value, variables, depth)
                    raise _ReturnSignal(value)
                else:
                    self._evaluate(statement.expression, variables, depth)
        except _ReturnSignal as signal:
            return signal.value
        return None

    def _evaluate(self, expression: Expression, variables: dict[str, object], depth: int) -> object:
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, Name):
            return variables[expression.identifier]
        if isinstance(expression, Call):
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
            if expression.callee == "println":
                self.output.write(self._display(arguments[0]) + "\n")
                return None
            return self._call(self.functions[expression.callee], arguments, expression.location, depth + 1)
        if isinstance(expression, Unary):
            value = self._evaluate(expression.operand, variables, depth)
            if expression.operator == "!":
                return not value
            result = -value
            self._check_numeric_result(result, expression.location)
            return result
        if isinstance(expression, Binary):
            left = self._evaluate(expression.left, variables, depth)
            if expression.operator == "and" and not left:
                return False
            if expression.operator == "or" and left:
                return True
            right = self._evaluate(expression.right, variables, depth)
            try:
                result = self._apply_binary(expression.operator, left, right)
            except ZeroDivisionError as error:
                raise Diagnostic(expression.location, "runtime", "division by zero") from error
            self._check_numeric_result(result, expression.location)
            return result
        raise AssertionError(f"unhandled expression node: {type(expression).__name__}")

    @staticmethod
    def _check_numeric_result(value: object, location: Location) -> None:
        if type(value) is int and not INT_MIN <= value <= INT_MAX:
            raise Diagnostic(location, "runtime", "Int overflow: result is outside the signed 64-bit range")
        if isinstance(value, float) and not math.isfinite(value):
            raise Diagnostic(location, "runtime", "Float overflow: result is outside the supported range")

    @staticmethod
    def _apply_binary(operator: str, left: object, right: object) -> object:
        if operator == "and":
            return bool(left and right)
        if operator == "or":
            return bool(left or right)
        if operator == "+":
            return left + right  # type: ignore[operator]
        if operator == "-":
            return left - right  # type: ignore[operator]
        if operator == "*":
            return left * right  # type: ignore[operator]
        if operator == "/":
            return left / right  # type: ignore[operator]
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        if operator == "<":
            return left < right  # type: ignore[operator]
        if operator == "<=":
            return left <= right  # type: ignore[operator]
        if operator == ">":
            return left > right  # type: ignore[operator]
        if operator == ">=":
            return left >= right  # type: ignore[operator]
        raise AssertionError(f"unhandled operator: {operator}")

    @staticmethod
    def _display(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
