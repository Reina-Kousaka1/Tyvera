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
    BYTE = "Byte"
    BYTES = "Bytes"
    BYTE_BUFFER = "ByteBuffer"
    TEXT_FILE = "TextFile"
    TEXT_READER = "TextReader"
    TEXT_WRITER = "TextWriter"
    VOID = "Void"


@dataclass(frozen=True, slots=True)
class IRAppliedType:
    """A supported Tyvera generic type applied to already lowered arguments."""

    name: str
    arguments: tuple[IRTypeRef, ...]


IRTypeRef: TypeAlias = IRType | IRAppliedType


def ir_type_name(type_ref: IRTypeRef) -> str:
    if isinstance(type_ref, IRType):
        return type_ref.value
    return f"{type_ref.name}<{','.join(ir_type_name(item) for item in type_ref.arguments)}>"


def _applied(name: str, *arguments: IRTypeRef) -> IRAppliedType:
    return IRAppliedType(name, tuple(arguments))


NUMERIC_TYPES = frozenset({IRType.INT, IRType.FLOAT})
_APPLIED_ARITIES = {"Option": 1, "Result": 2, "List": 1, "Map": 2}


@dataclass(frozen=True, slots=True)
class IRVariable:
    identifier: int
    name: str
    type: IRTypeRef
    mutable: bool
    location: Location


class IRExpression:
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRLiteral(IRExpression):
    value: int | float | bool | str
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRLocalRef(IRExpression):
    variable_id: int
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRCall(IRExpression):
    function: str
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRUnary(IRExpression):
    operator: str
    operand: IRExpression
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRMove(IRExpression):
    """An ownership transfer already approved by Tyvera's checked program."""

    operand: IRLocalRef
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRBinary(IRExpression):
    left: IRExpression
    operator: str
    right: IRExpression
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRVariant(IRExpression):
    variant: str
    payload: IRExpression | None
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRBuiltinCall(IRExpression):
    builtin: str
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRCollectionCreate(IRExpression):
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRCollectionLength(IRExpression):
    receiver: IRExpression
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRMethodCall(IRExpression):
    receiver: IRExpression
    method: str
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
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
class IRUsing(IRStatement):
    """Acquire a Result<Resource,String>, bind it for a lexical scope, then close it."""

    resource: IRVariable
    acquisition: IRExpression
    body: tuple[IRStatement, ...]
    error_variable: IRVariable | None
    else_body: tuple[IRStatement, ...]
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


class IRPattern:
    location: Location


@dataclass(frozen=True, slots=True)
class IRWildcardPattern(IRPattern):
    location: Location


@dataclass(frozen=True, slots=True)
class IRBindingPattern(IRPattern):
    variable: IRVariable
    location: Location


@dataclass(frozen=True, slots=True)
class IRLiteralPattern(IRPattern):
    value: IRLiteral
    location: Location


@dataclass(frozen=True, slots=True)
class IRVariantPattern(IRPattern):
    variant: str
    payload: IRPattern | None
    location: Location


