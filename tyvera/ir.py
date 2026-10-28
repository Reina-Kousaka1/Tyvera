"""Typed, backend-neutral IR for Tyvera's native compiler slice.

The IR deliberately describes only expressions and control flow that the
current native backend can lower. It carries Tyvera types and source locations
so later backends do not need to infer language semantics from generated C++.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields as dataclass_fields, is_dataclass
from enum import Enum
from typing import TypeAlias

from .source import Diagnostic, Location
from .typesys import INT_MAX, INT_MIN


class IRType(str, Enum):
    INT = "Int"
    FLOAT = "Float"
    BOOL = "Bool"
    STRING = "String"
    JSON = "Json"
    BYTE = "Byte"
    DURATION = "Duration"
    BYTES = "Bytes"
    BYTE_BUFFER = "ByteBuffer"
    TEXT_FILE = "TextFile"
    TEXT_READER = "TextReader"
    TEXT_WRITER = "TextWriter"
    SOCKET_ADDRESS = "SocketAddress"
    TCP_CONNECTION = "TcpConnection"
    TLS_CONNECTION = "TlsConnection"
    TCP_LISTENER = "TcpListener"
    WEBSOCKET_URL = "WebSocketUrl"
    WEBSOCKET = "WebSocket"
    WEBSOCKET_MESSAGE = "WebSocketMessage"
    HTTP_METHOD = "HttpMethod"
    HTTP_URL = "HttpUrl"
    HTTP_HEADERS = "HttpHeaders"
    HTTP_REQUEST = "HttpRequest"
    HTTP_RESPONSE = "HttpResponse"
    ACTOR_REF = "ActorRef"
    SUPERVISOR = "Supervisor"
    TASK_GROUP = "TaskGroup"
    RESTART_POLICY = "RestartPolicy"
    SUPERVISOR_BACKOFF = "SupervisorBackoff"
    VOID = "Void"


@dataclass(frozen=True, slots=True)
class IRAppliedType:
    """A supported Tyvera generic type applied to already lowered arguments."""

    name: str
    arguments: tuple[IRTypeRef, ...]


@dataclass(frozen=True, slots=True)
class IRActorRefType:
    """A statically typed reference to one checked actor declaration."""

    actor_name: str


@dataclass(frozen=True, slots=True)
class IRUserType:
    """A checked, non-generic Tyvera record or enum type."""

    name: str


IRTypeRef: TypeAlias = IRType | IRAppliedType | IRActorRefType | IRUserType


def ir_type_name(type_ref: IRTypeRef) -> str:
    if isinstance(type_ref, IRType):
        return type_ref.value
    if isinstance(type_ref, IRActorRefType):
        return f"ActorRef<{type_ref.actor_name}>"
    if isinstance(type_ref, IRUserType):
        return type_ref.name
    return f"{type_ref.name}<{','.join(ir_type_name(item) for item in type_ref.arguments)}>"


def _valid_qualified_name(value: str) -> bool:
    """Validate a declared name after an opaque linked-module path prefix."""
    if not value or not value.isprintable():
        return False
    name = value.rpartition(".")[2]
    return name.isidentifier()


def _applied(name: str, *arguments: IRTypeRef) -> IRAppliedType:
    return IRAppliedType(name, tuple(arguments))


NUMERIC_TYPES = frozenset({IRType.INT, IRType.FLOAT})
_APPLIED_ARITIES = {"Option": 1, "Result": 2, "List": 1, "Map": 2, "Task": 1, "Secret": 1, "ReplySender": 1, "ReplyReceiver": 1}


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


@dataclass(frozen=True, slots=True)
class IRRecordCreate(IRExpression):
    record_name: str
    field_types: tuple[IRTypeRef, ...]
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRFieldAccess(IRExpression):
    receiver: IRExpression
    record_name: str
    field_index: int
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IREnumCreate(IRExpression):
    enum_name: str
    variant_name: str
    variant_index: int
    payload_types: tuple[IRTypeRef, ...]
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRActorSelfRef(IRExpression):
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRActorSpawn(IRExpression):
    actor_name: str
    initializer_symbol: str | None
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
    location: Location
    restartable: bool = False


@dataclass(frozen=True, slots=True)
class IRActorSend(IRExpression):
    receiver: IRExpression
    actor_name: str
    handler_symbol: str
    arguments: tuple[IRExpression, ...]
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRActorRequest(IRExpression):
    receiver: IRExpression
    actor_name: str
    handler_symbol: str
    arguments: tuple[IRExpression, ...]
    timeout: IRExpression | None
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
class IRRecordPattern(IRPattern):
    record_name: str
    field_types: tuple[IRTypeRef, ...]
    fields: tuple[IRPattern, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IREnumPattern(IRPattern):
    enum_name: str
    variant_name: str
    variant_index: int
    payload_types: tuple[IRTypeRef, ...]
    payload_patterns: tuple[IRPattern, ...]
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


@dataclass(frozen=True, slots=True)
class IRAwait(IRStatement):
    task: IRExpression
    result: IRVariable | None
    location: Location


IRStatementLike: TypeAlias = (
    IRLet | IRAssign | IRExpressionStatement | IROutput | IRReturn | IRBlock
    | IRUsing | IRIf | IRWhile | IRLoopControl | IRMatch | IRForEach | IRAwait
)
IRExpressionLike: TypeAlias = (
    IRLiteral | IRLocalRef | IRCall | IRUnary | IRBinary | IRVariant | IRBuiltinCall
    | IRMove | IRCollectionCreate | IRCollectionLength | IRMethodCall
    | IRRecordCreate | IRFieldAccess | IREnumCreate
    | IRActorSelfRef | IRActorSpawn | IRActorSend
)


@dataclass(frozen=True, slots=True)
class IRFunction:
    name: str
    symbol: str
    parameters: tuple[IRVariable, ...]
    return_type: IRTypeRef
    body: tuple[IRStatement, ...]
    location: Location
    is_async: bool = False
    actor_name: str | None = None
    actor_initializer: bool = False


@dataclass(frozen=True, slots=True)
class IRRecordField:
    name: str
    type: IRTypeRef
    location: Location


@dataclass(frozen=True, slots=True)
class IRRecord:
    name: str
    fields: tuple[IRRecordField, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IREnumVariant:
    name: str
    payload_types: tuple[IRTypeRef, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IREnum:
    name: str
    variants: tuple[IREnumVariant, ...]
    location: Location


@dataclass(frozen=True, slots=True)
class IRActor:
    name: str
    fields: tuple[IRVariable, ...]
    handler_symbols: tuple[tuple[str, str], ...]
    initializer_symbol: str | None
    location: Location


@dataclass(frozen=True, slots=True)
class IRModule:
    functions: tuple[IRFunction, ...]
    entry_symbol: str
    actors: tuple[IRActor, ...] = ()
    records: tuple[IRRecord, ...] = ()
    enums: tuple[IREnum, ...] = ()


def _invalid(location: Location, message: str) -> Diagnostic:
    return Diagnostic(location, "ir", message)


def _validate_type(type_ref: IRTypeRef, location: Location) -> None:
    if isinstance(type_ref, IRType):
        return
    if isinstance(type_ref, IRActorRefType):
        if not _valid_qualified_name(type_ref.actor_name):
            raise _invalid(location, "invalid IR ActorRef target")
        return
    if isinstance(type_ref, IRUserType):
        if not _valid_qualified_name(type_ref.name):
            raise _invalid(location, "invalid IR user-defined type name")
        return
    if not isinstance(type_ref, IRAppliedType):
        raise _invalid(location, "unknown IR type representation")
    expected = _APPLIED_ARITIES.get(type_ref.name)
    if expected is None or len(type_ref.arguments) != expected:
        raise _invalid(location, f"invalid applied IR type '{ir_type_name(type_ref)}'")
    for argument in type_ref.arguments:
        _validate_type(argument, location)
        if argument == IRType.VOID and type_ref.name != "Task":
            raise _invalid(location, "IR generic type arguments cannot have type Void")
    if type_ref.name == "Map" and not _hashable_key_type(type_ref.arguments[0]):
        raise _invalid(location, f"IR Map key type '{ir_type_name(type_ref.arguments[0])}' is not hashable")
    if type_ref.name == "Secret" and type_ref.arguments != (IRType.STRING,):
        raise _invalid(location, "native IR supports only Secret<String>")


def _hashable_key_type(type_ref: IRTypeRef) -> bool:
    if type_ref in {IRType.INT, IRType.FLOAT, IRType.BOOL, IRType.STRING, IRType.JSON, IRType.BYTE, IRType.DURATION}:
        return True
    if isinstance(type_ref, IRAppliedType) and type_ref.name == "Option":
        return _hashable_key_type(type_ref.arguments[0])
    if isinstance(type_ref, IRAppliedType) and type_ref.name == "Result":
        return all(_hashable_key_type(argument) for argument in type_ref.arguments)
    return False


def _contains_resource(type_ref: IRTypeRef) -> bool:
    resources = {
        IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER,
        IRType.TCP_CONNECTION, IRType.TLS_CONNECTION, IRType.TCP_LISTENER, IRType.WEBSOCKET,
    }
    if type_ref in resources:
        return True
    return isinstance(type_ref, IRAppliedType) and any(_contains_resource(item) for item in type_ref.arguments)


def _contains_task(type_ref: IRTypeRef) -> bool:
    if type_ref == IRType.TASK_GROUP:
        return True
    if isinstance(type_ref, IRAppliedType):
        return type_ref.name == "Task" or any(_contains_task(item) for item in type_ref.arguments)
    return False


def _method_signature(receiver: IRTypeRef, method: str) -> tuple[tuple[IRTypeRef, ...], IRTypeRef] | None:
    if isinstance(receiver, IRAppliedType) and receiver.name == "ReplySender":
        return {"send": ((receiver.arguments[0],), IRType.BOOL)}.get(method)
    if isinstance(receiver, IRAppliedType) and receiver.name == "ReplyReceiver":
        item = receiver.arguments[0]
        signatures = {
            "isReady": ((), IRType.BOOL),
            "isCancelled": ((), IRType.BOOL),
            "isTimedOut": ((), IRType.BOOL),
            "cancel": ((), IRType.BOOL),
            "demonitor": ((), IRType.BOOL),
            "tryReceive": ((), _applied("Result", _applied("Option", item), IRType.STRING)),
        }
        return signatures.get(method)
    if isinstance(receiver, IRActorRefType):
        return {
            "isAlive": ((), IRType.BOOL),
            "monitor": ((), _applied("ReplyReceiver", IRType.STRING)),
            "stop": ((), IRType.VOID),
        }.get(method)
    if receiver == IRType.SUPERVISOR:
        return {"isAlive": ((), IRType.BOOL), "stop": ((), IRType.VOID)}.get(method)
    if receiver == IRType.TASK_GROUP:
        return {"isDone": ((), IRType.BOOL), "cancel": ((), IRType.INT)}.get(method)
    if isinstance(receiver, IRAppliedType) and receiver.name == "Task":
        signatures = {
            "isDone": ((), IRType.BOOL),
            "isCancelled": ((), IRType.BOOL),
            "cancel": ((), IRType.BOOL),
            "withTimeout": ((IRType.DURATION,), _applied("Task", receiver.arguments[0])),
            "tryResult": ((), _applied(
                "Result", IRType.BOOL if receiver.arguments[0] == IRType.VOID
                else _applied("Option", receiver.arguments[0]), IRType.STRING,
            )),
        }
        return signatures.get(method)
    if isinstance(receiver, IRAppliedType) and receiver.name == "Secret":
        return {"reveal": ((), IRType.STRING)}.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.BYTE:
        return ((), IRType.INT) if method == "toInt" else None
    if isinstance(receiver, IRType) and receiver == IRType.JSON:
        signatures = {
            "kind": ((), IRType.STRING),
            "isNull": ((), IRType.BOOL),
            "asString": ((), _applied("Option", IRType.STRING)),
            "asInt": ((), _applied("Option", IRType.INT)),
            "asFloat": ((), _applied("Option", IRType.FLOAT)),
            "asBool": ((), _applied("Option", IRType.BOOL)),
            "arrayLength": ((), _applied("Option", IRType.INT)),
            "objectLength": ((), _applied("Option", IRType.INT)),
            "arrayAt": ((IRType.INT,), _applied("Option", IRType.JSON)),
            "objectGet": ((IRType.STRING,), _applied("Option", IRType.JSON)),
            "objectKeys": ((), _applied("Option", _applied("List", IRType.STRING))),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.DURATION:
        return ((), IRType.INT) if method == "inMilliseconds" else None
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
    if isinstance(receiver, IRType) and receiver == IRType.SOCKET_ADDRESS:
        signatures = {
            "host": ((), IRType.STRING),
            "port": ((), IRType.INT),
            "family": ((), IRType.STRING),
            "toString": ((), IRType.STRING),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.TCP_CONNECTION:
        signatures = {
            "read": ((IRType.INT,), _applied("Task", _applied("Result", _applied("Option", IRType.BYTES), IRType.STRING))),
            "write": ((IRType.BYTES,), _applied("Task", _applied("Result", IRType.INT, IRType.STRING))),
            "writeAll": ((IRType.BYTES,), _applied("Task", _applied("Result", IRType.INT, IRType.STRING))),
            "localAddress": ((), _applied("Result", IRType.SOCKET_ADDRESS, IRType.STRING)),
            "remoteAddress": ((), _applied("Result", IRType.SOCKET_ADDRESS, IRType.STRING)),
            "close": ((), IRType.BOOL),
            "isOpen": ((), IRType.BOOL),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.TLS_CONNECTION:
        signatures = {
            "read": ((IRType.INT,), _applied("Task", _applied("Result", _applied("Option", IRType.BYTES), IRType.STRING))),
            "write": ((IRType.BYTES,), _applied("Task", _applied("Result", IRType.INT, IRType.STRING))),
            "writeAll": ((IRType.BYTES,), _applied("Task", _applied("Result", IRType.INT, IRType.STRING))),
            "localAddress": ((), _applied("Result", IRType.SOCKET_ADDRESS, IRType.STRING)),
            "remoteAddress": ((), _applied("Result", IRType.SOCKET_ADDRESS, IRType.STRING)),
            "close": ((), IRType.BOOL),
            "isOpen": ((), IRType.BOOL),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.TCP_LISTENER:
        signatures = {
            "accept": ((), _applied("Task", _applied("Result", IRType.TCP_CONNECTION, IRType.STRING))),
            "localAddress": ((), _applied("Result", IRType.SOCKET_ADDRESS, IRType.STRING)),
            "close": ((), IRType.BOOL),
            "isOpen": ((), IRType.BOOL),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.WEBSOCKET_URL:
        signatures = {
            "scheme": ((), IRType.STRING), "host": ((), IRType.STRING),
            "port": ((), IRType.INT), "target": ((), IRType.STRING),
            "hostEndsWith": ((IRType.STRING,), IRType.BOOL),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.WEBSOCKET_MESSAGE:
        signatures = {
            "kind": ((), IRType.STRING), "bytes": ((), IRType.BYTES),
            "text": ((), _applied("Result", IRType.STRING, IRType.STRING)),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.HTTP_METHOD:
        signatures = {"name": ((), IRType.STRING), "isStandard": ((), IRType.BOOL)}
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.HTTP_URL:
        signatures = {
            "scheme": ((), IRType.STRING), "host": ((), IRType.STRING),
            "port": ((), IRType.INT), "target": ((), IRType.STRING),
            "authority": ((), IRType.STRING),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.HTTP_HEADERS:
        signatures = {
            "add": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.BOOL, IRType.STRING)),
            "set": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.BOOL, IRType.STRING)),
            "remove": ((IRType.STRING,), IRType.INT),
            "get": ((IRType.STRING,), _applied("Option", IRType.STRING)),
            "values": ((IRType.STRING,), _applied("List", IRType.STRING)),
            "contains": ((IRType.STRING,), IRType.BOOL),
            "length": ((), IRType.INT),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.HTTP_REQUEST:
        signatures = {
            "method": ((), IRType.HTTP_METHOD), "target": ((), IRType.STRING),
            "headers": ((), IRType.HTTP_HEADERS), "body": ((), IRType.BYTES),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.HTTP_RESPONSE:
        signatures = {
            "status": ((), IRType.INT), "headers": ((), IRType.HTTP_HEADERS),
            "body": ((), IRType.BYTES),
        }
        return signatures.get(method)
    if isinstance(receiver, IRType) and receiver == IRType.WEBSOCKET:
        signatures = {
            "sendText": ((IRType.STRING,), _applied("Task", _applied("Result", IRType.INT, IRType.STRING))),
            "send": ((IRType.BYTES,), _applied("Task", _applied("Result", IRType.INT, IRType.STRING))),
            "receive": ((), _applied("Task", _applied("Result", _applied("Option", IRType.WEBSOCKET_MESSAGE), IRType.STRING))),
            "receiveWithin": ((IRType.DURATION,), _applied("Task", _applied("Result", _applied("Option", IRType.WEBSOCKET_MESSAGE), IRType.STRING))),
            "closeCode": ((), _applied("Option", IRType.INT)),
            "closeReason": ((), _applied("Option", IRType.STRING)),
            "close": ((), IRType.BOOL),
            "closeGracefully": ((), _applied("Task", _applied("Result", IRType.BOOL, IRType.STRING))),
            "isOpen": ((), IRType.BOOL),
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
    actors = {actor.name: actor for actor in module.actors}
    if len(actors) != len(module.actors):
        raise _invalid(module.actors[0].location, "duplicate IR actor declaration")
    records = {record.name: record for record in module.records}
    enums = {enum.name: enum for enum in module.enums}
    if (len(records) != len(module.records) or len(enums) != len(module.enums)
            or set(records) & set(enums)):
        location = (module.records or module.enums)[0].location
        raise _invalid(location, "duplicate IR record or enum declaration")
    user_type_names = set(records) | set(enums)
    for declaration in (*module.records, *module.enums):
        if not _valid_qualified_name(declaration.name):
            raise _invalid(declaration.location, "invalid IR user-defined type name")
    for record in module.records:
        names: set[str] = set()
        for field in record.fields:
            _validate_type(field.type, field.location)
            if field.type == IRType.VOID or field.name in names:
                raise _invalid(field.location, "invalid or duplicate IR record field")
            if _contains_resource(field.type) or _contains_task(field.type):
                raise _invalid(field.location, "IR records cannot contain Resources or Tasks")
            names.add(field.name)
    for enum in module.enums:
        names: set[str] = set()
        for variant in enum.variants:
            if variant.name in names:
                raise _invalid(variant.location, "duplicate IR enum variant")
            for payload_type in variant.payload_types:
                _validate_type(payload_type, variant.location)
                if payload_type == IRType.VOID or _contains_resource(payload_type) or _contains_task(payload_type):
                    raise _invalid(variant.location, "invalid IR enum payload type")
            names.add(variant.name)
    for node in _walk_ir(module):
        if isinstance(node, IRUserType) and node.name not in user_type_names:
            raise _invalid(Location("<ir>", 1, 1), f"IR refers to undeclared user type '{node.name}'")
    for actor in module.actors:
        if not _valid_qualified_name(actor.name):
            raise _invalid(actor.location, "invalid IR actor name")
        fields_by_name: set[str] = set()
        fields_by_id: set[int] = set()
        for field in actor.fields:
            _validate_type(field.type, field.location)
            if isinstance(field.type, IRActorRefType) and field.type.actor_name not in actors:
                raise _invalid(field.location, f"IR actor field refers to unknown actor '{field.type.actor_name}'")
            if field.type == IRType.VOID or field.name in fields_by_name or field.identifier in fields_by_id:
                raise _invalid(field.location, "invalid or duplicate IR actor state field")
            if field.identifier >= 0:
                raise _invalid(field.location, "IR actor state field identifiers must be negative")
            fields_by_name.add(field.name)
            fields_by_id.add(field.identifier)
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
    if entry.parameters or entry.return_type != IRType.VOID or entry.is_async:
        raise _invalid(entry.location, "IR main must have no parameters and return Void")
    if module.entry_symbol not in symbols:
        raise _invalid(module.functions[0].location, "IR entry symbol does not exist")

    for actor in module.actors:
        declared: set[str] = set()
        symbols_for_handlers: set[str] = set()
        for method_name, symbol in actor.handler_symbols:
            function = symbols.get(symbol)
            if (method_name == "init" or method_name in declared or symbol in symbols_for_handlers
                    or function is None or function.actor_name != actor.name or function.actor_initializer):
                raise _invalid(actor.location, f"IR actor handler '{method_name}' has an invalid definition")
            declared.add(method_name)
            symbols_for_handlers.add(symbol)
        if actor.initializer_symbol is not None:
            initializer = symbols.get(actor.initializer_symbol)
            if (initializer is None or initializer.actor_name != actor.name
                    or not initializer.actor_initializer):
                raise _invalid(actor.location, "IR actor initializer has an invalid definition")

    for function in module.functions:
        for node in _walk_ir(function):
            if isinstance(node, IRActorRefType) and node.actor_name not in actors:
                raise _invalid(function.location, f"IR refers to unknown actor '{node.actor_name}'")
            if isinstance(node, IRActorSpawn):
                actor = actors.get(node.actor_name)
                if actor is None or node.initializer_symbol != actor.initializer_symbol:
                    raise _invalid(node.location, f"IR spawn refers to unknown actor '{node.actor_name}'")
                if node.restartable and any(
                    _contains_resource(field.type)
                    or _contains_task(field.type)
                    for field in actor.fields
                ):
                    raise _invalid(node.location, "native supervised actors cannot own Resources or Tasks")
            if isinstance(node, IRActorSend):
                actor = actors.get(node.actor_name)
                if actor is None or (node.handler_symbol not in dict(actor.handler_symbols).values()):
                    raise _invalid(node.location, f"IR message refers to unknown handler on '{node.actor_name}'")
            if isinstance(node, IRActorRequest):
                actor = actors.get(node.actor_name)
                if actor is None or node.handler_symbol not in dict(actor.handler_symbols).values():
                    raise _invalid(node.location, f"IR request refers to unknown handler on '{node.actor_name}'")

    for node in _walk_ir(module):
        if isinstance(node, IRRecordCreate):
            record = records.get(node.record_name)
            if (record is None or node.type != IRUserType(node.record_name)
                    or tuple(field.type for field in record.fields) != node.field_types):
                raise _invalid(node.location, f"IR record construction does not match '{node.record_name}'")
        elif isinstance(node, IRFieldAccess):
            record = records.get(node.record_name)
            if (record is None or not 0 <= node.field_index < len(record.fields)
                    or node.type != record.fields[node.field_index].type):
                raise _invalid(node.location, f"IR field access does not match '{node.record_name}'")
        elif isinstance(node, IREnumCreate):
            enum = enums.get(node.enum_name)
            if (enum is None or not 0 <= node.variant_index < len(enum.variants)):
                raise _invalid(node.location, f"IR enum construction refers to an unknown variant of '{node.enum_name}'")
            variant = enum.variants[node.variant_index]
            if (node.type != IRUserType(node.enum_name) or variant.name != node.variant_name
                    or variant.payload_types != node.payload_types):
                raise _invalid(node.location, f"IR enum construction does not match '{node.enum_name}.{node.variant_name}'")
        elif isinstance(node, IRRecordPattern):
            record = records.get(node.record_name)
            if (record is None or tuple(field.type for field in record.fields) != node.field_types):
                raise _invalid(node.location, f"IR record pattern does not match '{node.record_name}'")
        elif isinstance(node, IREnumPattern):
            enum = enums.get(node.enum_name)
            if enum is None or not 0 <= node.variant_index < len(enum.variants):
                raise _invalid(node.location, f"IR enum pattern refers to an unknown variant of '{node.enum_name}'")
            variant = enum.variants[node.variant_index]
            if (variant.name != node.variant_name or variant.payload_types != node.payload_types):
                raise _invalid(node.location, f"IR enum pattern does not match '{node.enum_name}.{node.variant_name}'")

    for function in module.functions:
        if function.actor_name is not None:
            actor = actors.get(function.actor_name)
            if actor is None or function.is_async or function.return_type != IRType.VOID:
                raise _invalid(function.location, "IR actor handler has an invalid owner or signature")
            is_initializer = function.symbol == actor.initializer_symbol
            if function.actor_initializer != is_initializer:
                raise _invalid(function.location, "IR actor initializer metadata does not match its declaration")
            fields = {field.identifier: field for field in actor.fields}
            parameters = {parameter.identifier: parameter for parameter in function.parameters}
            if set(fields) & set(parameters):
                raise _invalid(function.location, "IR actor parameter identifier collides with a state field")
            scopes = [fields, parameters]
            seen_ids = set(fields) | set(parameters)
            _validate_statements(function.body, IRType.VOID, symbols, scopes, seen_ids, 0, False)
            continue
        scopes = [{parameter.identifier: parameter for parameter in function.parameters}]
        seen_ids = set(scopes[0])
        _validate_statements(function.body, function.return_type, symbols, scopes, seen_ids, 0, function.is_async)
        if function.return_type != IRType.VOID and "next" in _flow_outcomes(function.body):
            raise _invalid(function.location, f"IR function '{function.name}' can fall through without returning {function.return_type.value}")


def _walk_ir(value):
    """Yield nodes in an IR dataclass tree for module-level reference checks."""
    if isinstance(value, tuple):
        for item in value:
            yield from _walk_ir(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_ir(item)
    elif is_dataclass(value):
        yield value
        for field in dataclass_fields(value):
            yield from _walk_ir(getattr(value, field.name))


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
        expected_result = _applied("Task", function.return_type) if function.is_async else function.return_type
        if expression.type != expected_result:
            raise _invalid(expression.location, f"IR call result for '{function.name}' has the wrong type")
        return expression.type
    if isinstance(expression, IRActorSelfRef):
        if not isinstance(expression.type, IRActorRefType):
            raise _invalid(expression.location, "IR actor self reference must have ActorRef type")
        return expression.type
    if isinstance(expression, IRActorSpawn):
        if not isinstance(expression.type, IRActorRefType) or expression.type.actor_name != expression.actor_name:
            raise _invalid(expression.location, "IR actor spawn result type does not match its actor")
        actual = tuple(_validate_expression(argument, functions, scopes) for argument in expression.arguments)
        if expression.initializer_symbol is None:
            if expression.arguments:
                raise _invalid(expression.location, "IR actor without an initializer cannot receive constructor arguments")
        else:
            initializer = functions.get(expression.initializer_symbol)
            if (initializer is None or initializer.actor_name != expression.actor_name
                    or not initializer.actor_initializer):
                raise _invalid(expression.location, "IR actor spawn has no matching initializer")
            expected = tuple(parameter.type for parameter in initializer.parameters)
            if actual != expected:
                raise _invalid(expression.location, "IR actor constructor arguments do not match its initializer")
        if expression.restartable and any(
            _contains_resource(argument_type) or _contains_task(argument_type)
            for argument_type in actual
        ):
            raise _invalid(expression.location, "native supervised actors cannot receive Resources or Tasks")
        return expression.type
    if isinstance(expression, IRActorSend):
        receiver_type = _validate_expression(expression.receiver, functions, scopes)
        if receiver_type != IRActorRefType(expression.actor_name) or expression.type != IRType.VOID:
            raise _invalid(expression.location, "IR actor message receiver or result type is invalid")
        handler = functions.get(expression.handler_symbol)
        if (handler is None or handler.actor_name != expression.actor_name or handler.actor_initializer
                or not handler.name.startswith(expression.actor_name + ".")):
            raise _invalid(expression.location, "IR actor message has no matching handler")
        expected = tuple(parameter.type for parameter in handler.parameters)
        actual = tuple(_validate_expression(argument, functions, scopes) for argument in expression.arguments)
        if actual != expected:
            raise _invalid(expression.location, "IR actor message arguments do not match its handler")
        return expression.type
    if isinstance(expression, IRActorRequest):
        receiver_type = _validate_expression(expression.receiver, functions, scopes)
        if receiver_type != IRActorRefType(expression.actor_name):
            raise _invalid(expression.location, "IR actor request receiver type does not match its actor")
        handler = functions.get(expression.handler_symbol)
        if (handler is None or handler.actor_name != expression.actor_name or handler.actor_initializer
                or not handler.name.startswith(expression.actor_name + ".")):
            raise _invalid(expression.location, "IR actor request has no matching handler")
        if not handler.parameters:
            raise _invalid(expression.location, "IR actor request handler must end with ReplySender<T>")
        sender_type = handler.parameters[-1].type
        if (not isinstance(sender_type, IRAppliedType) or sender_type.name != "ReplySender"
                or len(sender_type.arguments) != 1):
            raise _invalid(expression.location, "IR actor request handler must end with ReplySender<T>")
        item_type = sender_type.arguments[0]
        if expression.type != _applied("ReplyReceiver", item_type):
            raise _invalid(expression.location, "IR actor request result type does not match its reply")
        if expression.timeout is not None and _validate_expression(expression.timeout, functions, scopes) != IRType.DURATION:
            raise _invalid(expression.timeout.location, "IR actor request timeout must have Duration type")
        actual = tuple(_validate_expression(argument, functions, scopes) for argument in expression.arguments)
        expected = tuple(parameter.type for parameter in handler.parameters[:-1])
        if actual != expected:
            raise _invalid(expression.location, "IR actor request arguments do not match its handler")
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
        elif operator in {"+", "-"} and left_type == right_type == expression.type == IRType.DURATION:
            valid = True
        elif operator in {"==", "!="}:
            valid = expression.type == IRType.BOOL and (
                left_type == right_type and left_type != IRType.VOID
                or left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
            )
        elif operator in {"<", "<=", ">", ">="}:
            valid = expression.type == IRType.BOOL and (
                left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
                or left_type == right_type == IRType.DURATION
            )
        else:
            valid = False
        if not valid:
            raise _invalid(expression.location, f"invalid typed IR binary operation '{operator}'")
        return expression.type
    if isinstance(expression, IRRecordCreate):
        if expression.type != IRUserType(expression.record_name) or len(expression.arguments) != len(expression.field_types):
            raise _invalid(expression.location, "IR record constructor has an invalid nominal type or arity")
        for field_type, argument in zip(expression.field_types, expression.arguments):
            _validate_type(field_type, expression.location)
            if _validate_expression(argument, functions, scopes) != field_type:
                raise _invalid(argument.location, "IR record field initializer has the wrong type")
        return expression.type
    if isinstance(expression, IRFieldAccess):
        receiver_type = _validate_expression(expression.receiver, functions, scopes)
        if receiver_type != IRUserType(expression.record_name) or expression.field_index < 0 or expression.type == IRType.VOID:
            raise _invalid(expression.location, "IR record field access has an invalid receiver or field")
        return expression.type
    if isinstance(expression, IREnumCreate):
        if (expression.type != IRUserType(expression.enum_name) or expression.variant_index < 0
                or len(expression.arguments) != len(expression.payload_types)):
            raise _invalid(expression.location, "IR enum constructor has an invalid nominal type or arity")
        for payload_type, argument in zip(expression.payload_types, expression.arguments):
            _validate_type(payload_type, expression.location)
            if _validate_expression(argument, functions, scopes) != payload_type:
                raise _invalid(argument.location, "IR enum payload has the wrong type")
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
        if expression.builtin in {"oneForOne", "oneForAll", "restForOne"}:
            expected = ((IRType.INT, IRType.INT) if len(argument_types) == 2 else
                        (IRType.INT, IRType.INT, IRType.SUPERVISOR_BACKOFF))
            if argument_types != expected or expression.type != IRType.SUPERVISOR:
                raise _invalid(expression.location, "invalid native Supervisor strategy constructor")
            return expression.type
        signatures: dict[str, tuple[tuple[IRTypeRef, ...], IRTypeRef]] = {
            "hostOS": ((), IRType.STRING),
            "hostArchitecture": ((), IRType.STRING),
            "monotonicMilliseconds": ((), IRType.INT),
            "environment": ((IRType.STRING,), _applied("Option", _applied("Secret", IRType.STRING))),
            "milliseconds": ((IRType.INT,), IRType.DURATION),
            "seconds": ((IRType.INT,), IRType.DURATION),
            "durationFromSeconds": ((IRType.FLOAT,), _applied("Result", IRType.DURATION, IRType.STRING)),
            "sleepFor": ((IRType.DURATION,), _applied("Task", IRType.VOID)),
            "byteFromInt": ((IRType.INT,), _applied("Result", IRType.BYTE, IRType.STRING)),
            "bytesFromString": ((IRType.STRING,), IRType.BYTES),
            "byteBuffer": ((IRType.INT,), _applied("Result", IRType.BYTE_BUFFER, IRType.STRING)),
            "readText": ((IRType.STRING,), _applied("Result", IRType.STRING, IRType.STRING)),
            "writeText": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.BOOL, IRType.STRING)),
            "readBytes": ((IRType.STRING,), _applied("Result", IRType.BYTES, IRType.STRING)),
            "writeBytes": ((IRType.STRING, IRType.BYTES), _applied("Result", IRType.BOOL, IRType.STRING)),
            "pathJoin": ((IRType.STRING, IRType.STRING), IRType.STRING),
            "pathExists": ((IRType.STRING,), IRType.BOOL),
            "parseJson": ((IRType.STRING,), _applied("Result", IRType.JSON, IRType.STRING)),
            "jsonStringify": ((IRType.JSON,), _applied("Result", IRType.STRING, IRType.STRING)),
            "jsonString": ((IRType.STRING,), IRType.JSON),
            "jsonInt": ((IRType.INT,), IRType.JSON),
            "jsonFloat": ((IRType.FLOAT,), IRType.JSON),
            "jsonBool": ((IRType.BOOL,), IRType.JSON),
            "jsonNull": ((), IRType.JSON),
            "jsonArray": ((_applied("List", IRType.JSON),), IRType.JSON),
            "jsonObject": ((_applied("Map", IRType.STRING, IRType.JSON),), IRType.JSON),
            "openText": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.TEXT_FILE, IRType.STRING)),
            "openTextReader": ((IRType.STRING,), _applied("Result", IRType.TEXT_READER, IRType.STRING)),
            "openTextWriter": ((IRType.STRING, IRType.STRING), _applied("Result", IRType.TEXT_WRITER, IRType.STRING)),
            "runTasks": ((), _applied("Result", IRType.INT, IRType.STRING)),
            "taskGroup": ((), IRType.TASK_GROUP),
            "runActors": ((), _applied("Result", IRType.INT, IRType.STRING)),
            "oneForOne": ((IRType.INT, IRType.INT), IRType.SUPERVISOR),
            "oneForAll": ((IRType.INT, IRType.INT), IRType.SUPERVISOR),
            "restForOne": ((IRType.INT, IRType.INT), IRType.SUPERVISOR),
            "permanentChildPolicy": ((), IRType.RESTART_POLICY),
            "transientChildPolicy": ((), IRType.RESTART_POLICY),
            "temporaryChildPolicy": ((), IRType.RESTART_POLICY),
            "exponentialBackoff": ((IRType.INT, IRType.INT), IRType.SUPERVISOR_BACKOFF),
            "resolveHost": ((IRType.STRING, IRType.INT), _applied("Task", _applied("Result", _applied("List", IRType.SOCKET_ADDRESS), IRType.STRING))),
            "tcpConnect": ((IRType.STRING, IRType.INT), _applied("Task", _applied("Result", IRType.TCP_CONNECTION, IRType.STRING))),
            "tlsConnect": ((IRType.STRING, IRType.INT), _applied("Task", _applied("Result", IRType.TLS_CONNECTION, IRType.STRING))),
            "tcpListen": ((IRType.STRING, IRType.INT, IRType.INT), _applied("Task", _applied("Result", IRType.TCP_LISTENER, IRType.STRING))),
            "webSocketUrl": ((IRType.STRING,), _applied("Result", IRType.WEBSOCKET_URL, IRType.STRING)),
            "httpMethod": ((IRType.STRING,), _applied("Result", IRType.HTTP_METHOD, IRType.STRING)),
            "httpUrl": ((IRType.STRING,), _applied("Result", IRType.HTTP_URL, IRType.STRING)),
            "httpHeaders": ((), IRType.HTTP_HEADERS),
            "httpRequest": ((IRType.HTTP_METHOD, IRType.STRING, IRType.HTTP_HEADERS, IRType.BYTES),
                            _applied("Result", IRType.HTTP_REQUEST, IRType.STRING)),
        }
        if expression.builtin in {"httpSend", "httpGet", "httpPost", "httpPut", "httpDelete"}:
            expected_result = _applied("Task", _applied("Result", IRType.HTTP_RESPONSE, IRType.STRING))
            if expression.builtin in {"httpSend"}:
                expected = (IRType.HTTP_URL, IRType.HTTP_REQUEST, IRType.DURATION)
            elif expression.builtin in {"httpGet", "httpDelete"}:
                expected = (IRType.HTTP_URL, IRType.DURATION)
            else:
                expected = (IRType.HTTP_URL, IRType.HTTP_HEADERS, IRType.BYTES, IRType.DURATION)
            if argument_types != expected or expression.type != expected_result:
                raise _invalid(expression.location, f"invalid typed IR builtin '{expression.builtin}'")
            return expression.type
        if expression.builtin == "webSocketConnect":
            expected_result = _applied("Task", _applied("Result", IRType.WEBSOCKET, IRType.STRING))
            if (len(argument_types) != 2 or argument_types[0] not in {IRType.STRING, IRType.WEBSOCKET_URL}
                    or argument_types[1] != IRType.DURATION or expression.type != expected_result):
                raise _invalid(expression.location, "invalid typed IR builtin 'webSocketConnect'")
            return expression.type
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
        if receiver_type == IRType.TASK_GROUP and expression.method == "add":
            if len(expression.arguments) != 1:
                raise _invalid(expression.location, "IR TaskGroup.add has the wrong number of arguments")
            task_type = _validate_expression(expression.arguments[0], functions, scopes)
            if not isinstance(task_type, IRAppliedType) or task_type.name != "Task" or len(task_type.arguments) != 1:
                raise _invalid(expression.arguments[0].location, "IR TaskGroup.add requires Task<T>")
            if expression.type != IRType.BOOL:
                raise _invalid(expression.location, "IR TaskGroup.add must return Bool")
            return expression.type
        if receiver_type == IRType.SUPERVISOR and expression.method == "start":
            if len(expression.arguments) not in {1, 2}:
                raise _invalid(expression.location, "IR Supervisor.start has the wrong number of arguments")
            child_type = _validate_expression(expression.arguments[0], functions, scopes)
            if isinstance(child_type, IRActorRefType):
                expected_result = child_type
            elif child_type == IRType.SUPERVISOR:
                expected_result = IRType.SUPERVISOR
            else:
                raise _invalid(expression.arguments[0].location, "IR Supervisor.start child must be an ActorRef or Supervisor")
            if len(expression.arguments) == 2 and _validate_expression(
                    expression.arguments[1], functions, scopes) != IRType.RESTART_POLICY:
                raise _invalid(expression.arguments[1].location, "IR Supervisor.start policy must be RestartPolicy")
            if expression.type != expected_result:
                raise _invalid(expression.location, "IR Supervisor.start result type does not match its child")
            return expression.type
        if isinstance(receiver_type, IRActorRefType) and expression.method in {"link", "unlink"}:
            if len(expression.arguments) != 1:
                raise _invalid(expression.location, f"IR ActorRef.{expression.method} expects one ActorRef argument")
            argument_type = _validate_expression(expression.arguments[0], functions, scopes)
            if not isinstance(argument_type, IRActorRefType):
                raise _invalid(expression.arguments[0].location, f"IR ActorRef.{expression.method} argument must be an ActorRef")
            if expression.type != IRType.BOOL:
                raise _invalid(expression.location, f"IR ActorRef.{expression.method} result must be Bool")
            return expression.type
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
    is_async: bool = False,
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
            _validate_statements(statement.statements, return_type, functions, scopes, seen_ids, loop_depth, is_async)
            scopes.pop()
        elif isinstance(statement, IRUsing):
            acquisition_type = _validate_expression(statement.acquisition, functions, scopes)
            expected_result = _applied("Result", statement.resource.type, IRType.STRING)
            if (statement.resource.type not in {
                    IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER,
                    IRType.TCP_CONNECTION, IRType.TLS_CONNECTION, IRType.TCP_LISTENER,
                    IRType.WEBSOCKET,
                } or acquisition_type != expected_result):
                raise _invalid(statement.location, "IR using requires a checked file or network Result acquisition")
            if statement.resource.identifier in seen_ids:
                raise _invalid(statement.location, "IR reuses a local identifier")
            scopes.append({statement.resource.identifier: statement.resource})
            seen_ids.add(statement.resource.identifier)
            _validate_statements(statement.body, return_type, functions, scopes, seen_ids, loop_depth, is_async)
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
                _validate_statements(statement.else_body, return_type, functions, scopes, seen_ids, loop_depth, is_async)
                scopes.pop()
        elif isinstance(statement, IRIf):
            if _validate_expression(statement.condition, functions, scopes) != IRType.BOOL:
                raise _invalid(statement.location, "IR if condition must be Bool")
            scopes.append({})
            _validate_statements(statement.then_body, return_type, functions, scopes, seen_ids, loop_depth, is_async)
            scopes.pop()
            scopes.append({})
            _validate_statements(statement.else_body, return_type, functions, scopes, seen_ids, loop_depth, is_async)
            scopes.pop()
        elif isinstance(statement, IRWhile):
            if _validate_expression(statement.condition, functions, scopes) != IRType.BOOL:
                raise _invalid(statement.location, "IR while condition must be Bool")
            scopes.append({})
            _validate_statements(statement.body, return_type, functions, scopes, seen_ids, loop_depth + 1, is_async)
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
            _validate_statements(statement.body, return_type, functions, scopes, seen_ids, loop_depth + 1, is_async)
            scopes.pop()
        elif isinstance(statement, IRAwait):
            if not is_async:
                raise _invalid(statement.location, "IR await is only valid inside an async function")
            awaitable_type = _validate_expression(statement.task, functions, scopes)
            if awaitable_type == IRType.TASK_GROUP:
                if statement.result is not None:
                    raise _invalid(statement.location, "IR await of TaskGroup cannot bind a result")
                result_type = IRType.VOID
            else:
                if (not isinstance(awaitable_type, IRAppliedType)
                        or awaitable_type.name not in {"Task", "ReplyReceiver"}
                        or len(awaitable_type.arguments) != 1):
                    raise _invalid(statement.location, "IR await operand must be Task<T>, TaskGroup, or ReplyReceiver<T>")
                result_type = awaitable_type.arguments[0]
            if statement.result is None and _contains_resource(result_type):
                raise _invalid(statement.location, "IR await of a Resource result requires a result binding")
            if statement.result is not None:
                result = statement.result
                _validate_type(result.type, result.location)
                if result.type != result_type or result.type == IRType.VOID:
                    raise _invalid(statement.location, "IR await binding type does not match awaitable result")
                if result.identifier in seen_ids:
                    raise _invalid(statement.location, "IR await reuses a local identifier")
                scopes[-1][result.identifier] = result
                seen_ids.add(result.identifier)
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
                _validate_statements(arm.body, return_type, functions, scopes, seen_ids, loop_depth, is_async)
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
    if isinstance(pattern, IRRecordPattern):
        if expected_type != IRUserType(pattern.record_name) or len(pattern.fields) != len(pattern.field_types):
            raise _invalid(pattern.location, "IR record pattern has an invalid nominal type or arity")
        for field_type, child in zip(pattern.field_types, pattern.fields):
            _validate_type(field_type, pattern.location)
            _validate_pattern(child, field_type, scopes, seen_ids)
        return
    if isinstance(pattern, IREnumPattern):
        if (expected_type != IRUserType(pattern.enum_name) or pattern.variant_index < 0
                or len(pattern.payload_patterns) != len(pattern.payload_types)):
            raise _invalid(pattern.location, "IR enum pattern has an invalid nominal type or arity")
        for payload_type, child in zip(pattern.payload_types, pattern.payload_patterns):
            _validate_type(payload_type, pattern.location)
            _validate_pattern(child, payload_type, scopes, seen_ids)
        return
    raise _invalid(pattern.location, f"unknown IR pattern {type(pattern).__name__}")

