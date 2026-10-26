"""Typed, backend-neutral IR for Tyvera's native compiler slice.

The IR deliberately describes only expressions and control flow that the
current native backend can lower. It carries Tyvera types and source locations
so later backends do not need to infer language semantics from generated C++.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from .source import Diagnostic, Location
from .typesys import INT_MAX, INT_MIN


class IRType(str, Enum):
    INT = "Int"
    FLOAT = "Float"
    BOOL = "Bool"
    STRING = "String"
    VOID = "Void"


NUMERIC_TYPES = frozenset({IRType.INT, IRType.FLOAT})


@dataclass(frozen=True, slots=True)
class IRVariable:
    identifier: int
    name: str
    type: IRType
    mutable: bool
    location: Location


class IRExpression:
    type: IRType
    location: Location


@dataclass(frozen=True, slots=True)
class IRLiteral(IRExpression):
    value: int | float | bool | str
    type: IRType
    location: Location


@dataclass(frozen=True, slots=True)
class IRLocalRef(IRExpression):
    variable_id: int
    type: IRType
    location: Location


@dataclass(frozen=True, slots=True)
class IRCall(IRExpression):
    function: str
    arguments: tuple[IRExpression, ...]
    type: IRType
    location: Location


@dataclass(frozen=True, slots=True)
class IRUnary(IRExpression):
    operator: str
    operand: IRExpression
    type: IRType
    location: Location


@dataclass(frozen=True, slots=True)
class IRBinary(IRExpression):
    left: IRExpression
    operator: str
    right: IRExpression
    type: IRType
    location: Location


class IRStatement:
    location: Location


@dataclass(frozen=True, slots=True)
class IRLet(IRStatement):
    variable: IRVariable
    value: IRExpression
    location: Location


@dataclass(frozen=True, slots=True)
class IRAssign(IRStatement):
    variable_id: int
    value: IRExpression
    location: Location


@dataclass(frozen=True, slots=True)
class IRExpressionStatement(IRStatement):
    expression: IRExpression
    location: Location


@dataclass(frozen=True, slots=True)
class IROutput(IRStatement):
    channel: str
    value: IRExpression
    location: Location


@dataclass(frozen=True, slots=True)
class IRReturn(IRStatement):
    value: IRExpression | None
    location: Location


@dataclass(frozen=True, slots=True)
class IRBlock(IRStatement):
    statements: tuple[IRStatement, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRIf(IRStatement):
    condition: IRExpression
    then_body: tuple[IRStatement, ...]
    else_body: tuple[IRStatement, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRWhile(IRStatement):
    condition: IRExpression
    body: tuple[IRStatement, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRLoopControl(IRStatement):
    kind: str
    location: Location


IRStatementLike: TypeAlias = (
    IRLet | IRAssign | IRExpressionStatement | IROutput | IRReturn | IRBlock
    | IRIf | IRWhile | IRLoopControl
)
IRExpressionLike: TypeAlias = IRLiteral | IRLocalRef | IRCall | IRUnary | IRBinary


@dataclass(frozen=True, slots=True)
class IRFunction:
    name: str
    symbol: str
    parameters: tuple[IRVariable, ...]
    return_type: IRType
    body: tuple[IRStatement, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRModule:
    functions: tuple[IRFunction, ...]
    entry_symbol: str


def _invalid(location: Location, message: str) -> Diagnostic:
    return Diagnostic(location, "ir", message)


def validate_ir(module: IRModule) -> None:
    """Check types, local binding topology, calls, and control-flow placement."""
    if not module.functions:
        raise _invalid(Location("<ir>", 1, 1), "IR module has no functions")
    symbols: dict[str, IRFunction] = {}
    entries: list[IRFunction] = []
    for function in module.functions:
        if function.symbol in symbols:
            raise _invalid(function.location, f"duplicate IR function symbol '{function.symbol}'")
        symbols[function.symbol] = function
        if function.name == "main":
            entries.append(function)
        if any(parameter.type == IRType.VOID for parameter in function.parameters):
            raise _invalid(function.location, "IR function parameters cannot have type Void")
        parameter_ids = [parameter.identifier for parameter in function.parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise _invalid(function.location, "IR function has duplicate parameter identifiers")
    if len(entries) != 1 or entries[0].symbol != module.entry_symbol:
        raise _invalid(module.functions[0].location, "IR entry point must be the unique main function")
    entry = entries[0]
    if entry.parameters or entry.return_type != IRType.VOID:
        raise _invalid(entry.location, "IR main must have no parameters and return Void")
    if module.entry_symbol not in symbols:
        raise _invalid(module.functions[0].location, "IR entry symbol does not exist")

    for function in module.functions:
        scopes: list[dict[int, IRVariable]] = [{
            parameter.identifier: parameter for parameter in function.parameters
        }]
        seen_ids = set(scopes[0])
        _validate_statements(function.body, function.return_type, symbols, scopes, seen_ids, 0)
        if function.return_type != IRType.VOID and "next" in _flow_outcomes(function.body):
            raise _invalid(function.location, f"IR function '{function.name}' can fall through without returning {function.return_type.value}")


def _flow_outcomes(statements: tuple[IRStatement, ...]) -> set[str]:
    outcomes = {"next"}
    for statement in statements:
        if "next" not in outcomes:
            continue
        outcomes.remove("next")
        outcomes.update(_statement_outcomes(statement))
    return outcomes


def _statement_outcomes(statement: IRStatement) -> set[str]:
    if isinstance(statement, IRReturn):
        return {"return"}
    if isinstance(statement, IRLoopControl):
        return {statement.kind}
    if isinstance(statement, IRBlock):
        return _flow_outcomes(statement.statements)
    if isinstance(statement, IRIf):
        then_outcomes = _flow_outcomes(statement.then_body)
        else_outcomes = _flow_outcomes(statement.else_body) if statement.else_body else {"next"}
        return then_outcomes | else_outcomes
    if isinstance(statement, IRWhile):
        body_outcomes = _flow_outcomes(statement.body)
        if isinstance(statement.condition, IRLiteral) and statement.condition.type == IRType.BOOL and statement.condition.value:
            result = body_outcomes & {"return"}
            if "break" in body_outcomes:
                result.add("next")
            return result
        return {"next"} | (body_outcomes & {"return"})
    return {"next"}


def _find_variable(scopes: list[dict[int, IRVariable]], identifier: int) -> IRVariable | None:
    for scope in reversed(scopes):
        if identifier in scope:
            return scope[identifier]
    return None


def _validate_expression(
    expression: IRExpression,
    functions: dict[str, IRFunction],
    scopes: list[dict[int, IRVariable]],
) -> IRType:
    if isinstance(expression, IRLiteral):
        valid = (
            expression.type == IRType.INT and type(expression.value) is int
            and INT_MIN <= expression.value <= INT_MAX
            or expression.type == IRType.FLOAT and type(expression.value) is float
            and math.isfinite(expression.value)
            or expression.type == IRType.BOOL and type(expression.value) is bool
            or expression.type == IRType.STRING and isinstance(expression.value, str)
        )
        if not valid:
            raise _invalid(expression.location, f"invalid {expression.type.value} literal in IR")
        return expression.type
    if isinstance(expression, IRLocalRef):
        variable = _find_variable(scopes, expression.variable_id)
        if variable is None:
            raise _invalid(expression.location, "IR references a local outside its scope")
        if variable.type != expression.type:
            raise _invalid(expression.location, "IR local reference type does not match its binding")
        return expression.type
    if isinstance(expression, IRCall):
        function = functions.get(expression.function)
        if function is None:
            raise _invalid(expression.location, f"IR calls unknown function '{expression.function}'")
        if len(expression.arguments) != len(function.parameters):
            raise _invalid(expression.location, f"IR call to '{function.name}' has the wrong number of arguments")
        for argument, parameter in zip(expression.arguments, function.parameters):
            if _validate_expression(argument, functions, scopes) != parameter.type:
                raise _invalid(argument.location, f"IR argument to '{function.name}' has the wrong type")
        if expression.type != function.return_type:
            raise _invalid(expression.location, f"IR call result for '{function.name}' has the wrong type")
        return expression.type
    if isinstance(expression, IRUnary):
        operand_type = _validate_expression(expression.operand, functions, scopes)
        if expression.operator == "!" and operand_type == expression.type == IRType.BOOL:
            return expression.type
        if expression.operator == "-" and operand_type in NUMERIC_TYPES and operand_type == expression.type:
            return expression.type
        raise _invalid(expression.location, f"invalid typed IR unary operation '{expression.operator}'")
    if isinstance(expression, IRBinary):
        left_type = _validate_expression(expression.left, functions, scopes)
        right_type = _validate_expression(expression.right, functions, scopes)
        operator = expression.operator
        if operator in {"and", "or"}:
            valid = left_type == right_type == expression.type == IRType.BOOL
        elif operator == "+" and left_type == right_type == expression.type == IRType.STRING:
            valid = True
        elif operator in {"+", "-", "*", "/", "%"} and left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES:
            expected = IRType.FLOAT if operator == "/" or IRType.FLOAT in {left_type, right_type} else IRType.INT
            valid = expression.type == expected
        elif operator in {"==", "!="}:
            valid = expression.type == IRType.BOOL and (
                left_type == right_type or left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
            )
        elif operator in {"<", "<=", ">", ">="}:
            valid = expression.type == IRType.BOOL and left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
        else:
            valid = False
        if not valid:
            raise _invalid(expression.location, f"invalid typed IR binary operation '{operator}'")
        return expression.type
    raise _invalid(expression.location, f"unknown IR expression {type(expression).__name__}")


def _validate_statements(
    statements: tuple[IRStatement, ...],
    return_type: IRType,
    functions: dict[str, IRFunction],
    scopes: list[dict[int, IRVariable]],
    seen_ids: set[int],
    loop_depth: int,
) -> None:
    for statement in statements:
        if isinstance(statement, IRLet):
            if statement.variable.identifier in seen_ids:
                raise _invalid(statement.location, "IR reuses a local identifier")
            value_type = _validate_expression(statement.value, functions, scopes)
            if statement.variable.type != value_type or statement.variable.type == IRType.VOID:
                raise _invalid(statement.location, "IR local initializer type does not match its binding")
            if statement.variable.identifier in scopes[-1]:
                raise _invalid(statement.location, "IR local is declared twice in one scope")
            scopes[-1][statement.variable.identifier] = statement.variable
            seen_ids.add(statement.variable.identifier)
        elif isinstance(statement, IRAssign):
            variable = _find_variable(scopes, statement.variable_id)
            if variable is None:
                raise _invalid(statement.location, "IR assignment targets a local outside its scope")
            if not variable.mutable:
                raise _invalid(statement.location, f"IR assigns to immutable local '{variable.name}'")
            if _validate_expression(statement.value, functions, scopes) != variable.type:
                raise _invalid(statement.location, "IR assignment type does not match its local")
        elif isinstance(statement, IRExpressionStatement):
            _validate_expression(statement.expression, functions, scopes)
        elif isinstance(statement, IROutput):
            if statement.channel not in {"stdout", "stderr"}:
                raise _invalid(statement.location, f"unknown IR output channel '{statement.channel}'")
            if _validate_expression(statement.value, functions, scopes) not in {
                IRType.INT, IRType.FLOAT, IRType.BOOL, IRType.STRING,
            }:
                raise _invalid(statement.location, "IR output value is not displayable")
        elif isinstance(statement, IRReturn):
            actual = IRType.VOID if statement.value is None else _validate_expression(statement.value, functions, scopes)
            if actual != return_type:
                raise _invalid(statement.location, "IR return type does not match its function")
        elif isinstance(statement, IRBlock):
            scopes.append({})
            _validate_statements(statement.statements, return_type, functions, scopes, seen_ids, loop_depth)
            scopes.pop()
        elif isinstance(statement, IRIf):
            if _validate_expression(statement.condition, functions, scopes) != IRType.BOOL:
                raise _invalid(statement.location, "IR if condition must be Bool")
            scopes.append({})
            _validate_statements(statement.then_body, return_type, functions, scopes, seen_ids, loop_depth)
            scopes.pop()
            scopes.append({})
            _validate_statements(statement.else_body, return_type, functions, scopes, seen_ids, loop_depth)
            scopes.pop()
        elif isinstance(statement, IRWhile):
            if _validate_expression(statement.condition, functions, scopes) != IRType.BOOL:
                raise _invalid(statement.location, "IR while condition must be Bool")
            scopes.append({})
            _validate_statements(statement.body, return_type, functions, scopes, seen_ids, loop_depth + 1)
            scopes.pop()
        elif isinstance(statement, IRLoopControl):
            if statement.kind not in {"break", "continue"} or loop_depth == 0:
                raise _invalid(statement.location, "IR loop control is outside a loop")
        else:
            raise _invalid(statement.location, f"unknown IR statement {type(statement).__name__}")