@dataclass(frozen=True, slots=True)
class IRMatchArm:
    pattern: IRPattern
    body: tuple[IRStatement, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRMatch(IRStatement):
    value: IRExpression
    arms: tuple[IRMatchArm, ...]
    exhaustive: bool
    covered_cases: frozenset[str]
    location: Location


@dataclass(frozen=True, slots=True)
class IRForEach(IRStatement):
    variable: IRVariable
    iterable: IRExpression
    body: tuple[IRStatement, ...]
    location: Location


IRStatementLike: TypeAlias = (
    IRLet | IRAssign | IRExpressionStatement | IROutput | IRReturn | IRBlock
    | IRUsing | IRIf | IRWhile | IRLoopControl | IRMatch | IRForEach
)
IRExpressionLike: TypeAlias = (
    IRLiteral | IRLocalRef | IRCall | IRUnary | IRBinary | IRVariant | IRBuiltinCall
    | IRMove | IRCollectionCreate | IRCollectionLength | IRMethodCall
)


@dataclass(frozen=True, slots=True)
class IRFunction:
    name: str
    symbol: str
    parameters: tuple[IRVariable, ...]
    return_type: IRTypeRef
    body: tuple[IRStatement, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRModule:
    functions: tuple[IRFunction, ...]
    entry_symbol: str


def _invalid(location: Location, message: str) -> Diagnostic:
    return Diagnostic(location, "ir", message)


def _validate_type(type_ref: IRTypeRef, location: Location) -> None:
    if isinstance(type_ref, IRType):
        return
    if not isinstance(type_ref, IRAppliedType):
        raise _invalid(location, "unknown IR type representation")
    expected = _APPLIED_ARITIES.get(type_ref.name)
    if expected is None or len(type_ref.arguments) != expected:
        raise _invalid(location, f"invalid applied IR type '{ir_type_name(type_ref)}'")
    for argument in type_ref.arguments:
        _validate_type(argument, location)
        if argument == IRType.VOID:
            raise _invalid(location, "IR generic type arguments cannot have type Void")
    if type_ref.name == "Map" and not _hashable_key_type(type_ref.arguments[0]):
        raise _invalid(location, f"IR Map key type '{ir_type_name(type_ref.arguments[0])}' is not hashable")


def _hashable_key_type(type_ref: IRTypeRef) -> bool:
    if type_ref in {IRType.INT, IRType.FLOAT, IRType.BOOL, IRType.STRING, IRType.BYTE}:
        return True
    if isinstance(type_ref, IRAppliedType) and type_ref.name == "Option":
        return _hashable_key_type(type_ref.arguments[0])
    if isinstance(type_ref, IRAppliedType) and type_ref.name == "Result":
        return all(_hashable_key_type(argument) for argument in type_ref.arguments)
    return False


def _contains_resource(type_ref: IRTypeRef) -> bool:
    resources = {IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER}
    if type_ref in resources:
        return True
    return isinstance(type_ref, IRAppliedType) and any(_contains_resource(item) for item in type_ref.arguments)


def _method_signature(receiver: IRTypeRef, method: str) -> tuple[tuple[IRTypeRef, ...], IRTypeRef] | None:
    if isinstance(receiver, IRType) and receiver == IRType.BYTE:
        return ((), IRType.INT) if method == "toInt" else None
    if isinstance(receiver, IRType) and receiver == IRType.BYTES:
        signatures: dict[str, tuple[tuple[IRTypeRef, ...], IRTypeRef]] = {
            "length": ((), IRType.INT),
            "toString": ((), _applied("Result", IRType.STRING, IRType.STRING)),
            "get": ((IRType.INT,), _applied("Result", IRType.BYTE, IRType.STRING)),
            "slice": ((IRType.INT, IRType.INT), _applied("Result", IRType.BYTES, IRType.STRING)),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.BYTE_BUFFER:
        signatures = {
            "length": ((), IRType.INT),
            "get": ((IRType.INT,), _applied("Result", IRType.BYTE, IRType.STRING)),
            "set": ((IRType.INT, IRType.BYTE), _applied("Result", IRType.BOOL, IRType.STRING)),
            "toBytes": ((), _applied("Result", IRType.BYTES, IRType.STRING)),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver in {IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER}:
        read_all = _applied("Result", IRType.STRING, IRType.STRING)
        write = _applied("Result", IRType.BOOL, IRType.STRING)
        signatures: dict[str, tuple[tuple[IRTypeRef, ...], IRTypeRef]] = {
            "close": ((), IRType.BOOL), "isOpen": ((), IRType.BOOL),
        }
        if receiver in {IRType.TEXT_FILE, IRType.TEXT_READER}:
            signatures["readAll"] = ((), read_all)
        if receiver in {IRType.TEXT_FILE, IRType.TEXT_WRITER}:
            signatures.update({
                "write": ((IRType.STRING,), write),
                "append": ((IRType.STRING,), write),
                "flush": ((), write),
            })
        return signatures.get(method)
    if isinstance(receiver, IRAppliedType) and receiver.name == "List":
        item = receiver.arguments[0]
        signatures = {
            "append": ((item,), IRType.VOID),
            "insert": ((IRType.INT, item), IRType.VOID),
            "get": ((IRType.INT,), _applied("Option", item)),
            "set": ((IRType.INT, item), IRType.VOID),
            "removeAt": ((IRType.INT,), _applied("Option", item)),
            "contains": ((item,), IRType.BOOL),
            "clear": ((), IRType.VOID),
        }
        return signatures.get(method)
    if isinstance(receiver, IRAppliedType) and receiver.name == "Map":
        key, value = receiver.arguments
        signatures = {
            "put": ((key, value), IRType.VOID),
            "get": ((key,), _applied("Option", value)),
            "remove": ((key,), _applied("Option", value)),
            "containsKey": ((key,), IRType.BOOL),
            "keys": ((), _applied("List", key)),
            "values": ((), _applied("List", value)),
            "clear": ((), IRType.VOID),
        }
        return signatures.get(method)
    return None


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
        _validate_type(function.return_type, function.location)
        parameter_ids = [parameter.identifier for parameter in function.parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise _invalid(function.location, "IR function has duplicate parameter identifiers")
        for parameter in function.parameters:
            _validate_type(parameter.type, parameter.location)
            if isinstance(parameter.type, IRAppliedType) and parameter.type.name == "Map" and not _hashable_key_type(parameter.type.arguments[0]):
                raise _invalid(parameter.location, f"IR Map key type '{ir_type_name(parameter.type.arguments[0])}' is not hashable")
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
    if isinstance(statement, IRUsing):
        body = _flow_outcomes(statement.body)
        if statement.error_variable is None:
            return body
        return body | _flow_outcomes(statement.else_body)
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
    if isinstance(statement, IRForEach):
        return {"next"} | (_flow_outcomes(statement.body) & {"return"})
    if isinstance(statement, IRMatch):
        outcomes: set[str] = set()
        for arm in statement.arms:
            outcomes.update(_flow_outcomes(arm.body))
        if not statement.exhaustive:
            outcomes.add("next")
        return outcomes
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
) -> IRTypeRef:
    _validate_type(expression.type, expression.location)
    if isinstance(expression, IRLiteral):
        valid = (
            expression.type == IRType.INT and type(expression.value) is int
            and INT_MIN <= expression.value <= INT_MAX
            or expression.type == IRType.FLOAT and type(expression.value) is float
            and math.isfinite(expression.value)
            or expression.type == IRType.BOOL and type(expression.value) is bool
            or expression.type == IRType.STRING and isinstance(expression.value, str)
            or expression.type == IRType.BYTE and type(expression.value) is int and 0 <= expression.value <= 255
        )
        if not valid:
            raise _invalid(expression.location, f"invalid {ir_type_name(expression.type)} literal in IR")
        return expression.type
    if isinstance(expression, IRLocalRef):
        variable = _find_variable(scopes, expression.variable_id)
        if variable is None:
            raise _invalid(expression.location, "IR references a local outside its scope")
        if variable.type != expression.type:
            raise _invalid(expression.location, "IR local reference type does not match its binding")
        return expression.type
    if isinstance(expression, IRMove):
        operand_type = _validate_expression(expression.operand, functions, scopes)
        if expression.type != operand_type or not _contains_resource(operand_type):
            raise _invalid(expression.location, "IR move must transfer a Resource-containing local value")
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
                left_type == right_type and left_type != IRType.VOID
                or left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
            )
        elif operator in {"<", "<=", ">", ">="}:
            valid = expression.type == IRType.BOOL and left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
        else:
            valid = False
        if not valid:
            raise _invalid(expression.location, f"invalid typed IR binary operation '{operator}'")
        return expression.type
    if isinstance(expression, IRVariant):
        if not isinstance(expression.type, IRAppliedType):
            raise _invalid(expression.location, "IR variant constructor must produce Option or Result")
        expected_payload: IRTypeRef | None
        if expression.type.name == "Option":
            if expression.variant == "Some":
                expected_payload = expression.type.arguments[0]
            elif expression.variant == "None":
                expected_payload = None
            else:
                raise _invalid(expression.location, f"Option has no IR variant '{expression.variant}'")
        elif expression.type.name == "Result":
            if expression.variant == "Ok":
                expected_payload = expression.type.arguments[0]
            elif expression.variant == "Err":
                expected_payload = expression.type.arguments[1]
            else:
                raise _invalid(expression.location, f"Result has no IR variant '{expression.variant}'")
        else:
            raise _invalid(expression.location, "IR variant constructor must produce Option or Result")
        if expected_payload is None:
            if expression.payload is not None:
                raise _invalid(expression.location, f"{expression.variant} cannot have an IR payload")
        elif expression.payload is None or _validate_expression(expression.payload, functions, scopes) != expected_payload:
            raise _invalid(expression.location, f"{expression.variant} IR payload type does not match {ir_type_name(expected_payload)}")
        return expression.type
    if isinstance(expression, IRBuiltinCall):
        argument_types = tuple(_validate_expression(argument, functions, scopes) for argument in expression.arguments)
        signatures: dict[str, tuple[tuple[IRTypeRef, ...], IRTypeRef]] = {
            "hostOS": ((), IRType.STRING),
            "hostArchitecture": ((), IRType.STRING),
            "monotonicMilliseconds": ((), IRType.INT),
            "byteFromInt": ((IRType.INT,), _applied("Result", IRType.BYTE, IRType.STRING)),
            "bytesFromString": ((IRType.STRING,), IRType.BYTES),
            "byteBuffer": ((IRType.INT,), _applied("Result", IRType.BYTE_BUFFER, IRType.STRING)),
            "readText": ((IRType.STRING,), _applied("Result", IRType.STRING, IRType.STRING)),
            "writeText": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.BOOL, IRType.STRING)),
            "readBytes": ((IRType.STRING,), _applied("Result", IRType.BYTES, IRType.STRING)),
            "writeBytes": ((IRType.STRING, IRType.BYTES), _applied("Result", IRType.BOOL, IRType.STRING)),
            "pathJoin": ((IRType.STRING, IRType.STRING), IRType.STRING),
            "pathExists": ((IRType.STRING,), IRType.BOOL),
            "openText": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.TEXT_FILE, IRType.STRING)),
            "openTextReader": ((IRType.STRING,), _applied("Result", IRType.TEXT_READER, IRType.STRING)),
            "openTextWriter": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.TEXT_WRITER, IRType.STRING)),
        }
        signature = signatures.get(expression.builtin)
        if signature is None:
            raise _invalid(expression.location, f"unknown native IR builtin '{expression.builtin}'")
        expected, result = signature
        if argument_types != expected or expression.type != result:
            raise _invalid(expression.location, f"invalid typed IR builtin '{expression.builtin}'")
        return expression.type
    if isinstance(expression, IRCollectionCreate):
        _validate_type(expression.type, expression.location)
        if not isinstance(expression.type, IRAppliedType) or expression.type.name not in {"List", "Map"}:
            raise _invalid(expression.location, "IR collection constructor must produce List or Map")
        if expression.type.name == "Map" and not _hashable_key_type(expression.type.arguments[0]):
            raise _invalid(expression.location, f"IR Map key type '{ir_type_name(expression.type.arguments[0])}' is not hashable")
        return expression.type
    if isinstance(expression, IRCollectionLength):
        receiver = _validate_expression(expression.receiver, functions, scopes)
        if (not isinstance(receiver, IRAppliedType) or receiver.name not in {"List", "Map"}
                or expression.type != IRType.INT):
            raise _invalid(expression.location, "IR collection length requires List or Map and returns Int")
        return expression.type
    if isinstance(expression, IRMethodCall):
        receiver_type = _validate_expression(expression.receiver, functions, scopes)
        expected = _method_signature(receiver_type, expression.method)
        if expected is None:
            raise _invalid(expression.location, f"unsupported IR method '{expression.method}' on {ir_type_name(receiver_type)}")
        parameter_types, result_type = expected
        if len(expression.arguments) != len(parameter_types):
            raise _invalid(expression.location, f"IR method '{expression.method}' has the wrong number of arguments")
        for argument, parameter_type in zip(expression.arguments, parameter_types):
            if _validate_expression(argument, functions, scopes) != parameter_type:
                raise _invalid(argument.location, f"IR method '{expression.method}' argument has the wrong type")
        if expression.type != result_type:
            raise _invalid(expression.location, f"IR method '{expression.method}' result has the wrong type")
        return expression.type
    raise _invalid(expression.location, f"unknown IR expression {type(expression).__name__}")


