from __future__ import annotations

import math
from collections import ChainMap
from dataclasses import dataclass
from typing import TextIO

from .ast import (
    Assignment,
    Binary,
    Block,
    Call,
    ClassDeclaration,
    Expression,
    ExpressionStatement,
    Function,
    ForStatement,
    IfStatement,
    LetStatement,
    Literal,
    LoopControl,
    MemberAccess,
    MemberCall,
    Method,
    Name,
    Program,
    ReturnStatement,
    Statement,
    Unary,
    WhileStatement,
)
from .source import Diagnostic, Location
from .typesys import INT_MAX, INT_MIN


_MISSING = object()


@dataclass(slots=True)
class _ReturnSignal(Exception):
    value: object


@dataclass(slots=True)
class _LoopSignal(Exception):
    kind: str


@dataclass(slots=True, eq=False)
class _TyveraInstance:
    class_name: str
    fields: dict[str, object]


@dataclass(slots=True, eq=False)
class _TyveraList:
    values: list[object]


@dataclass(slots=True, eq=False)
class _TyveraMap:
    values: dict[object, object]


class Interpreter:
    def __init__(self, program: Program, output: TextIO, max_call_depth: int = 256) -> None:
        self.functions = {function.name: function for function in program.functions}
        self.classes: dict[str, ClassDeclaration] = {declaration.name: declaration for declaration in program.classes}
        self.output = output
        self.max_call_depth = max_call_depth

    def run(self) -> None:
        main = self.functions["main"]
        self._call(main, [], main.location, 0)

    def _call(self, function: Function, arguments: list[object], call_location: Location, depth: int) -> object:
        if depth >= self.max_call_depth:
            raise Diagnostic(call_location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
        variables = ChainMap({parameter.name: value for parameter, value in zip(function.parameters, arguments)})
        try:
            self._execute_block(function.body, variables, depth)
        except _ReturnSignal as signal:
            return signal.value
        except RecursionError as error:
            raise Diagnostic(call_location, "runtime", "bootstrap runtime stack limit exceeded") from error
        return None

    def _execute_block(self, body: list[Statement], variables: ChainMap[str, object], depth: int) -> None:
        for statement in body:
            if isinstance(statement, LetStatement):
                variables[statement.name] = self._evaluate(statement.value, variables, depth)
            elif isinstance(statement, Assignment):
                if isinstance(statement.target, MemberAccess):
                    receiver = self._evaluate(statement.target.receiver, variables, depth)
                    value = self._evaluate(statement.value, variables, depth)
                    if not isinstance(receiver, _TyveraInstance):
                        raise AssertionError("checked field assignment receiver is not an instance")
                    receiver.fields[statement.target.member] = value
                    continue
                value = self._evaluate(statement.value, variables, depth)
                for scope in variables.maps:
                    if statement.target in scope:
                        scope[statement.target] = value
                        break
                else:
                    raise AssertionError("checked assignment has no binding")
            elif isinstance(statement, ReturnStatement):
                value = None if statement.value is None else self._evaluate(statement.value, variables, depth)
                raise _ReturnSignal(value)
            elif isinstance(statement, Block):
                self._execute_block(statement.body, variables.new_child(), depth)
            elif isinstance(statement, IfStatement):
                body = statement.then_body if self._evaluate(statement.condition, variables, depth) else statement.else_body
                self._execute_block(body, variables.new_child(), depth)
            elif isinstance(statement, WhileStatement):
                while self._evaluate(statement.condition, variables, depth):
                    try:
                        self._execute_block(statement.body, variables.new_child(), depth)
                    except _LoopSignal as signal:
                        if signal.kind == "break":
                            break
            elif isinstance(statement, ForStatement):
                iterable = self._evaluate(statement.iterable, variables, depth)
                if isinstance(iterable, _TyveraList):
                    items = tuple(iterable.values)
                elif isinstance(iterable, _TyveraMap):
                    items = tuple(iterable.values.keys())
                else:
                    raise AssertionError("checked for-loop value is not iterable")
                for item in items:
                    iteration_scope = variables.new_child()
                    iteration_scope[statement.name] = item
                    try:
                        self._execute_block(statement.body, iteration_scope, depth)
                    except _LoopSignal as signal:
                        if signal.kind == "break":
                            break
            elif isinstance(statement, LoopControl):
                raise _LoopSignal(statement.kind)
            elif isinstance(statement, ExpressionStatement):
                self._evaluate(statement.expression, variables, depth)
            else:
                raise AssertionError(f"unhandled statement node: {type(statement).__name__}")

    def _evaluate(self, expression: Expression, variables: ChainMap[str, object], depth: int) -> object:
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, Name):
            return variables[expression.identifier]
        if isinstance(expression, Call):
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
            if expression.callee == "println":
                self.output.write(self._display(arguments[0]) + "\n")
                return None
            if expression.callee == "List":
                return _TyveraList([])
            if expression.callee == "Map":
                return _TyveraMap({})
            if expression.callee in self.classes:
                return self._construct(self.classes[expression.callee], arguments, expression.location, depth + 1)
            return self._call(self.functions[expression.callee], arguments, expression.location, depth + 1)
        if isinstance(expression, MemberAccess):
            receiver = self._evaluate(expression.receiver, variables, depth)
            if expression.member == "length" and isinstance(receiver, (_TyveraList, _TyveraMap)):
                return len(receiver.values)
            if not isinstance(receiver, _TyveraInstance):
                raise AssertionError("checked field access receiver is not an instance")
            return receiver.fields[expression.member]
        if isinstance(expression, MemberCall):
            receiver = self._evaluate(expression.receiver, variables, depth)
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
            if isinstance(receiver, _TyveraList):
                return self._call_list_method(receiver, expression.method, arguments, expression.location)
            if isinstance(receiver, _TyveraMap):
                return self._call_map_method(receiver, expression.method, arguments, expression.location)
            if not isinstance(receiver, _TyveraInstance):
                raise AssertionError("checked method receiver is not an instance")
            declaration = self.classes[receiver.class_name]
            method = next(method for method in declaration.methods if method.name == expression.method)
            return self._call_method(method, receiver, arguments, expression.location, depth + 1)
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
    def _list_index(values: list[object], index: object, location: Location, insertion: bool = False) -> int:
        if type(index) is not int:
            raise AssertionError("checked collection index is not Int")
        upper = len(values) if insertion else len(values) - 1
        if index < 0 or index > upper:
            raise Diagnostic(location, "runtime", f"List index {index} is out of range")
        return index

    def _call_list_method(
        self, receiver: _TyveraList, method: str, arguments: list[object], location: Location,
    ) -> object:
        values = receiver.values
        if method == "append":
            values.append(arguments[0])
            return None
        if method == "insert":
            index = self._list_index(values, arguments[0], location, insertion=True)
            values.insert(index, arguments[1])
            return None
        if method == "get":
            return values[self._list_index(values, arguments[0], location)]
        if method == "set":
            values[self._list_index(values, arguments[0], location)] = arguments[1]
            return None
        if method == "removeAt":
            return values.pop(self._list_index(values, arguments[0], location))
        if method == "contains":
            return arguments[0] in values
        if method == "clear":
            values.clear()
            return None
        raise AssertionError(f"checked collection method not implemented: {method}")

    @staticmethod
    def _call_map_method(
        receiver: _TyveraMap, method: str, arguments: list[object], location: Location,
    ) -> object:
        values = receiver.values
        try:
            if method == "put":
                values[arguments[0]] = arguments[1]
                return None
            if method == "get":
                try:
                    return values[arguments[0]]
                except KeyError as error:
                    raise Diagnostic(location, "runtime", "Map.get could not find the requested key") from error
            if method == "remove":
                return values.pop(arguments[0], _MISSING) is not _MISSING
            if method == "containsKey":
                return arguments[0] in values
            if method == "keys":
                return _TyveraList(list(values.keys()))
            if method == "values":
                return _TyveraList(list(values.values()))
            if method == "clear":
                values.clear()
                return None
        except TypeError as error:
            raise Diagnostic(location, "runtime", "Map key is not hashable") from error
        raise AssertionError(f"checked collection method not implemented: {method}")

    def _construct(
        self, declaration: ClassDeclaration, arguments: list[object], call_location: Location, depth: int,
    ) -> _TyveraInstance:
        instance = _TyveraInstance(declaration.name, {})
        constructor = next((method for method in declaration.methods if method.name == "init"), None)
        if constructor is not None:
            self._call_method(constructor, instance, arguments, call_location, depth)
        return instance

    def _call_method(
        self, method: Method, receiver: _TyveraInstance, arguments: list[object],
        call_location: Location, depth: int,
    ) -> object:
        if depth >= self.max_call_depth:
            raise Diagnostic(call_location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
        initial = {parameter.name: value for parameter, value in zip(method.parameters, arguments)}
        initial["self"] = receiver
        variables = ChainMap(initial)
        try:
            self._execute_block(method.body, variables, depth)
        except _ReturnSignal as signal:
            return signal.value
        except RecursionError as error:
            raise Diagnostic(call_location, "runtime", "bootstrap runtime stack limit exceeded") from error
        return None

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
