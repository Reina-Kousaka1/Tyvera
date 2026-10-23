from __future__ import annotations

import math
from collections import ChainMap
from dataclasses import dataclass

from .ast import (
    Assignment,
    Binary,
    Block,
    Call,
    Expression,
    ExpressionStatement,
    Function,
    IfStatement,
    LetStatement,
    Literal,
    LoopControl,
    Name,
    Program,
    ReturnStatement,
    Statement,
    Unary,
    WhileStatement,
)
from .source import Diagnostic, Location


_BUILTIN_TYPES = {"Int", "Float", "Bool", "String", "Void", "Unit"}
_DISPLAYABLE_TYPES = {"Int", "Float", "Bool", "String"}
_NUMERIC_TYPES = {"Int", "Float"}
INT_MIN = -(1 << 63)
INT_MAX = (1 << 63) - 1


def canonical_type(name: str) -> str:
    return "Void" if name == "Unit" else name


@dataclass(frozen=True, slots=True)
class Binding:
    type_name: str
    mutable: bool = False


@dataclass(frozen=True, slots=True)
class Signature:
    parameter_types: tuple[str, ...]
    return_type: str
    location: Location


class TypeChecker:
    def __init__(self, program: Program, filename: str = "<memory>") -> None:
        self.program = program
        self.filename = filename
        self.signatures: dict[str, Signature] = {}

    def check(self) -> dict[str, Signature]:
        self._collect_signatures()
        entry = self.signatures.get("main")
        if entry is None:
            location = self.program.functions[0].location if self.program.functions else Location(self.filename, 1, 1)
            raise Diagnostic(location, "type", "program must define 'fn main()'")
        if entry.parameter_types:
            raise Diagnostic(entry.location, "type", "entry point 'main' must not have parameters")
        if entry.return_type != "Void":
            raise Diagnostic(entry.location, "type", "entry point 'main' must return Void")
        for function in self.program.functions:
            self._check_function(function)
        return self.signatures

    def _collect_signatures(self) -> None:
        for function in self.program.functions:
            if function.name == "println":
                raise Diagnostic(function.location, "type", "'println' is a built-in function name")
            if function.name in self.signatures:
                raise Diagnostic(function.location, "type", f"function '{function.name}' is already declared")
            parameter_names: set[str] = set()
            parameter_types: list[str] = []
            for parameter in function.parameters:
                if parameter.name in parameter_names:
                    raise Diagnostic(parameter.location, "type", f"parameter '{parameter.name}' is declared more than once")
                parameter_names.add(parameter.name)
                self._require_known_type(parameter.type_name, parameter.location)
                if canonical_type(parameter.type_name) == "Void":
                    raise Diagnostic(parameter.location, "type", "parameters cannot have type Void")
                parameter_types.append(canonical_type(parameter.type_name))
            self._require_known_type(function.return_type, function.location)
            self.signatures[function.name] = Signature(
                tuple(parameter_types), canonical_type(function.return_type), function.location
            )

    def _require_known_type(self, type_name: str, location: Location) -> None:
        if type_name not in _BUILTIN_TYPES:
            raise Diagnostic(location, "type", f"unknown type '{type_name}'")

    def _check_function(self, function: Function) -> None:
        signature = self.signatures[function.name]
        variables = ChainMap({p.name: Binding(canonical_type(p.type_name)) for p in function.parameters})
        for parameter in function.parameters:
            if parameter.name == "println":
                raise Diagnostic(parameter.location, "type", "'println' is reserved for the built-in function")
        outcomes = self._check_block(function.body, variables, signature.return_type, 0)
        if signature.return_type != "Void" and "next" in outcomes:
            raise Diagnostic(function.location, "type",
                             f"function '{function.name}' must return {signature.return_type} on all reachable paths")

    def _check_block(
        self, body: list[Statement], variables: ChainMap[str, Binding],
        return_type: str, loop_depth: int,
    ) -> set[str]:
        # Outcomes model control flow independently of the interpreter. Even
        # unreachable statements are checked, but cannot add reachable exits.
        outcomes = {"next"}
        for statement in body:
            exits = {"next"}
            if isinstance(statement, LetStatement):
                if statement.name == "println":
                    raise Diagnostic(statement.location, "type", "'println' is reserved for the built-in function")
                if statement.name in variables.maps[0]:
                    raise Diagnostic(statement.location, "type", f"variable '{statement.name}' is already declared")
                value_type = self._expression_type(statement.value, variables)
                if statement.annotation is not None:
                    self._require_known_type(statement.annotation, statement.location)
                    if canonical_type(statement.annotation) == "Void":
                        raise Diagnostic(statement.location, "type", "variables cannot have type Void")
                    self._require_same_type(canonical_type(statement.annotation), value_type, statement.location)
                    variable_type = canonical_type(statement.annotation)
                else:
                    if value_type == "Void":
                        raise Diagnostic(statement.location, "type", "cannot infer a variable type from a Void expression")
                    variable_type = value_type
                variables[statement.name] = Binding(variable_type, statement.mutable)
            elif isinstance(statement, Assignment):
                binding = variables.get(statement.name)
                if binding is None:
                    raise Diagnostic(statement.location, "type", f"unknown variable '{statement.name}'")
                if not binding.mutable:
                    raise Diagnostic(statement.location, "type", f"cannot assign to immutable binding '{statement.name}'")
                self._require_same_type(binding.type_name, self._expression_type(statement.value, variables), statement.location)
            elif isinstance(statement, ReturnStatement):
                value_type = "Void" if statement.value is None else self._expression_type(statement.value, variables)
                self._require_same_type(return_type, value_type, statement.location)
                exits = {"return"}
            elif isinstance(statement, Block):
                exits = self._check_block(statement.body, variables.new_child(), return_type, loop_depth)
            elif isinstance(statement, IfStatement):
                self._require_same_type("Bool", self._expression_type(statement.condition, variables), statement.condition.location)
                then_exits = self._check_block(statement.then_body, variables.new_child(), return_type, loop_depth)
                else_exits = self._check_block(statement.else_body, variables.new_child(), return_type, loop_depth)
                if isinstance(statement.condition, Literal):
                    exits = then_exits if statement.condition.value else else_exits
                else:
                    exits = then_exits | else_exits
            elif isinstance(statement, WhileStatement):
                self._require_same_type("Bool", self._expression_type(statement.condition, variables), statement.condition.location)
                body_exits = self._check_block(statement.body, variables.new_child(), return_type, loop_depth + 1)
                literal = isinstance(statement.condition, Literal)
                exits = set()
                if not literal or statement.condition.value:
                    exits |= body_exits & {"return"}
                if not literal or not statement.condition.value or "break" in body_exits:
                    exits.add("next")
            elif isinstance(statement, LoopControl):
                if not loop_depth:
                    raise Diagnostic(statement.location, "type", f"'{statement.kind}' is only valid inside a loop")
                exits = {statement.kind}
            elif isinstance(statement, ExpressionStatement):
                self._expression_type(statement.expression, variables)
            else:
                raise AssertionError(f"unhandled statement node: {type(statement).__name__}")
            if "next" in outcomes:
                outcomes = (outcomes - {"next"}) | exits
        return outcomes

    def _expression_type(self, expression: Expression, variables: ChainMap[str, Binding]) -> str:
        if isinstance(expression, Literal):
            if expression.type_name == "Int" and expression.value > INT_MAX:
                raise Diagnostic(expression.location, "type", "Int literal is outside the signed 64-bit range")
            if expression.type_name == "Float" and not math.isfinite(expression.value):
                raise Diagnostic(expression.location, "type", "Float literal is outside the supported range")
            return expression.type_name
        if isinstance(expression, Name):
            if expression.identifier not in variables:
                raise Diagnostic(expression.location, "type", f"unknown variable '{expression.identifier}'")
            return variables[expression.identifier].type_name
        if isinstance(expression, Call):
            argument_types = [self._expression_type(argument, variables) for argument in expression.arguments]
            if expression.callee == "println":
                if len(argument_types) != 1:
                    raise Diagnostic(expression.location, "type", "'println' expects exactly one argument")
                if argument_types[0] not in _DISPLAYABLE_TYPES:
                    raise Diagnostic(expression.arguments[0].location, "type", "'println' cannot display a Void value")
                return "Void"
            signature = self.signatures.get(expression.callee)
            if signature is None:
                raise Diagnostic(expression.location, "type", f"unknown function '{expression.callee}'")
            if len(argument_types) != len(signature.parameter_types):
                raise Diagnostic(
                    expression.location,
                    "type",
                    f"function '{expression.callee}' expects {len(signature.parameter_types)} argument(s), got {len(argument_types)}",
                )
            for index, (actual, expected) in enumerate(zip(argument_types, signature.parameter_types), start=1):
                if actual != expected:
                    raise Diagnostic(
                        expression.arguments[index - 1].location,
                        "type",
                        f"argument {index} of '{expression.callee}' expects {expected}, found {actual}",
                    )
            return signature.return_type
        if isinstance(expression, Unary):
            if (
                expression.operator == "-"
                and isinstance(expression.operand, Literal)
                and expression.operand.type_name == "Int"
                and expression.operand.value == INT_MAX + 1
            ):
                return "Int"
            operand_type = self._expression_type(expression.operand, variables)
            if expression.operator == "!":
                if operand_type != "Bool":
                    raise Diagnostic(expression.location, "type", f"operator '!' expects Bool, found {operand_type}")
                return "Bool"
            if operand_type not in _NUMERIC_TYPES:
                raise Diagnostic(expression.location, "type", f"unary '-' expects a number, found {operand_type}")
            return operand_type
        if isinstance(expression, Binary):
            left_type = self._expression_type(expression.left, variables)
            right_type = self._expression_type(expression.right, variables)
            return self._binary_type(expression, left_type, right_type)
        raise AssertionError(f"unhandled expression node: {type(expression).__name__}")

    def _binary_type(self, expression: Binary, left: str, right: str) -> str:
        operator = expression.operator
        if operator in {"and", "or"}:
            if left != "Bool" or right != "Bool":
                raise Diagnostic(expression.location, "type", f"operator '{operator}' expects Bool operands, found {left} and {right}")
            return "Bool"
        if operator == "+" and left == right == "String":
            return "String"
        if operator in {"+", "-", "*", "/"}:
            if left not in _NUMERIC_TYPES or right not in _NUMERIC_TYPES:
                raise Diagnostic(expression.location, "type", f"operator '{operator}' expects numeric operands, found {left} and {right}")
            if operator == "/":
                return "Float"
            return "Float" if "Float" in {left, right} else "Int"
        if operator in {"==", "!="}:
            if left == "Void" or right == "Void":
                raise Diagnostic(expression.location, "type", "Void values cannot be compared")
            if left != right and not ({left, right} <= _NUMERIC_TYPES):
                raise Diagnostic(expression.location, "type", f"cannot compare {left} with {right}")
            return "Bool"
        if operator in {"<", "<=", ">", ">="}:
            if left not in _NUMERIC_TYPES or right not in _NUMERIC_TYPES:
                raise Diagnostic(expression.location, "type", f"operator '{operator}' expects numeric operands, found {left} and {right}")
            return "Bool"
        raise AssertionError(f"unhandled operator: {operator}")

    @staticmethod
    def _require_same_type(expected: str, actual: str, location: Location) -> None:
        if expected != actual:
            raise Diagnostic(location, "type", f"expected {expected}, found {actual}")