def _validate_statements(
    statements: tuple[IRStatement, ...],
    return_type: IRTypeRef,
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
            _validate_type(statement.variable.type, statement.variable.location)
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
                IRType.INT, IRType.FLOAT, IRType.BOOL, IRType.STRING, IRType.BYTE,
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
        elif isinstance(statement, IRUsing):
            acquisition_type = _validate_expression(statement.acquisition, functions, scopes)
            expected_result = _applied("Result", statement.resource.type, IRType.STRING)
            if (statement.resource.type not in {
                    IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER,
                } or acquisition_type != expected_result):
                raise _invalid(statement.location, "IR using requires a checked Result<TextResource,String> acquisition")
            if statement.resource.identifier in seen_ids:
                raise _invalid(statement.location, "IR reuses a local identifier")
            scopes.append({statement.resource.identifier: statement.resource})
            seen_ids.add(statement.resource.identifier)
            _validate_statements(statement.body, return_type, functions, scopes, seen_ids, loop_depth)
            scopes.pop()
            if statement.error_variable is None:
                if statement.else_body:
                    raise _invalid(statement.location, "IR using without an error binding cannot have an else body")
            else:
                error = statement.error_variable
                if error.type != IRType.STRING or error.identifier in seen_ids:
                    raise _invalid(statement.location, "IR using error binding must be a fresh String local")
                scopes.append({error.identifier: error})
                seen_ids.add(error.identifier)
                _validate_statements(statement.else_body, return_type, functions, scopes, seen_ids, loop_depth)
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
        elif isinstance(statement, IRForEach):
            iterable_type = _validate_expression(statement.iterable, functions, scopes)
            if not isinstance(iterable_type, IRAppliedType) or iterable_type.name not in {"List", "Map"}:
                raise _invalid(statement.location, "IR for-each iterable must be List or Map")
            expected = iterable_type.arguments[0]
            _validate_type(statement.variable.type, statement.variable.location)
            if statement.variable.type != expected or statement.variable.mutable:
                raise _invalid(statement.location, "IR for-each binding type or mutability does not match its collection")
            if statement.variable.identifier in seen_ids:
                raise _invalid(statement.location, "IR reuses a local identifier")
            scopes.append({statement.variable.identifier: statement.variable})
            seen_ids.add(statement.variable.identifier)
            _validate_statements(statement.body, return_type, functions, scopes, seen_ids, loop_depth + 1)
            scopes.pop()
        elif isinstance(statement, IRMatch):
            subject_type = _validate_expression(statement.value, functions, scopes)
            if not statement.arms:
                raise _invalid(statement.location, "IR match has no arms")
            if isinstance(subject_type, IRAppliedType) and subject_type.name == "Option":
                valid_cases = {"Some", "None"}
                if not statement.covered_cases <= valid_cases:
                    raise _invalid(statement.location, "IR Option match contains an invalid coverage case")
                if statement.exhaustive and statement.covered_cases != valid_cases:
                    raise _invalid(statement.location, "IR Option match is marked exhaustive without covering Some and None")
            elif isinstance(subject_type, IRAppliedType) and subject_type.name == "Result":
                valid_cases = {"Ok", "Err"}
                if not statement.covered_cases <= valid_cases:
                    raise _invalid(statement.location, "IR Result match contains an invalid coverage case")
                if statement.exhaustive and statement.covered_cases != valid_cases:
                    raise _invalid(statement.location, "IR Result match is marked exhaustive without covering Ok and Err")
            for arm in statement.arms:
                scopes.append({})
                _validate_pattern(arm.pattern, subject_type, scopes, seen_ids)
                _validate_statements(arm.body, return_type, functions, scopes, seen_ids, loop_depth)
                scopes.pop()
        elif isinstance(statement, IRLoopControl):
            if statement.kind not in {"break", "continue"} or loop_depth == 0:
                raise _invalid(statement.location, "IR loop control is outside a loop")
        else:
            raise _invalid(statement.location, f"unknown IR statement {type(statement).__name__}")


def _validate_pattern(
    pattern: IRPattern,
    expected_type: IRTypeRef,
    scopes: list[dict[int, IRVariable]],
    seen_ids: set[int],
) -> None:
    if isinstance(pattern, IRWildcardPattern):
        return
    if isinstance(pattern, IRBindingPattern):
        variable = pattern.variable
        _validate_type(variable.type, variable.location)
        if variable.type != expected_type or variable.mutable:
            raise _invalid(pattern.location, f"IR pattern binding type does not match {ir_type_name(expected_type)}")
        if variable.identifier in seen_ids:
            raise _invalid(pattern.location, "IR reuses a local identifier")
        if any(existing.name == variable.name for existing in scopes[-1].values()):
            raise _invalid(pattern.location, f"IR duplicates pattern binding '{variable.name}'")
        scopes[-1][variable.identifier] = variable
        seen_ids.add(variable.identifier)
        return
    if isinstance(pattern, IRLiteralPattern):
        if pattern.value.type != expected_type:
            raise _invalid(pattern.location, f"IR literal pattern type does not match {ir_type_name(expected_type)}")
        _validate_expression(pattern.value, {}, scopes)
        return
    if isinstance(pattern, IRVariantPattern):
        if not isinstance(expected_type, IRAppliedType):
            raise _invalid(pattern.location, "IR variant pattern requires Option or Result")
        if expected_type.name == "Option":
            if pattern.variant == "None":
                if pattern.payload is not None:
                    raise _invalid(pattern.location, "IR None pattern cannot bind a payload")
                return
            if pattern.variant != "Some" or pattern.payload is None:
                raise _invalid(pattern.location, f"invalid IR Option pattern '{pattern.variant}'")
            _validate_pattern(pattern.payload, expected_type.arguments[0], scopes, seen_ids)
            return
        if expected_type.name == "Result":
            if pattern.variant == "Ok":
                payload_type = expected_type.arguments[0]
            elif pattern.variant == "Err":
                payload_type = expected_type.arguments[1]
            else:
                raise _invalid(pattern.location, f"invalid IR Result pattern '{pattern.variant}'")
            if pattern.payload is None:
                raise _invalid(pattern.location, f"IR {pattern.variant} pattern must have a payload pattern")
            _validate_pattern(pattern.payload, payload_type, scopes, seen_ids)
            return
        raise _invalid(pattern.location, "IR variant pattern requires Option or Result")
    raise _invalid(pattern.location, f"unknown IR pattern {type(pattern).__name__}")
