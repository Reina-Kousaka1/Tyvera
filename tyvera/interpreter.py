from __future__ import annotations

import json
import math
import errno
import selectors
import socket
import ssl
import sys
from collections import ChainMap, deque
from dataclasses import dataclass, field as dataclass_field
from itertools import count
from pathlib import Path
from typing import Callable, TextIO

from .ast import (
    ActorDeclaration,
    Assignment,
    AwaitStatement,
    Binary,
    Block,
    Call,
    ClassDeclaration,
    Expression,
    ExpressionStatement,
    EnumPattern,
    Function,
    ForStatement,
    IfStatement,
    LetStatement,
    Literal,
    LoopControl,
    BindingPattern,
    LiteralPattern,
    MatchStatement,
    MemberAccess,
    MemberCall,
    Method,
    Name,
    Program,
    ReturnStatement,
    RecordPattern,
    NewtypeDeclaration,
    Statement,
    TupleExpression,
    TuplePattern,
    Unary,
    UsingStatement,
    VariantPattern,
    WildcardPattern,
    WhileStatement,
)
from .source import Diagnostic, Location
from .secrets import SecretProvider, SecretProviderError
from .io_backend import IOBackend, IOBackendError
from .platform_runtime import (
    ShutdownController,
    ShutdownDiagnostic,
    detect_host_platform,
    environment_value,
    monotonic_seconds,
    monotonic_sleep,
)
from .supervision import (
    CrashKind,
    CrashReason,
    ExponentialBackoff,
    RestartPolicy,
    RestartStrategy,
    RuntimeEvent,
    RuntimeEventKind,
)
from .http_protocol import (
    HttpHeaders, HttpLimits, HttpMessageDecoder, HttpMethod, HttpProtocolError,
    HttpRequest, HttpResponse, HttpRouter, HttpUrl, encode_request, encode_response,
    make_method, parse_url, text_response,
)
from .websocket_protocol import (
    MAX_WEBSOCKET_HANDSHAKE_BYTES,
    WebSocketEvent,
    WebSocketFrameDecoder,
    WebSocketProtocolError,
    WebSocketUrl,
    create_client_handshake,
    encode_client_frame,
    parse_websocket_url,
    validate_close_payload,
    validate_server_handshake,
)
from .typesys import INT_MAX, INT_MIN, canonical_type
from .type_names import split_type


_MISSING = object()
_MOVED = object()
_MAX_ACTOR_MAILBOX = 100_000
_MAX_ACTOR_DELIVERIES = 100_000
_MAX_SUPERVISOR_RESTARTS = 1_000
_MAX_TASK_STEPS = 100_000
_MAX_NETWORK_READ = 1_048_576
_MAX_BYTE_BUFFER_LENGTH = 64 * 1024 * 1024
_IDENTITY_SEQUENCE = count(1)
_HASH_MASK = (1 << 64) - 1


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
    actor: _TyveraActor | None = None
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))


@dataclass(slots=True)
class _ActorMessage:
    method: str
    arguments: list[object]
    location: Location


@dataclass(slots=True, eq=False, repr=False)
class _TyveraReplyCell:
    state: str = "pending"
    value: object = None
    error: str | None = None
    started_at: float | None = None
    timeout_milliseconds: int | None = None
    clock: Callable[[], float] | None = None
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<ReplyChannel>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraReplySender:
    cell: _TyveraReplyCell

    def __repr__(self) -> str:
        return "<ReplySender>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraReplyReceiver:
    cell: _TyveraReplyCell

    def __repr__(self) -> str:
        return "<ReplyReceiver>"


@dataclass(slots=True, frozen=True, order=True, repr=False)
class _TyveraDuration:
    milliseconds: int

    def __repr__(self) -> str:
        return "<Duration>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraTimer:
    started_at: float
    duration_milliseconds: int
    clock: Callable[[], float]
    cancelled: bool = False
    scheduled_actor: _TyveraActor | None = None
    scheduled_method: str | None = None
    scheduled_arguments: list[object] = dataclass_field(default_factory=list)
    scheduled_location: Location | None = None
    delivered: bool = False
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<Timer>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraTextFile:
    stream: TextIO
    mode: str
    resource_type: str = "TextFile"
    closed: bool = False
    scoped: bool = False
    location: Location | None = None
    pending_operations: int = 0
    close_requested: bool = False
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return f"<{self.resource_type}>"


@dataclass(slots=True, frozen=True, repr=False)
class _TyveraBytes:
    value: bytes

    def __repr__(self) -> str:
        return "<Bytes>"


@dataclass(slots=True, frozen=True, repr=False)
class _TyveraByte:
    value: int

    def __repr__(self) -> str:
        return f"Byte({self.value})"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraByteBuffer:
    value: bytearray

    def __repr__(self) -> str:
        return "<ByteBuffer>"


@dataclass(slots=True, frozen=True, repr=False)
class _TyveraSocketAddress:
    host: str
    port: int
    family: str

    def __repr__(self) -> str:
        return "<SocketAddress>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraNetworkResource:
    stream: socket.socket
    resource_type: str
    closed: bool = False
    scoped: bool = False
    location: Location | None = None
    pending_read: bool = False
    pending_write: bool = False
    pending_read_task: _TyveraTask | None = None
    pending_write_task: _TyveraTask | None = None
    websocket_decoder: WebSocketFrameDecoder | None = None
    operation_timeout_milliseconds: int = 0
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return f"<{self.resource_type}>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraHttpServer:
    listener: _TyveraNetworkResource
    router: HttpRouter
    limits: HttpLimits
    timeout_milliseconds: int
    local_address: _TyveraSocketAddress
    stopped: bool = False
    closed: bool = False
    run_task: _TyveraTask | None = None
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<HttpServer>"


@dataclass(slots=True, frozen=True, repr=False)
class _TyveraWebSocketMessage:
    opcode: int
    payload: bytes

    def __repr__(self) -> str:
        return "<WebSocketMessage redacted>"


_TyveraResource = _TyveraTextFile | _TyveraNetworkResource | _TyveraHttpServer


@dataclass(slots=True, eq=False)
class _AsyncBlockFrame:
    body: list[Statement]
    variables: ChainMap[str, object]
    index: int = 0


@dataclass(slots=True, eq=False)
class _AsyncLoopFrame:
    statement: WhileStatement | ForStatement
    variables: ChainMap[str, object]
    depth: int
    items: tuple[object, ...] | None = None
    index: int = 0


@dataclass(slots=True, eq=False)
class _AsyncResourceFrame:
    resource: _TyveraResource
    location: Location


@dataclass(slots=True, eq=False, repr=False)
class _TyveraTask:
    function: Function | None
    variables: ChainMap[str, object] | None
    location: Location
    depth: int = 0
    result_type: str = "Void"
    state: str = "runnable"
    frames: list[_AsyncBlockFrame | _AsyncLoopFrame | _AsyncResourceFrame] = dataclass_field(default_factory=list)
    wait_target: object | None = None
    wait_frame: _AsyncBlockFrame | None = None
    wait_statement: AwaitStatement | None = None
    due_at: float | None = None
    timeout_target: _TyveraTask | None = None
    error: str | None = None
    result: object = None
    parent: _TyveraTask | None = None
    owner_actor: _TyveraActor | None = None
    children: list[_TyveraTask] = dataclass_field(default_factory=list)
    io_cancel: Callable[[], None] | None = None
    io_resource: object | None = None
    native_step: Callable[[_TyveraTask], None] | None = None
    native_state: dict[str, object] = dataclass_field(default_factory=dict)
    native_wait_result: object = _MISSING
    native_wait_error: str | None = None
    native_resources: list[object] = dataclass_field(default_factory=list)
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<Task>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraTaskGroup:
    owner: _TyveraTask | None
    tasks: list[_TyveraTask] = dataclass_field(default_factory=list)
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<TaskGroup>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraSupervisor:
    max_restarts: int
    window_milliseconds: int
    clock: Callable[[], float]
    strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE
    backoff: ExponentialBackoff | None = None
    parent: _TyveraSupervisor | None = None
    restart_policy: RestartPolicy = RestartPolicy.PERMANENT
    restart_times: deque[float] = dataclass_field(default_factory=deque)
    children: list[_TyveraActor | _TyveraSupervisor] = dataclass_field(default_factory=list)
    status: str = "alive"
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<Supervisor>"


@dataclass(slots=True, eq=False)
class _TyveraActor:
    instance: _TyveraInstance
    status: str = "alive"
    mailbox: deque[_ActorMessage] = dataclass_field(default_factory=deque)
    queued: bool = False
    supervisor: _TyveraSupervisor | None = None
    restart_policy: RestartPolicy = RestartPolicy.PERMANENT
    declaration: ActorDeclaration | None = None
    constructor_arguments: list[object] = dataclass_field(default_factory=list)
    pending_replies: set[_TyveraReplyCell] = dataclass_field(default_factory=set)
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))


@dataclass(slots=True)
class _PendingSupervisorRestart:
    supervisor: _TyveraSupervisor
    children: tuple[_TyveraActor | _TyveraSupervisor, ...]
    trigger: _TyveraActor | _TyveraSupervisor
    failure: str
    due_at: float
    location: Location
    depth: int


@dataclass(slots=True, eq=False, repr=False)
class _TyveraActorRef:
    actor: _TyveraActor

    def __repr__(self) -> str:
        return "<ActorRef>"


@dataclass(slots=True, eq=False)
class _TyveraList:
    values: list[object]


@dataclass(slots=True, frozen=True, eq=False)
class _TyveraTuple:
    values: tuple[object, ...]


@dataclass(slots=True, eq=False)
class _TyveraRecord:
    type_name: str
    fields: dict[str, object]


@dataclass(slots=True, frozen=True, eq=False)
class _TyveraNewtype:
    type_name: str
    value: object


@dataclass(slots=True, eq=False)
class _TyveraMap:
    entries: list[tuple[int, object, object]] = dataclass_field(default_factory=list)

    def _find(self, key: object, key_hash: int | None = None) -> int | None:
        hash_value = _tyvera_hash(key) if key_hash is None else key_hash
        for index, (entry_hash, entry_key, _) in enumerate(self.entries):
            if entry_hash == hash_value and _tyvera_equal(entry_key, key):
                return index
        return None

    def put(self, key: object, value: object) -> None:
        key_hash = _tyvera_hash(key)
        index = self._find(key, key_hash)
        if index is None:
            self.entries.append((key_hash, key, value))
        else:
            _, entry_key, _ = self.entries[index]
            self.entries[index] = (key_hash, entry_key, value)

    def get(self, key: object, default: object = _MISSING) -> object:
        index = self._find(key)
        return default if index is None else self.entries[index][2]

    def remove(self, key: object) -> object:
        index = self._find(key)
        if index is None:
            return _MISSING
        return self.entries.pop(index)[2]

    def contains_key(self, key: object) -> bool:
        return self._find(key) is not None

    def keys(self) -> list[object]:
        return [key for _, key, _ in self.entries]

    def values(self) -> list[object]:
        return [value for _, _, value in self.entries]

    def clear(self) -> None:
        self.entries.clear()

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(slots=True, eq=False)
class _TyveraVariant:
    variant: str
    payload: object = None
    enum_name: str | None = None
    payload_arity: int | None = None


@dataclass(slots=True, eq=False, repr=False)
class _TyveraSecret:
    value: str
    identity: int = dataclass_field(default_factory=lambda: next(_IDENTITY_SEQUENCE))

    def __repr__(self) -> str:
        return "<Secret redacted>"


@dataclass(slots=True, frozen=True, repr=False)
class _TyveraJson:
    kind: str
    value: object

    def __repr__(self) -> str:
        return "<Json value redacted>"


def _hash_bytes(data: bytes) -> int:
    result = 0xCBF29CE484222325
    for byte in data:
        result = ((result ^ byte) * 0x100000001B3) & _HASH_MASK
    return result


def _encode_hash_integer(value: int) -> bytes:
    magnitude = abs(value)
    width = max(1, (magnitude.bit_length() + 7) // 8)
    return (b"-" if value < 0 else b"+") + width.to_bytes(4, "big") + magnitude.to_bytes(width, "big")


def _hash_parts(tag: bytes, *parts: int) -> int:
    data = bytearray(tag)
    data.append(0)
    for part in parts:
        encoded = _encode_hash_integer(int(part))
        data.extend(len(encoded).to_bytes(4, "big"))
        data.extend(encoded)
    return _hash_bytes(bytes(data))


def _hash_number(numerator: int, denominator: int = 1) -> int:
    return _hash_parts(b"number", numerator, denominator)


def _tyvera_equal(left: object, right: object, seen: set[tuple[int, int]] | None = None) -> bool:
    """Tyvera equality, independent of Python dataclass and container equality."""
    if type(left) is bool or type(right) is bool:
        return type(left) is bool and type(right) is bool and left is right
    if type(left) is int and type(right) is int:
        return left == right
    if type(left) is float and type(right) is float:
        return left == right
    if type(left) is int and type(right) is float:
        return right.is_integer() and int(right) == left
    if type(left) is float and type(right) is int:
        return left.is_integer() and int(left) == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if left is None or right is None:
        return left is None and right is None
    if type(left) is not type(right):
        return False
    if isinstance(left, _TyveraDuration):
        return left.milliseconds == right.milliseconds
    if isinstance(left, _TyveraInstance):
        return left.identity == right.identity
    if isinstance(left, _TyveraActorRef):
        return left.actor.identity == right.actor.identity
    if isinstance(left, _TyveraTask):
        return left.identity == right.identity
    if isinstance(left, (_TyveraTimer, _TyveraSupervisor)):
        return left.identity == right.identity
    if isinstance(left, (_TyveraReplySender, _TyveraReplyReceiver)):
        return left.cell.identity == right.cell.identity
    if isinstance(left, _TyveraTaskGroup):
        return left.identity == right.identity
    if isinstance(left, _TyveraTextFile):
        return left.identity == right.identity
    if isinstance(left, _TyveraNetworkResource):
        return left.identity == right.identity
    if isinstance(left, _TyveraBytes):
        return left.value == right.value
    if isinstance(left, _TyveraByte):
        return left.value == right.value
    if isinstance(left, _TyveraSocketAddress):
        return (left.host, left.port, left.family) == (right.host, right.port, right.family)
    if isinstance(left, _TyveraSecret):
        return left.identity == right.identity
    if isinstance(left, _TyveraList):
        if len(left.values) != len(right.values):
            return False
        pairs = set() if seen is None else seen
        pair = (id(left), id(right))
        if pair in pairs:
            return True
        pairs.add(pair)
        try:
            return all(_tyvera_equal(a, b, pairs) for a, b in zip(left.values, right.values))
        finally:
            pairs.remove(pair)
    if isinstance(left, _TyveraTuple):
        return (len(left.values) == len(right.values)
                and all(_tyvera_equal(a, b, seen) for a, b in zip(left.values, right.values)))
    if isinstance(left, _TyveraRecord):
        return (left.type_name == right.type_name and left.fields.keys() == right.fields.keys()
                and all(_tyvera_equal(left.fields[name], right.fields[name], seen) for name in left.fields))
    if isinstance(left, _TyveraNewtype):
        return left.type_name == right.type_name and _tyvera_equal(left.value, right.value, seen)
    if isinstance(left, _TyveraMap):
        if len(left.entries) != len(right.entries):
            return False
        pairs = set() if seen is None else seen
        pair = (id(left), id(right))
        if pair in pairs:
            return True
        pairs.add(pair)
        try:
            return all(
                any(_tyvera_equal(key, other_key, pairs) and _tyvera_equal(value, other_value, pairs)
                    for _, other_key, other_value in right.entries)
                for _, key, value in left.entries
            )
        finally:
            pairs.remove(pair)
    if isinstance(left, _TyveraVariant):
        return (left.enum_name == right.enum_name and left.variant == right.variant
                and left.payload_arity == right.payload_arity
                and _tyvera_equal(left.payload, right.payload, seen))
    if isinstance(left, _TyveraJson):
        if left.kind != right.kind:
            return False
        if left.kind == "array":
            return len(left.value) == len(right.value) and all(
                _tyvera_equal(a, b, seen) for a, b in zip(left.value, right.value)
            )
        if left.kind == "object":
            return (left.value.keys() == right.value.keys()
                    and all(_tyvera_equal(left.value[key], right.value[key], seen) for key in left.value))
        return _tyvera_equal(left.value, right.value, seen)
    return left is right


def _tyvera_hash(value: object) -> int:
    """Stable 64-bit hash for statically permitted Tyvera Map key values."""
    if value is None:
        return _hash_bytes(b"unit")
    if type(value) is bool:
        return _hash_bytes(b"bool\0true" if value else b"bool\0false")
    if type(value) is int:
        return _hash_number(value)
    if type(value) is float:
        numerator, denominator = value.as_integer_ratio()
        return _hash_number(numerator, denominator)
    if isinstance(value, str):
        return _hash_bytes(b"string\0" + value.encode("utf-8"))
    if isinstance(value, _TyveraDuration):
        return _hash_parts(b"duration", value.milliseconds)
    if isinstance(value, _TyveraTuple):
        children = [_tyvera_hash(item) for item in value.values]
        return _hash_parts(b"tuple", len(children), *children)
    if isinstance(value, _TyveraRecord):
        parts = [part for name, item in sorted(value.fields.items())
                 for part in (_hash_bytes(name.encode("utf-8")), _tyvera_hash(item))]
        return _hash_parts(b"record-" + value.type_name.encode("utf-8"), len(value.fields), *parts)
    if isinstance(value, _TyveraNewtype):
        return _hash_parts(b"newtype-" + value.type_name.encode("utf-8"), _tyvera_hash(value.value))
    if isinstance(value, _TyveraInstance):
        return _hash_parts(b"object-identity", value.identity)
    if isinstance(value, _TyveraActorRef):
        return _hash_parts(b"actor-identity", value.actor.identity)
    if isinstance(value, _TyveraTask):
        return _hash_parts(b"task-identity", value.identity)
    if isinstance(value, _TyveraTextFile):
        return _hash_parts(b"resource-identity", value.identity)
    if isinstance(value, _TyveraNetworkResource):
        return _hash_parts(b"resource-identity", value.identity)
    if isinstance(value, _TyveraBytes):
        return _hash_bytes(b"bytes\0" + value.value)
    if isinstance(value, _TyveraByte):
        return _hash_bytes(b"byte\0" + bytes((value.value,)))
    if isinstance(value, _TyveraSocketAddress):
        return _hash_bytes(b"socket-address\0" + value.host.encode("ascii", "strict")
                           + b"\0" + str(value.port).encode("ascii")
                           + b"\0" + value.family.encode("ascii"))
    if isinstance(value, _TyveraVariant):
        return _hash_parts(b"variant", _hash_bytes((value.enum_name or "").encode("utf-8")),
                           _hash_bytes(value.variant.encode("utf-8")),
                           -1 if value.payload_arity is None else value.payload_arity,
                           _tyvera_hash(value.payload))
    if isinstance(value, _TyveraJson):
        if value.kind == "array":
            children = [_tyvera_hash(item) for item in value.value]
            return _hash_parts(b"json-array", len(children), *children)
        if value.kind == "object":
            children = [(_hash_bytes(key.encode("utf-8")), _tyvera_hash(item))
                        for key, item in value.value.items()]
            children.sort()
            return _hash_parts(b"json-object", len(children), *(part for pair in children for part in pair))
        if value.kind == "float":
            return _hash_parts(b"json-float", _tyvera_hash(value.value))
        return _hash_parts(b"json-" + value.kind.encode("ascii"), _tyvera_hash(value.value))
    raise TypeError("value is not a hashable Tyvera key")


class Interpreter:
    def __init__(
        self, program: Program, output: TextIO, max_call_depth: int = 256, *,
        project_root: str | Path | None = None, error_output: TextIO | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        shutdown_controller: ShutdownController | None = None,
        runtime_event_sink: Callable[[RuntimeEvent], None] | None = None,
    ) -> None:
        self.functions = {function.name: function for function in program.functions}
        self.classes: dict[str, ClassDeclaration] = {declaration.name: declaration for declaration in program.classes}
        self.records = {declaration.name: declaration for declaration in program.records}
        self.enums = {declaration.name: declaration for declaration in program.enums}
        self.newtypes = {declaration.name: declaration for declaration in program.newtypes}
        self.actors: dict[str, ActorDeclaration] = {declaration.name: declaration for declaration in program.actors}
        self._ready_actors: deque[_TyveraActor] = deque()
        self._actors: list[_TyveraActor] = []
        self._supervisors: list[_TyveraSupervisor] = []
        self._pending_supervisor_restarts: list[_PendingSupervisorRestart] = []
        self._scheduled_timers: list[_TyveraTimer] = []
        self._ready_tasks: deque[_TyveraTask] = deque()
        self._tasks: list[_TyveraTask] = []
        self._task_groups: list[_TyveraTaskGroup] = []
        self._current_actor: _TyveraActor | None = None
        self._current_task: _TyveraTask | None = None
        self._running_tasks = False
        self._runtime_state = "ready"
        self._scheduler_state = "idle"
        self.output = output
        self.error_output = error_output if error_output is not None else sys.stderr
        self.max_call_depth = max_call_depth
        self.project_root = Path.cwd() if project_root is None else Path(project_root)
        self.secret_provider = SecretProvider(self.project_root)
        self._clock = monotonic_seconds if clock is None else clock
        self._sleep = monotonic_sleep if sleeper is None else sleeper
        self._io_backend: IOBackend | None = None
        self._shutdown_controller = shutdown_controller
        self._runtime_event_sink = runtime_event_sink
        self._dead_letters: deque[RuntimeEvent] = deque(maxlen=1024)
        if shutdown_controller is not None:
            shutdown_controller.set_waker(
                lambda: self._io_backend.wake() if self._io_backend is not None else None,
            )

    def run(self) -> None:
        main = self.functions["main"]
        if self._runtime_state != "ready":
            raise Diagnostic(main.location, "runtime", "interpreter runtime can only run once")
        self._runtime_state = "running"
        try:
            self._call(main, [], main.location, 0)
            self._check_shutdown(main.location)
        finally:
            self._shutdown_runtime()

    def _check_shutdown(self, location: Location) -> None:
        if self._shutdown_controller is not None:
            self._shutdown_controller.raise_if_requested(location)

    def _emit_runtime_event(self, event: RuntimeEvent) -> None:
        if self._runtime_event_sink is not None:
            try:
                self._runtime_event_sink(event)
            except Exception:
                # Observability must never change application behavior.
                pass

    def _shutdown_runtime(self) -> None:
        if self._runtime_state == "closed":
            return
        self._runtime_state = "shutting_down"
        for task in self._tasks:
            self._cancel_task(task)
        self._pending_supervisor_restarts.clear()
        for supervisor in reversed(self._supervisors):
            if supervisor.parent is None and supervisor.status in {"alive", "restarting"}:
                self._stop_supervisor_tree(supervisor, "runtime shut down")
        for actor in reversed(self._actors):
            if actor.status in {"alive", "starting", "restarting"}:
                self._stop_actor(actor, "runtime shut down before actor replied")
        self._ready_tasks.clear()
        self._ready_actors.clear()
        for timer in self._scheduled_timers:
            timer.cancelled = True
        self._scheduled_timers.clear()
        self._task_groups.clear()
        for task in self._tasks:
            self._cleanup_task_result(task)
        if self._io_backend is not None:
            self._io_backend.shutdown()
        self._scheduler_state = "closed"
        self._runtime_state = "closed"
        self._emit_runtime_event(RuntimeEvent(RuntimeEventKind.RUNTIME_SHUTDOWN, "runtime"))

    def _call(self, function: Function, arguments: list[object], call_location: Location, depth: int) -> object:
        if depth >= self.max_call_depth:
            error = Diagnostic(call_location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
            self._cleanup_failed_argument_transfer(arguments, error)
            raise error
        variables = ChainMap({parameter.name: value for parameter, value in zip(function.parameters, arguments)})
        try:
            self._execute_block(function.body, variables, depth)
        except _ReturnSignal as signal:
            return signal.value
        except RecursionError as error:
            raise Diagnostic(call_location, "runtime", "bootstrap runtime stack limit exceeded") from error
        return None

    def _execute_block(self, body: list[Statement], variables: ChainMap[str, object], depth: int) -> None:
        try:
            self._execute_block_inner(body, variables, depth)
        except Exception as primary:
            errors = self._cleanup_owned_scope(variables.maps[0])
            if not errors:
                raise
            cleanup_message = "; ".join(
                f"{location.filename}:{location.line}:{location.column}: {message}"
                for location, message in errors
            )
            if isinstance(primary, Diagnostic):
                if isinstance(primary, ShutdownDiagnostic):
                    raise primary.with_cleanup_failure(cleanup_message) from primary
                raise Diagnostic(primary.location, primary.stage,
                                 f"{primary.message}; resource cleanup also failed: {cleanup_message}") from primary
            location, message = errors[0]
            raise Diagnostic(location, "runtime", f"resource cleanup failed: {message}; {cleanup_message}") from primary
        errors = self._cleanup_owned_scope(variables.maps[0])
        if errors:
            location, message = errors[0]
            remaining = "; ".join(item for _, item in errors[1:])
            suffix = f"; additional cleanup failures: {remaining}" if remaining else ""
            raise Diagnostic(location, "runtime", f"resource cleanup failed: {message}{suffix}")

    def _execute_block_inner(self, body: list[Statement], variables: ChainMap[str, object], depth: int) -> None:
        for statement in body:
            self._check_shutdown(statement.location)
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
            elif isinstance(statement, AwaitStatement):
                raise AssertionError("await statements are dispatched by the cooperative task scheduler")
            elif isinstance(statement, Block):
                self._execute_block(statement.body, variables.new_child(), depth)
            elif isinstance(statement, UsingStatement):
                acquisition = self._evaluate(statement.resource, variables, depth)
                if not isinstance(acquisition, _TyveraVariant):
                    raise AssertionError("checked using acquisition did not return Result")
                if acquisition.variant == "Err":
                    if statement.error_name is None:
                        raise Diagnostic(statement.location, "runtime", str(acquisition.payload))
                    error_scope = variables.new_child()
                    error_scope[statement.error_name] = acquisition.payload
                    self._execute_block(statement.else_body, error_scope, depth)
                elif acquisition.variant == "Ok":
                    resource = acquisition.payload
                    if not isinstance(resource, (_TyveraTextFile, _TyveraNetworkResource)):
                        raise AssertionError("checked using acquisition is not a Resource")
                    resource.scoped = True
                    scope = variables.new_child()
                    scope[statement.name] = resource
                    try:
                        self._execute_block(statement.body, scope, depth)
                    except Exception as primary:
                        cleanup_error = self._close_resource(resource)
                        if cleanup_error is None:
                            raise
                        if isinstance(primary, Diagnostic):
                            if isinstance(primary, ShutdownDiagnostic):
                                raise primary.with_cleanup_failure(cleanup_error) from primary
                            raise Diagnostic(primary.location, primary.stage,
                                             f"{primary.message}; resource cleanup also failed: {cleanup_error}") from primary
                        raise Diagnostic(statement.location, "runtime",
                                         f"resource cleanup failed: {cleanup_error}") from primary
                    cleanup_error = self._close_resource(resource)
                    if cleanup_error is not None:
                        raise Diagnostic(statement.location, "runtime", f"resource cleanup failed: {cleanup_error}")
                else:
                    raise AssertionError("checked using acquisition returned neither Ok nor Err")
            elif isinstance(statement, IfStatement):
                body = statement.then_body if self._evaluate(statement.condition, variables, depth) else statement.else_body
                self._execute_block(body, variables.new_child(), depth)
            elif isinstance(statement, WhileStatement):
                while True:
                    self._check_shutdown(statement.location)
                    if not self._evaluate(statement.condition, variables, depth):
                        break
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
                    items = tuple(iterable.keys())
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
            elif isinstance(statement, MatchStatement):
                value = self._evaluate(statement.value, variables, depth)
                for arm in statement.arms:
                    matched, pattern_bindings = self._matches(arm.pattern, value)
                    if not matched:
                        continue
                    arm_variables = variables.new_child()
                    arm_variables.update(pattern_bindings)
                    self._execute_block(arm.body, arm_variables, depth)
                    break
            elif isinstance(statement, LoopControl):
                raise _LoopSignal(statement.kind)
            elif isinstance(statement, ExpressionStatement):
                self._evaluate(statement.expression, variables, depth)
            else:
                raise AssertionError(f"unhandled statement node: {type(statement).__name__}")

    def _evaluate_arguments(
        self, expressions: list[Expression], variables: ChainMap[str, object], depth: int,
    ) -> list[object]:
        arguments: list[object] = []
        try:
            for expression in expressions:
                arguments.append(self._evaluate(expression, variables, depth))
        except Exception as primary:
            self._cleanup_failed_argument_transfer(arguments, primary)
            raise
        return arguments

    def _cleanup_failed_argument_transfer(self, arguments: list[object], primary: Exception) -> None:
        scopes = {f"argument_{index}": value for index, value in enumerate(arguments)}
        errors = self._cleanup_owned_scope(scopes)
        if not errors:
            return
        cleanup_message = "; ".join(
            f"{location.filename}:{location.line}:{location.column}: {message}"
            for location, message in errors
        )
        if isinstance(primary, Diagnostic):
            raise Diagnostic(primary.location, primary.stage,
                             f"{primary.message}; argument resource cleanup also failed: {cleanup_message}") from primary
        location, message = errors[0]
        raise Diagnostic(location, "runtime",
                         f"argument resource cleanup failed: {message}; {cleanup_message}") from primary

    def _evaluate(self, expression: Expression, variables: ChainMap[str, object], depth: int) -> object:
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, TupleExpression):
            return _TyveraTuple(tuple(self._evaluate(element, variables, depth) for element in expression.elements))
        if isinstance(expression, Name):
            value = variables[expression.identifier]
            if value is _MOVED:
                raise Diagnostic(expression.location, "runtime", f"resource '{expression.identifier}' was already moved")
            return value
        if isinstance(expression, Call):
            if expression.callee == "taskGroup":
                if self._current_actor is not None:
                    raise Diagnostic(expression.location, "runtime", "task groups cannot be created inside actor message handlers")
                group = _TyveraTaskGroup(self._current_task)
                self._task_groups.append(group)
                return group
            if expression.callee in {"hostOS", "hostArchitecture", "monotonicMilliseconds"}:
                if expression.callee == "monotonicMilliseconds":
                    return int(self._clock() * 1000)
                platform = detect_host_platform()
                return (platform.operating_system.value if expression.callee == "hostOS"
                        else platform.architecture.value)
            if expression.callee == "environment":
                name = self._evaluate(expression.arguments[0], variables, depth)
                value = environment_value(name)
                if value is None:
                    return _TyveraVariant("None", None, "Option", 1)
                return _TyveraVariant("Some", _TyveraSecret(value), "Option", 1)
            if expression.callee == "spawn":
                construction = expression.arguments[0]
                if not isinstance(construction, Call):
                    raise AssertionError("checked spawn expression is not an actor construction")
                declaration = self.actors[construction.callee]
                arguments = self._evaluate_arguments(construction.arguments, variables, depth)
                return self._spawn_actor(declaration, arguments, expression.location, depth + 1)
            if expression.callee in {"oneForOne", "oneForAll", "restForOne"}:
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                max_restarts, window_milliseconds = arguments[:2]
                strategy = {
                    "oneForOne": RestartStrategy.ONE_FOR_ONE,
                    "oneForAll": RestartStrategy.ONE_FOR_ALL,
                    "restForOne": RestartStrategy.REST_FOR_ONE,
                }[expression.callee]
                if max_restarts < 0:
                    raise Diagnostic(expression.location, "runtime", f"{expression.callee} maxRestarts must be zero or greater")
                if max_restarts > _MAX_SUPERVISOR_RESTARTS:
                    raise Diagnostic(expression.location, "runtime", f"{expression.callee} maxRestarts cannot exceed {_MAX_SUPERVISOR_RESTARTS}")
                if window_milliseconds <= 0:
                    raise Diagnostic(expression.location, "runtime", f"{expression.callee} windowMilliseconds must be greater than zero")
                backoff = arguments[2] if len(arguments) == 3 else None
                supervisor = _TyveraSupervisor(max_restarts, window_milliseconds, self._clock, strategy, backoff)
                self._supervisors.append(supervisor)
                self._emit_runtime_event(RuntimeEvent(
                    RuntimeEventKind.SUPERVISOR_STARTED,
                    f"Supervisor#{supervisor.identity}", strategy,
                ))
                return supervisor
            if expression.callee in {"permanentChildPolicy", "transientChildPolicy", "temporaryChildPolicy"}:
                return {
                    "permanentChildPolicy": RestartPolicy.PERMANENT,
                    "transientChildPolicy": RestartPolicy.TRANSIENT,
                    "temporaryChildPolicy": RestartPolicy.TEMPORARY,
                }[expression.callee]
            if expression.callee == "exponentialBackoff":
                base, maximum = self._evaluate_arguments(expression.arguments, variables, depth)
                if base <= 0 or maximum < base:
                    raise Diagnostic(expression.location, "runtime", "exponentialBackoff requires 0 < baseMilliseconds <= maximumMilliseconds")
                return ExponentialBackoff(base, maximum)
            if expression.callee == "runActors":
                return self._run_actors(expression.location, depth)
            if expression.callee == "runTasks":
                return self._run_tasks(expression.location, depth)
            arguments = self._evaluate_arguments(expression.arguments, variables, depth)
            if expression.callee == "milliseconds":
                return self._make_duration(arguments[0], expression.location)
            if expression.callee == "seconds":
                seconds = arguments[0]
                return self._make_duration(seconds * 1000, expression.location)
            if expression.callee == "timerAfter":
                duration = arguments[0]
                if not isinstance(duration, _TyveraDuration):
                    raise AssertionError("checked timerAfter argument is not Duration")
                return _TyveraTimer(self._clock(), duration.milliseconds, self._clock)
            if expression.callee == "bytesFromString":
                return _TyveraBytes(arguments[0].encode("utf-8"))
            if expression.callee == "byteFromInt":
                value = arguments[0]
                if not 0 <= value <= 255:
                    return _TyveraVariant("Err", "Byte value must be between 0 and 255", "Result", 1)
                return _TyveraVariant("Ok", _TyveraByte(value), "Result", 1)
            if expression.callee == "byteBuffer":
                length = arguments[0]
                if length < 0 or length > _MAX_BYTE_BUFFER_LENGTH:
                    return _TyveraVariant(
                        "Err", f"ByteBuffer length must be between 0 and {_MAX_BYTE_BUFFER_LENGTH} bytes",
                        "Result", 1,
                    )
                try:
                    buffer = _TyveraByteBuffer(bytearray(length))
                except (MemoryError, OverflowError):
                    return _TyveraVariant("Err", "cannot allocate ByteBuffer", "Result", 1)
                return _TyveraVariant("Ok", buffer, "Result", 1)
            if expression.callee == "httpMethod":
                try:
                    return _TyveraVariant("Ok", make_method(arguments[0]), "Result", 1)
                except ValueError as error:
                    return _TyveraVariant("Err", str(error), "Result", 1)
            if expression.callee == "httpUrl":
                try:
                    return _TyveraVariant("Ok", parse_url(arguments[0]), "Result", 1)
                except ValueError as error:
                    return _TyveraVariant("Err", str(error), "Result", 1)
            if expression.callee == "httpLimits":
                try:
                    return _TyveraVariant("Ok", HttpLimits(*arguments), "Result", 1)
                except ValueError as error:
                    return _TyveraVariant("Err", str(error), "Result", 1)
            if expression.callee == "httpDefaultLimits":
                return HttpLimits()
            if expression.callee == "httpHeaders":
                return HttpHeaders()
            if expression.callee == "httpRequest":
                method, target, headers, body = arguments
                try:
                    if not isinstance(method, HttpMethod) or not isinstance(headers, HttpHeaders) or not isinstance(body, _TyveraBytes):
                        raise ValueError("invalid HTTP request values")
                    value = HttpRequest(method, target, headers, body.value)
                    return _TyveraVariant("Ok", value, "Result", 1)
                except (ValueError, TypeError) as error:
                    return _TyveraVariant("Err", str(error), "Result", 1)
            if expression.callee == "httpResponse":
                status, headers, body = arguments
                if not 100 <= status <= 599:
                    return _TyveraVariant("Err", "invalid HTTP status code", "Result", 1)
                if not isinstance(headers, HttpHeaders) or not isinstance(body, _TyveraBytes):
                    return _TyveraVariant("Err", "invalid HTTP response values", "Result", 1)
                return _TyveraVariant("Ok", HttpResponse(status, headers, body.value), "Result", 1)
            if expression.callee == "httpTextResponse":
                status, body = arguments
                if not 100 <= status <= 599:
                    return _TyveraVariant("Err", "invalid HTTP status code", "Result", 1)
                return _TyveraVariant("Ok", text_response(status, body), "Result", 1)
            if expression.callee == "httpJsonResponse":
                status, value = arguments
                if not 100 <= status <= 599:
                    return _TyveraVariant("Err", "invalid HTTP status code", "Result", 1)
                try:
                    serialized = json.dumps(
                        self._json_to_python(value), ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False,
                    )
                except (TypeError, ValueError, RecursionError):
                    return _TyveraVariant("Err", "HTTP JSON encoding failed", "Result", 1)
                headers = HttpHeaders()
                headers.set("Content-Type", "application/json; charset=utf-8")
                return _TyveraVariant("Ok", HttpResponse(status, headers, serialized.encode("utf-8")), "Result", 1)
            if expression.callee == "httpRouter":
                return HttpRouter()
            if expression.callee == "httpRequestDecoder":
                return HttpMessageDecoder(arguments[0])
            if expression.callee == "httpResponseDecoder":
                return HttpMessageDecoder(arguments[0], response=True, request_method=arguments[1].name)
            if expression.callee == "httpEncodeRequest":
                request = arguments[0]
                try:
                    return _TyveraVariant("Ok", _TyveraBytes(encode_request(request.method, request.target, request.headers, request.body)), "Result", 1)
                except (HttpProtocolError, ValueError, UnicodeEncodeError) as error:
                    return _TyveraVariant("Err", str(error), "Result", 1)
            if expression.callee == "httpEncodeResponse":
                response, keep_alive = arguments
                try:
                    return _TyveraVariant("Ok", _TyveraBytes(encode_response(response, keep_alive=keep_alive)), "Result", 1)
                except (HttpProtocolError, ValueError, UnicodeEncodeError) as error:
                    return _TyveraVariant("Err", str(error), "Result", 1)
            if expression.callee == "httpServer":
                listener, router, limits, timeout = arguments
                if not isinstance(listener, _TyveraNetworkResource) or listener.resource_type != "TcpListener" or listener.closed:
                    return _TyveraVariant("Err", "httpServer requires an open TcpListener", "Result", 1)
                if not isinstance(router, HttpRouter) or not isinstance(limits, HttpLimits):
                    return _TyveraVariant("Err", "invalid HTTP server configuration", "Result", 1)
                if not isinstance(timeout, _TyveraDuration) or timeout.milliseconds <= 0:
                    return _TyveraVariant("Err", "HTTP operation timeout must be greater than zero", "Result", 1)
                try:
                    address = listener.stream.getsockname()
                    local_address = self._socket_address(address, listener.stream.family)
                except OSError as error:
                    return _TyveraVariant("Err", self._network_error(error), "Result", 1)
                return _TyveraVariant("Ok", _TyveraHttpServer(
                    listener, router, limits, timeout.milliseconds, local_address,
                ), "Result", 1)
            if expression.callee in {"httpSend", "httpGet", "httpPost", "httpPut", "httpDelete"}:
                if expression.callee == "httpSend":
                    url, request, timeout = arguments
                elif expression.callee in {"httpGet", "httpDelete"}:
                    url, timeout = arguments
                    method_name = "GET" if expression.callee == "httpGet" else "DELETE"
                    request = HttpRequest(HttpMethod(method_name), url.target, HttpHeaders(), b"")
                else:
                    url, headers, body, timeout = arguments
                    method_name = "POST" if expression.callee == "httpPost" else "PUT"
                    request = HttpRequest(method=HttpMethod(method_name), target=url.target,
                                          headers=headers, body=body.value)
                return self._http_client_task(url, request, timeout, expression.location)
            if expression.callee == "webSocketConnect":
                url, timeout = arguments
                return self._websocket_connect_task(url, timeout, expression.location)
            if expression.callee == "resolveHost":
                host, port = arguments
                if not 0 <= port <= 65_535:
                    task = self._create_io_task("Result<List<SocketAddress>,String>", expression.location)
                    self._complete_io_task(task, _TyveraVariant("Err", "port must be between 0 and 65535", "Result", 1))
                    return task
                return self._resolve_host_task(host, port, expression.location)
            if expression.callee in {"tcpConnect", "tlsConnect"}:
                host, port = arguments
                if not 1 <= port <= 65_535:
                    result_type = "TlsConnection" if expression.callee == "tlsConnect" else "TcpConnection"
                    task = self._create_io_task(f"Result<{result_type},String>", expression.location)
                    self._complete_io_task(task, _TyveraVariant("Err", "port must be between 1 and 65535", "Result", 1))
                    return task
                return self._tcp_connect_task(
                    host, port, expression.location, secure=expression.callee == "tlsConnect",
                )
            if expression.callee == "tcpListen":
                host, port, backlog = arguments
                if not 0 <= port <= 65_535:
                    task = self._create_io_task("Result<TcpListener,String>", expression.location)
                    self._complete_io_task(task, _TyveraVariant("Err", "port must be between 0 and 65535", "Result", 1))
                    return task
                if not 1 <= backlog <= 4096:
                    task = self._create_io_task("Result<TcpListener,String>", expression.location)
                    self._complete_io_task(task, _TyveraVariant("Err", "backlog must be between 1 and 4096", "Result", 1))
                    return task
                return self._tcp_listen_task(host, port, backlog, expression.location)
            if expression.callee == "openTextAsync":
                path, mode = arguments
                return self._async_open_text_task(self._project_path(path), mode, expression.location)
            if expression.callee in {"openText", "openTextReader", "openTextWriter"}:
                path = self._project_path(arguments[0])
                if expression.callee == "openTextReader":
                    mode = "read"
                    resource_type = "TextReader"
                elif expression.callee == "openTextWriter":
                    mode = arguments[1]
                    resource_type = "TextWriter"
                    if mode not in {"write", "append"}:
                        return _TyveraVariant("Err", "openTextWriter mode must be 'write' or 'append'", "Result", 1)
                else:
                    mode = arguments[1]
                    resource_type = "TextFile"
                    if mode not in {"read", "write", "append"}:
                        return _TyveraVariant("Err", "openText mode must be 'read', 'write', or 'append'", "Result", 1)
                if self._is_secret_storage_path(path):
                    return _TyveraVariant("Err", "secret file can only be accessed through the secrets provider", "Result", 1)
                python_mode = {"read": "r", "write": "w", "append": "a"}[mode]
                try:
                    stream = path.open(python_mode, encoding="utf-8", newline="")
                except OSError:
                    return _TyveraVariant("Err", "cannot open text file", "Result", 1)
                return _TyveraVariant(
                    "Ok", _TyveraTextFile(stream, mode, resource_type=resource_type,
                                          location=expression.location), "Result", 1,
                )
            if expression.callee == "sleepFor":
                duration = arguments[0]
                if not isinstance(duration, _TyveraDuration):
                    raise AssertionError("checked sleepFor argument is not Duration")
                return self._create_sleep_task(duration, expression.location)
            if expression.callee == "println":
                self.output.write(self._display(arguments[0]) + "\n")
                return None
            if expression.callee == "eprintln":
                self.error_output.write(self._display(arguments[0]) + "\n")
                return None
            if expression.callee == "readText":
                path = self._project_path(arguments[0])
                if self._is_secret_storage_path(path):
                    return _TyveraVariant("Err", "secret file can only be read through the secrets provider")
                try:
                    return _TyveraVariant("Ok", path.read_text(encoding="utf-8"))
                except UnicodeDecodeError:
                    return _TyveraVariant("Err", "text file is not valid UTF-8")
                except (OSError, ValueError):
                    return _TyveraVariant("Err", "cannot read text file")
            if expression.callee == "readBytes":
                path = self._project_path(arguments[0])
                if self._is_secret_storage_path(path):
                    return _TyveraVariant("Err", "secret file can only be read through the secrets provider")
                try:
                    with path.open("rb") as stream:
                        data = stream.read(_MAX_BYTE_BUFFER_LENGTH + 1)
                    if len(data) > _MAX_BYTE_BUFFER_LENGTH:
                        return _TyveraVariant(
                            "Err", f"binary file exceeds the {_MAX_BYTE_BUFFER_LENGTH}-byte limit",
                        )
                    return _TyveraVariant("Ok", _TyveraBytes(data), "Result", 1)
                except (MemoryError, OSError, OverflowError, ValueError):
                    return _TyveraVariant("Err", "cannot read binary file")
            if expression.callee == "writeText":
                path = self._project_path(arguments[0])
                if self._is_secret_storage_path(path):
                    return _TyveraVariant("Err", "secret file cannot be changed by the text-file API")
                try:
                    path.write_text(arguments[1], encoding="utf-8")
                    return _TyveraVariant("Ok", True)
                except (OSError, ValueError):
                    return _TyveraVariant("Err", "cannot write text file")
            if expression.callee == "writeBytes":
                path, payload = self._project_path(arguments[0]), arguments[1]
                if self._is_secret_storage_path(path):
                    return _TyveraVariant("Err", "secret file cannot be changed by the binary-file API")
                if len(payload.value) > _MAX_BYTE_BUFFER_LENGTH:
                    return _TyveraVariant(
                        "Err", f"binary data exceeds the {_MAX_BYTE_BUFFER_LENGTH}-byte limit",
                    )
                try:
                    path.write_bytes(payload.value)
                    return _TyveraVariant("Ok", True, "Result", 1)
                except (OSError, ValueError):
                    return _TyveraVariant("Err", "cannot write binary file")
            if expression.callee == "pathJoin":
                return (Path(arguments[0]) / arguments[1]).as_posix()
            if expression.callee == "pathExists":
                return self._project_path(arguments[0]).exists()
            if expression.callee == "parseJson":
                return self._parse_json(arguments[0])
            if expression.callee == "jsonStringify":
                try:
                    serialized = json.dumps(
                        self._json_to_python(arguments[0]), ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False,
                    )
                    return _TyveraVariant("Ok", serialized)
                except (TypeError, ValueError, RecursionError):
                    return _TyveraVariant("Err", "JSON encoding failed")
            if expression.callee == "jsonString":
                return _TyveraJson("string", arguments[0])
            if expression.callee == "jsonInt":
                return _TyveraJson("int", arguments[0])
            if expression.callee == "jsonFloat":
                return _TyveraJson("float", arguments[0])
            if expression.callee == "jsonBool":
                return _TyveraJson("bool", arguments[0])
            if expression.callee == "jsonNull":
                return _TyveraJson("null", None)
            if expression.callee == "jsonArray":
                return _TyveraJson("array", list(arguments[0].values))
            if expression.callee == "jsonObject":
                return _TyveraJson("object", {key: value for _, key, value in arguments[0].entries})
            if expression.callee == "List":
                return _TyveraList([])
            if expression.callee == "Map":
                return _TyveraMap()
            if expression.callee == "Some":
                return _TyveraVariant("Some", arguments[0])
            if expression.callee == "None":
                return _TyveraVariant("None")
            if expression.callee == "Ok":
                return _TyveraVariant("Ok", arguments[0])
            if expression.callee == "Err":
                return _TyveraVariant("Err", arguments[0])
            if expression.callee in self.newtypes:
                return _TyveraNewtype(expression.callee, arguments[0])
            if expression.callee in self.classes:
                return self._construct(self.classes[expression.callee], arguments, expression.location, depth + 1)
            if expression.callee in self.records:
                declaration = self.records[expression.callee]
                return _TyveraRecord(declaration.name, {
                    field.name: value for field, value in zip(declaration.fields, arguments)
                })
            function = self.functions[expression.callee]
            if function.is_async:
                return self._create_async_task(function, arguments, expression.location, depth)
            return self._call(function, arguments, expression.location, depth + 1)
        if isinstance(expression, MemberAccess):
            if isinstance(expression.receiver, Name) and expression.receiver.identifier == "secrets":
                try:
                    value = self.secret_provider.get(expression.member)
                except SecretProviderError as error:
                    raise Diagnostic(expression.location, "runtime", str(error)) from error
                return _TyveraVariant("None") if value is None else _TyveraVariant("Some", _TyveraSecret(value))
            if isinstance(expression.receiver, Name) and expression.receiver.identifier in self.enums:
                return _TyveraVariant(expression.member, None, expression.receiver.identifier, 0)
            receiver = self._evaluate(expression.receiver, variables, depth)
            if isinstance(receiver, _TyveraTuple) and expression.member.isdecimal():
                return receiver.values[int(expression.member)]
            if expression.member == "length" and isinstance(receiver, _TyveraList):
                return len(receiver.values)
            if expression.member == "length" and isinstance(receiver, _TyveraMap):
                return len(receiver)
            if isinstance(receiver, _TyveraRecord):
                return receiver.fields[expression.member]
            if isinstance(receiver, _TyveraNewtype) and expression.member == "value":
                return receiver.value
            if not isinstance(receiver, _TyveraInstance):
                raise AssertionError("checked field access receiver is not an instance")
            return receiver.fields[expression.member]
        if isinstance(expression, MemberCall):
            if isinstance(expression.receiver, Name) and expression.receiver.identifier in self.enums:
                payloads = self._evaluate_arguments(expression.arguments, variables, depth)
                if not payloads:
                    payload = None
                elif len(payloads) == 1:
                    payload = payloads[0]
                else:
                    payload = _TyveraTuple(tuple(payloads))
                return _TyveraVariant(expression.method, payload, expression.receiver.identifier, len(payloads))
            receiver = self._evaluate(expression.receiver, variables, depth)
            if isinstance(receiver, _TyveraHttpServer):
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                if expression.method == "start":
                    if receiver.run_task is not None:
                        task = self._create_io_task("Result<Bool,String>", expression.location)
                        self._complete_io_task(task, _TyveraVariant(
                            "Err", "HTTP server has already been started", "Result", 1,
                        ))
                        return task
                    if receiver.closed:
                        task = self._create_io_task("Result<Bool,String>", expression.location)
                        self._complete_io_task(task, _TyveraVariant(
                            "Err", "HTTP server is closed", "Result", 1,
                        ))
                        return task
                    return self._start_http_server_task(receiver, expression.location)
                if expression.method == "shutdown":
                    was_running = receiver.run_task is not None and self._task_is_active(receiver.run_task)
                    receiver.stopped = True
                    if not receiver.listener.closed:
                        self._close_resource(receiver.listener)
                    return was_running
                if expression.method == "localAddress":
                    return _TyveraVariant("Ok", receiver.local_address, "Result", 1)
                if expression.method == "isOpen":
                    return not receiver.closed
                if expression.method == "isRunning":
                    return receiver.run_task is not None and self._task_is_active(receiver.run_task)
                if expression.method == "close":
                    was_open = not receiver.closed
                    self._close_resource(receiver)
                    return was_open
                raise AssertionError(f"unhandled checked HttpServer method: {expression.method}")
            if isinstance(receiver, _TyveraTextFile):
                if expression.method == "isOpen":
                    return not receiver.closed
                if expression.method == "close":
                    was_open = not receiver.closed
                    return was_open and self._close_resource(receiver) is None
                if expression.method in {"readAllAsync", "writeAsync", "appendAsync", "flushAsync", "closeAsync"}:
                    arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                    return self._async_text_file_task(receiver, expression.method, arguments, expression.location)
                if receiver.closed:
                    raise Diagnostic(expression.location, "runtime", f"{receiver.resource_type} is closed")
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                if expression.method == "readAll":
                    if receiver.mode != "read":
                        return _TyveraVariant("Err", "text file is not open for reading")
                    try:
                        return _TyveraVariant("Ok", receiver.stream.read())
                    except (OSError, UnicodeDecodeError):
                        return _TyveraVariant("Err", "cannot read text file")
                if expression.method == "flush":
                    try:
                        receiver.stream.flush()
                        return _TyveraVariant("Ok", True)
                    except OSError:
                        return _TyveraVariant("Err", "cannot flush text file")
                if expression.method == "append":
                    if receiver.mode != "append":
                        return _TyveraVariant("Err", "text file is not open for appending")
                    try:
                        receiver.stream.write(arguments[0])
                        return _TyveraVariant("Ok", True)
                    except OSError:
                        return _TyveraVariant("Err", "cannot append to text file")
                if expression.method == "write":
                    if receiver.mode == "read":
                        return _TyveraVariant("Err", "text file is not open for writing")
                    try:
                        receiver.stream.write(arguments[0])
                        return _TyveraVariant("Ok", True)
                    except OSError:
                        return _TyveraVariant("Err", "cannot write text file")
                raise AssertionError(f"unhandled checked TextFile method: {expression.method}")
            if isinstance(receiver, _TyveraNetworkResource):
                if expression.method == "isOpen":
                    return not receiver.closed
                if expression.method == "close":
                    was_open = not receiver.closed
                    return was_open and self._close_resource(receiver) is None
                if receiver.resource_type == "WebSocket" and expression.method == "closeGracefully":
                    return self._websocket_close_task(receiver, expression.location)
                if receiver.closed:
                    raise Diagnostic(expression.location, "runtime", f"{receiver.resource_type} is closed")
                if expression.method in {"localAddress", "remoteAddress"}:
                    try:
                        raw = (receiver.stream.getsockname() if expression.method == "localAddress"
                               else receiver.stream.getpeername())
                        family = receiver.stream.family
                        return _TyveraVariant("Ok", self._socket_address(raw, family), "Result", 1)
                    except OSError as error:
                        return _TyveraVariant("Err", self._network_error(error), "Result", 1)
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                if receiver.resource_type == "WebSocket":
                    if expression.method == "sendText":
                        try:
                            payload = arguments[0].encode("utf-8", "strict")
                            return self._websocket_send_task(receiver, payload, 1, expression.location)
                        except UnicodeEncodeError:
                            task = self._create_io_task("Result<Int,String>", expression.location)
                            self._complete_io_task(task, _TyveraVariant(
                                "Err", "WebSocket text is not valid UTF-8", "Result", 1,
                            ))
                            return task
                    if expression.method == "send":
                        payload = arguments[0]
                        if not isinstance(payload, _TyveraBytes):
                            raise AssertionError("checked WebSocket.send argument is not Bytes")
                        return self._websocket_send_task(receiver, payload.value, 2, expression.location)
                    if expression.method == "receive":
                        return self._websocket_receive_task(receiver, expression.location)
                    if expression.method == "receiveWithin":
                        timeout = arguments[0]
                        if not isinstance(timeout, _TyveraDuration):
                            raise AssertionError("checked WebSocket.receiveWithin argument is not Duration")
                        return self._websocket_receive_task(
                            receiver, expression.location,
                            timeout_milliseconds=timeout.milliseconds,
                        )
                if receiver.resource_type == "TcpListener" and expression.method == "accept":
                    return self._tcp_accept_task(receiver, expression.location)
                if receiver.resource_type in {"TcpConnection", "TlsConnection"} and expression.method == "read":
                    return self._tcp_read_task(receiver, arguments[0], expression.location)
                if receiver.resource_type in {"TcpConnection", "TlsConnection"} and expression.method in {"write", "writeAll"}:
                    payload = arguments[0]
                    if not isinstance(payload, _TyveraBytes):
                        raise AssertionError("checked TCP write argument is not Bytes")
                    return self._tcp_write_task(receiver, payload.value,
                                                expression.method == "writeAll", expression.location)
                raise AssertionError(f"unhandled checked {receiver.resource_type} method: {expression.method}")
            if isinstance(receiver, _TyveraBytes):
                if expression.method == "length":
                    return len(receiver.value)
                if expression.method == "toString":
                    try:
                        return _TyveraVariant("Ok", receiver.value.decode("utf-8"), "Result", 1)
                    except UnicodeDecodeError:
                        return _TyveraVariant("Err", "bytes are not valid UTF-8", "Result", 1)
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                if expression.method == "get":
                    index = arguments[0]
                    if index < 0 or index >= len(receiver.value):
                        return _TyveraVariant("Err", "Bytes index is out of bounds", "Result", 1)
                    return _TyveraVariant("Ok", _TyveraByte(receiver.value[index]), "Result", 1)
                if expression.method == "slice":
                    start, length = arguments
                    if start < 0 or length < 0 or start > len(receiver.value) or length > len(receiver.value) - start:
                        return _TyveraVariant("Err", "Bytes slice is out of bounds", "Result", 1)
                    try:
                        value = _TyveraBytes(receiver.value[start:start + length])
                    except MemoryError:
                        return _TyveraVariant("Err", "cannot allocate Bytes slice", "Result", 1)
                    return _TyveraVariant("Ok", value, "Result", 1)
            if isinstance(receiver, _TyveraByte):
                if expression.method == "toInt":
                    return receiver.value
            if isinstance(receiver, _TyveraByteBuffer):
                if expression.method == "length":
                    return len(receiver.value)
                if expression.method == "toBytes":
                    try:
                        value = _TyveraBytes(bytes(receiver.value))
                    except MemoryError:
                        return _TyveraVariant("Err", "cannot allocate Bytes snapshot", "Result", 1)
                    return _TyveraVariant("Ok", value, "Result", 1)
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                if expression.method == "get":
                    index = arguments[0]
                    if index < 0 or index >= len(receiver.value):
                        return _TyveraVariant("Err", "ByteBuffer index is out of bounds", "Result", 1)
                    return _TyveraVariant("Ok", _TyveraByte(receiver.value[index]), "Result", 1)
                if expression.method == "set":
                    index, byte_value = arguments
                    if index < 0 or index >= len(receiver.value):
                        return _TyveraVariant("Err", "ByteBuffer index is out of bounds", "Result", 1)
                    if not isinstance(byte_value, _TyveraByte):
                        raise AssertionError("checked ByteBuffer.set value is not Byte")
                    receiver.value[index] = byte_value.value
                    return _TyveraVariant("Ok", True, "Result", 1)
            if isinstance(receiver, _TyveraSocketAddress):
                if expression.method == "host":
                    return receiver.host
                if expression.method == "port":
                    return receiver.port
                if expression.method == "family":
                    return receiver.family
                if expression.method == "toString":
                    host = f"[{receiver.host}]" if receiver.family == "IPv6" else receiver.host
                    return f"{host}:{receiver.port}"
            if isinstance(receiver, _TyveraWebSocketMessage):
                if expression.method == "kind":
                    return "text" if receiver.opcode == 1 else "binary"
                if expression.method == "bytes":
                    return _TyveraBytes(receiver.payload)
                if expression.method == "text":
                    if receiver.opcode != 1:
                        return _TyveraVariant("Err", "WebSocket message is binary", "Result", 1)
                    try:
                        return _TyveraVariant("Ok", receiver.payload.decode("utf-8", "strict"), "Result", 1)
                    except UnicodeDecodeError:
                        return _TyveraVariant("Err", "WebSocket text is not valid UTF-8", "Result", 1)
            if isinstance(receiver, (HttpMethod, HttpUrl, HttpLimits, HttpHeaders,
                                     HttpRequest, HttpResponse, HttpMessageDecoder, HttpRouter)):
                arguments = self._evaluate_arguments(expression.arguments, variables, depth)
                if isinstance(receiver, HttpMethod):
                    return receiver.name if expression.method == "name" else receiver.is_standard
                if isinstance(receiver, HttpUrl):
                    if expression.method == "scheme":
                        return receiver.scheme
                    if expression.method == "host":
                        return receiver.host
                    if expression.method == "port":
                        return receiver.port
                    if expression.method == "target":
                        return receiver.target
                    if expression.method == "authority":
                        host = f"[{receiver.host}]" if ":" in receiver.host else receiver.host
                        default_port = 80 if receiver.scheme == "http" else 443
                        return host if receiver.port == default_port else f"{host}:{receiver.port}"
                if isinstance(receiver, HttpLimits):
                    return {
                        "requestLineBytes": receiver.request_line_bytes,
                        "headerBytes": receiver.header_bytes,
                        "headerCount": receiver.header_count,
                        "bodyBytes": receiver.body_bytes,
                        "maxConnections": receiver.max_connections,
                    }[expression.method]
                if isinstance(receiver, HttpHeaders):
                    name = arguments[0] if arguments else ""
                    if expression.method in {"add", "set"}:
                        try:
                            getattr(receiver, expression.method)(arguments[0], arguments[1])
                            return _TyveraVariant("Ok", True, "Result", 1)
                        except ValueError as error:
                            return _TyveraVariant("Err", str(error), "Result", 1)
                    if expression.method == "remove":
                        try:
                            return receiver.remove(name)
                        except ValueError:
                            return 0
                    if expression.method == "get":
                        try:
                            value = receiver.get(name)
                        except ValueError:
                            value = None
                        return _TyveraVariant("None") if value is None else _TyveraVariant("Some", value)
                    if expression.method == "values":
                        try:
                            return _TyveraList(receiver.values(name))
                        except ValueError:
                            return _TyveraList([])
                    if expression.method == "contains":
                        try:
                            return receiver.contains(name)
                        except ValueError:
                            return False
                    if expression.method == "length":
                        return len(receiver.entries)
                if isinstance(receiver, HttpRequest):
                    if expression.method == "method":
                        return receiver.method
                    if expression.method == "target":
                        return receiver.target
                    if expression.method == "path":
                        return receiver._path
                    if expression.method == "version":
                        return receiver.version
                    if expression.method == "headers":
                        return receiver.headers
                    if expression.method == "body":
                        return _TyveraBytes(receiver.body)
                    if expression.method == "header":
                        value = receiver.headers.get(arguments[0])
                        return _TyveraVariant("None") if value is None else _TyveraVariant("Some", value)
                    if expression.method == "query":
                        values = receiver.query_values(arguments[0])
                        return _TyveraVariant("None") if not values else _TyveraVariant("Some", values[0])
                    if expression.method == "queryValues":
                        return _TyveraList(receiver.query_values(arguments[0]))
                    if expression.method == "routeParameter":
                        value = receiver.route_parameters.get(arguments[0])
                        return _TyveraVariant("None") if value is None else _TyveraVariant("Some", value)
                    if expression.method == "remoteAddress":
                        value = receiver._remote_address
                        return _TyveraVariant("None") if value is None else _TyveraVariant("Some", value)
                    if expression.method == "isKeepAlive":
                        return receiver.is_keep_alive()
                    if expression.method == "json":
                        try:
                            text = receiver.body.decode("utf-8")
                        except UnicodeDecodeError:
                            return _TyveraVariant("Err", "HTTP JSON body is not valid UTF-8", "Result", 1)
                        return self._parse_json(text)
                if isinstance(receiver, HttpResponse):
                    if expression.method == "status":
                        return receiver.status
                    if expression.method == "headers":
                        return receiver.headers
                    if expression.method == "body":
                        return _TyveraBytes(receiver.body)
                if isinstance(receiver, HttpMessageDecoder):
                    if expression.method == "feed":
                        try:
                            payload = arguments[0]
                            if not isinstance(payload, _TyveraBytes):
                                raise HttpProtocolError("feed expects Bytes")
                            receiver.feed(payload.value)
                            return _TyveraVariant("Ok", True, "Result", 1)
                        except (HttpProtocolError, ValueError) as error:
                            return _TyveraVariant("Err", str(error), "Result", 1)
                    if expression.method == "finish":
                        receiver.finish()
                        return _TyveraVariant("Ok", True, "Result", 1)
                    if expression.method == "bufferedBytes":
                        return len(receiver.buffer)
                    if expression.method in {"nextRequest", "nextResponse"}:
                        if receiver.response != (expression.method == "nextResponse"):
                            return _TyveraVariant("Err", "HTTP decoder used for the wrong message direction", "Result", 1)
                        try:
                            message = receiver.next_message()
                            option = (_TyveraVariant("None") if message is None
                                      else _TyveraVariant("Some", message))
                            return _TyveraVariant("Ok", option, "Result", 1)
                        except (HttpProtocolError, ValueError) as error:
                            return _TyveraVariant("Err", str(error), "Result", 1)
                if isinstance(receiver, HttpRouter):
                    if expression.method == "add":
                        try:
                            return _TyveraVariant("Ok", receiver.add(*arguments), "Result", 1)
                        except ValueError as error:
                            return _TyveraVariant("Err", str(error), "Result", 1)
                    if expression.method == "dispatch":
                        return receiver.dispatch(arguments[0])
            if isinstance(receiver, _TyveraSupervisor):
                if expression.method == "start":
                    if receiver.status != "alive":
                        raise Diagnostic(expression.location, "runtime", f"cannot start a child on a {receiver.status} supervisor")
                    child_expression = expression.arguments[0]
                    policy = (RestartPolicy.PERMANENT if len(expression.arguments) == 1
                              else self._evaluate(expression.arguments[1], variables, depth))
                    if not isinstance(policy, RestartPolicy):
                        raise AssertionError("checked supervisor child policy has an invalid runtime value")
                    if isinstance(child_expression, Call) and child_expression.callee in self.actors:
                        declaration = self.actors[child_expression.callee]
                        arguments = self._evaluate_arguments(child_expression.arguments, variables, depth)
                        return self._spawn_actor(
                            declaration, arguments, expression.location, depth + 1, receiver, policy,
                        )
                    nested = self._evaluate(child_expression, variables, depth)
                    if not isinstance(nested, _TyveraSupervisor):
                        raise AssertionError("checked supervisor child is not an actor or supervisor")
                    if nested is receiver or nested.parent is not None:
                        raise Diagnostic(expression.location, "runtime", "a Supervisor child must be a distinct, unowned supervisor")
                    nested.parent = receiver
                    nested.restart_policy = policy
                    receiver.children.append(nested)
                    self._emit_runtime_event(RuntimeEvent(
                        RuntimeEventKind.SUPERVISOR_STARTED, f"Supervisor#{nested.identity}", nested.strategy,
                    ))
                    return nested
                if expression.method == "isAlive":
                    return receiver.status == "alive"
                if expression.method == "stop":
                    self._stop_supervised_supervisor(receiver, expression.location, depth + 1)
                    return None
            if isinstance(receiver, _TyveraActorRef) and expression.method in {"request", "requestWithin", "sendAfter"}:
                actor = receiver.actor
                scheduled = expression.method == "sendAfter"
                timed = expression.method in {"requestWithin", "sendAfter"}
                prefix = 1 if timed else 0
                duration = self._evaluate(expression.arguments[0], variables, depth) if timed else None
                selector = expression.arguments[prefix]
                if not isinstance(selector, Name):
                    raise AssertionError("checked actor request selector is not a name")
                request_arguments = self._evaluate_arguments(
                    expression.arguments[prefix + 1:], variables, depth,
                )
                if scheduled:
                    if not isinstance(duration, _TyveraDuration):
                        raise AssertionError("checked sendAfter duration is not Duration")
                    if actor.status != "alive":
                        raise Diagnostic(expression.location, "runtime", f"cannot schedule a message for a {actor.status} actor")
                    timer = _TyveraTimer(
                        self._clock(), duration.milliseconds, self._clock,
                        scheduled_actor=actor,
                        scheduled_method=selector.identifier,
                        scheduled_arguments=request_arguments,
                        scheduled_location=expression.location,
                    )
                    self._scheduled_timers.append(timer)
                    return timer
                cell = _TyveraReplyCell()
                if duration is not None:
                    if not isinstance(duration, _TyveraDuration):
                        raise AssertionError("checked requestWithin timeout is not Duration")
                    cell.started_at = self._clock()
                    cell.timeout_milliseconds = duration.milliseconds
                    cell.clock = self._clock
                receiver_value = _TyveraReplyReceiver(cell)
                self._enqueue_actor_message(
                    actor, selector.identifier, [*request_arguments, _TyveraReplySender(cell)], expression.location,
                )
                return receiver_value
            arguments = self._evaluate_arguments(expression.arguments, variables, depth)
            if isinstance(receiver, _TyveraTaskGroup):
                if self._current_actor is not None or receiver.owner is not self._current_task:
                    raise Diagnostic(expression.location, "runtime", "TaskGroup may only be used by its creating task")
                if expression.method == "add":
                    task = arguments[0]
                    if not isinstance(task, _TyveraTask) or task not in self._tasks or task.parent is not receiver.owner:
                        raise Diagnostic(expression.location, "runtime", "TaskGroup.add accepts only a task created by the group's owner")
                    if task in receiver.tasks:
                        return False
                    receiver.tasks.append(task)
                    return True
                if expression.method == "isDone":
                    return all(task.state in {"completed", "failed", "cancelled"} for task in receiver.tasks)
                if expression.method == "cancel":
                    active = [task for task in receiver.tasks
                              if task.state not in {"completed", "failed", "cancelled"}]
                    for task in active:
                        self._cancel_task(task)
                    self._wake_waiting_tasks()
                    return len(active)
            if isinstance(receiver, _TyveraActorRef):
                actor = receiver.actor
                if expression.method == "isAlive":
                    return actor.status == "alive"
                if expression.method == "stop":
                    self._stop_supervised_actor(actor, expression.location, depth + 1)
                    return None
                self._enqueue_actor_message(actor, expression.method, arguments, expression.location)
                return None
            if isinstance(receiver, _TyveraReplySender) and expression.method == "send":
                return self._send_reply(receiver.cell, arguments[0])
            if isinstance(receiver, _TyveraReplyReceiver):
                self._refresh_reply(receiver.cell)
                cell = receiver.cell
                if expression.method == "isReady":
                    return cell.state != "pending"
                if expression.method == "isCancelled":
                    return cell.state == "cancelled"
                if expression.method == "isTimedOut":
                    return cell.state == "timed_out"
                if expression.method == "cancel":
                    if cell.state != "pending":
                        return False
                    cell.state = "cancelled"
                    return True
                if expression.method == "tryReceive":
                    if cell.state == "sent":
                        value = cell.value
                        cell.value = None
                        cell.state = "consumed"
                        return _TyveraVariant("Ok", _TyveraVariant("Some", value))
                    if cell.state in {"failed", "timed_out"}:
                        return _TyveraVariant("Err", cell.error or "request failed")
                    return _TyveraVariant("Ok", _TyveraVariant("None", None, "Option", 1), "Result", 1)
            if isinstance(receiver, _TyveraTask):
                carries_resource = self._type_contains_resource(receiver.result_type)
                if carries_resource and expression.method in {"withTimeout", "tryResult"}:
                    self._consume_task_binding(expression.receiver, variables)
                if expression.method == "isDone":
                    return receiver.state in {"completed", "failed", "cancelled"}
                if expression.method == "isCancelled":
                    return receiver.state == "cancelled"
                if expression.method == "cancel":
                    if receiver.state in {"completed", "failed", "cancelled"}:
                        return False
                    self._cancel_task(receiver)
                    self._wake_waiting_tasks()
                    return True
                if expression.method == "detach":
                    parent = receiver.parent
                    if (parent is None or parent is not self._current_task
                            or receiver.state in {"completed", "failed", "cancelled"}):
                        return False
                    parent.children.remove(receiver)
                    receiver.parent = None
                    for group in self._task_groups:
                        if group.owner is parent and receiver in group.tasks:
                            group.tasks.remove(receiver)
                    return True
                if expression.method == "withTimeout":
                    duration = arguments[0]
                    if not isinstance(duration, _TyveraDuration):
                        raise AssertionError("checked Task.withTimeout argument is not Duration")
                    return self._create_timeout_task(receiver, duration, expression.location)
                if expression.method == "tryResult":
                    if receiver.state == "failed":
                        return _TyveraVariant("Err", receiver.error or "task failed")
                    if receiver.state == "cancelled":
                        return _TyveraVariant("Err", receiver.error or "task was cancelled")
                    if receiver.result_type == "Void":
                        return _TyveraVariant("Ok", receiver.state == "completed")
                    if receiver.state == "completed":
                        value = receiver.result
                        if self._resource_handles(value):
                            receiver.result = None
                        return _TyveraVariant("Ok", _TyveraVariant("Some", value, "Option", 1), "Result", 1)
                    if carries_resource:
                        self._cancel_task(receiver)
                    return _TyveraVariant("Ok", _TyveraVariant("None"))
            if isinstance(receiver, _TyveraDuration) and expression.method == "inMilliseconds":
                return receiver.milliseconds
            if isinstance(receiver, _TyveraTimer):
                if expression.method == "isCancelled":
                    return receiver.cancelled
                if receiver.cancelled:
                    if expression.method == "isReady":
                        return False
                    if expression.method == "remaining":
                        return _TyveraDuration(0)
                    if expression.method == "cancel":
                        return False
                elapsed = (receiver.clock() - receiver.started_at) * 1000.0
                ready = elapsed >= receiver.duration_milliseconds
                if expression.method == "isReady":
                    return ready
                if expression.method == "cancel":
                    if ready:
                        return False
                    receiver.cancelled = True
                    self._prune_scheduled_timers()
                    return True
                if expression.method == "remaining":
                    if ready:
                        return _TyveraDuration(0)
                    remaining = math.ceil(receiver.duration_milliseconds - elapsed)
                    return _TyveraDuration(max(0, min(remaining, INT_MAX)))
            if isinstance(receiver, _TyveraSecret) and expression.method == "reveal":
                return receiver.value
            if isinstance(receiver, _TyveraJson):
                return self._call_json_method(receiver, expression.method, arguments)
            if isinstance(receiver, _TyveraList):
                return self._call_list_method(receiver, expression.method, arguments, expression.location)
            if isinstance(receiver, _TyveraMap):
                return self._call_map_method(receiver, expression.method, arguments, expression.location)
            if not isinstance(receiver, _TyveraInstance):
                raise AssertionError("checked method receiver is not an instance")
            if receiver.actor is not None:
                self._enqueue_actor_message(receiver.actor, expression.method, arguments, expression.location)
                return None
            declaration = self.classes[receiver.class_name]
            method = next(method for method in declaration.methods if method.name == expression.method)
            return self._call_method(method, receiver, arguments, expression.location, depth + 1)
        if isinstance(expression, Unary):
            if expression.operator == "move":
                if not isinstance(expression.operand, Name):
                    raise AssertionError("checked move operand is not a binding name")
                name = expression.operand.identifier
                value = variables[name]
                if value is _MOVED:
                    raise Diagnostic(expression.location, "runtime", f"resource '{name}' was already moved")
                for scope in variables.maps:
                    if name in scope:
                        scope[name] = _MOVED
                        break
                else:
                    raise AssertionError("checked move binding disappeared")
                return value
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
                message = "remainder by zero" if expression.operator == "%" else "division by zero"
                raise Diagnostic(expression.location, "runtime", message) from error
            self._check_numeric_result(result, expression.location)
            return result
        raise AssertionError(f"unhandled expression node: {type(expression).__name__}")

    @staticmethod
    def _close_text_file(resource: _TyveraTextFile) -> str | None:
        if resource.closed:
            return None
        resource.closed = True
        try:
            resource.stream.close()
        except OSError:
            return "cannot close text file"
        return None

    def _close_resource(self, resource: object) -> str | None:
        if isinstance(resource, _TyveraHttpServer):
            resource.stopped = True
            if not resource.listener.closed:
                self._close_resource(resource.listener)
            resource.closed = True
            if resource.run_task is not None and self._task_is_active(resource.run_task):
                self._cancel_task(resource.run_task)
            return None
        if isinstance(resource, _TyveraTextFile):
            if resource.closed:
                return None
            if resource.pending_operations:
                resource.closed = True
                resource.close_requested = True
                return None
            return self._close_text_file(resource)
        if isinstance(resource, _TyveraNetworkResource):
            if resource.closed:
                return None
            resource.closed = True
            for pending in (resource.pending_read_task, resource.pending_write_task):
                if pending is not None and self._task_is_active(pending):
                    if pending.io_cancel is not None:
                        cancel, pending.io_cancel = pending.io_cancel, None
                        cancel()
                    self._complete_io_task(pending, _TyveraVariant("Err", "resource closed", "Result", 1))
            try:
                self._get_io_backend().unwatch(resource.stream, selectors.EVENT_READ)
                self._get_io_backend().unwatch(resource.stream, selectors.EVENT_WRITE)
                if isinstance(resource.stream, ssl.SSLSocket):
                    try:
                        # Send TLS close_notify without waiting synchronously for
                        # the peer's reciprocal shutdown on a non-blocking stream.
                        resource.stream.unwrap()
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        pass
                    except (ssl.SSLError, OSError):
                        # The transport must still close if the peer already
                        # disconnected or the alert could not be drained.
                        pass
                resource.stream.close()
            except OSError:
                return f"cannot close {resource.resource_type}"
            return None
        if isinstance(resource, _TyveraTask):
            self._cancel_task(resource)
            self._cleanup_task_result(resource)
            return None
        return "value is not an owned Resource"

    def _cleanup_task_result(self, task: _TyveraTask) -> None:
        handles = self._resource_handles(task.result)
        for resource in handles:
            if isinstance(resource, _TyveraTask):
                if self._task_is_active(resource):
                    self._cancel_task(resource)
                self._cleanup_task_result(resource)
            else:
                self._close_resource(resource)
        if handles:
            task.result = None

    def _resource_handles(self, value: object) -> list[object]:
        if isinstance(value, (_TyveraTextFile, _TyveraNetworkResource, _TyveraHttpServer)):
            return [value]
        if isinstance(value, _TyveraTask) and self._type_contains_resource(value.result_type):
            return [value]
        if isinstance(value, _TyveraVariant):
            return self._resource_handles(value.payload)
        if isinstance(value, _TyveraTuple):
            return [resource for item in reversed(value.values) for resource in self._resource_handles(item)]
        if isinstance(value, _TyveraRecord):
            return [resource for item in reversed(tuple(value.fields.values())) for resource in self._resource_handles(item)]
        if isinstance(value, _TyveraNewtype):
            return self._resource_handles(value.value)
        if isinstance(value, _TyveraList):
            return [resource for item in reversed(value.values) for resource in self._resource_handles(item)]
        if isinstance(value, _TyveraMap):
            return [resource for _, key, item in reversed(value.entries)
                    for candidate in (item, key)
                    for resource in self._resource_handles(candidate)]
        if isinstance(value, _TyveraInstance):
            return [resource for item in reversed(tuple(value.fields.values())) for resource in self._resource_handles(item)]
        return []

    @staticmethod
    def _type_contains_resource(type_name: str) -> bool:
        base, arguments = split_type(canonical_type(type_name))
        resources = {"TextFile", "TextReader", "TextWriter", "TcpConnection", "TcpListener", "TlsConnection", "WebSocket", "HttpServer"}
        return base in resources or any(Interpreter._type_contains_resource(argument) for argument in arguments)

    @staticmethod
    def _consume_task_binding(expression: Expression, variables: ChainMap[str, object]) -> None:
        if not isinstance(expression, Name):
            return
        for scope in variables.maps:
            if expression.identifier in scope:
                scope[expression.identifier] = _MOVED
                return

    def _cleanup_owned_scope(self, scope: dict[str, object]) -> list[tuple[Location, str]]:
        errors: list[tuple[Location, str]] = []
        resources: dict[int, object] = {}
        for value in scope.values():
            if value is _MOVED:
                continue
            for resource in self._resource_handles(value):
                resources.setdefault(id(resource), resource)
        for resource in sorted(resources.values(), key=lambda item: item.identity, reverse=True):
            if isinstance(resource, (_TyveraTextFile, _TyveraNetworkResource, _TyveraHttpServer)) and (resource.closed or getattr(resource, "scoped", False)):
                continue
            cleanup_error = self._close_resource(resource)
            if cleanup_error is not None:
                location = getattr(resource, "location", None) or Location("<resource>", 1, 1)
                errors.append((location, cleanup_error))
        return errors

    @staticmethod
    def _resource_cleanup_diagnostic(location: Location, message: str) -> str:
        return f"{location.filename}:{location.line}:{location.column}: runtime error: {message}"

    def _cleanup_resource_frames(
        self, frames: list[_AsyncBlockFrame | _AsyncLoopFrame | _AsyncResourceFrame],
    ) -> list[str]:
        errors: list[str] = []
        for frame in reversed(frames):
            if isinstance(frame, _AsyncBlockFrame):
                errors.extend(
                    self._resource_cleanup_diagnostic(location, message)
                    for location, message in self._cleanup_owned_scope(frame.variables.maps[0])
                )
            elif isinstance(frame, _AsyncResourceFrame):
                cleanup_error = self._close_resource(frame.resource)
                if cleanup_error is not None:
                    errors.append(self._resource_cleanup_diagnostic(frame.location, cleanup_error))
        return errors

    def _release_task_frames(self, task: _TyveraTask) -> list[str]:
        frames = task.frames
        task.frames = []
        errors = self._cleanup_resource_frames(frames)
        resources, task.native_resources = task.native_resources, []
        for resource in reversed(resources):
            cleanup_error = self._close_resource(resource)
            if cleanup_error is not None:
                errors.append(self._resource_cleanup_diagnostic(
                    getattr(resource, "location", None) or task.location, cleanup_error,
                ))
        return errors

    def _project_path(self, value: object) -> Path:
        if not isinstance(value, str):
            raise AssertionError("checked path argument is not String")
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    def _is_secret_storage_path(self, path: Path) -> bool:
        try:
            return path.resolve() == (self.project_root / ".tyvera.secrets").resolve()
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _parse_json(text: str) -> _TyveraVariant:
        def parse_integer(raw: str) -> int:
            value = int(raw)
            if not INT_MIN <= value <= INT_MAX:
                raise ValueError("number outside Tyvera Int range")
            return value

        def parse_float(raw: str) -> float:
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("non-finite number")
            return value

        def reject_constant(_raw: str):
            raise ValueError("non-standard JSON constant")

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate object key")
                result[key] = value
            return result

        try:
            value = json.loads(
                text, parse_int=parse_integer, parse_float=parse_float,
                parse_constant=reject_constant, object_pairs_hook=object_pairs,
            )
            return _TyveraVariant("Ok", Interpreter._json_from_python(value))
        except (json.JSONDecodeError, ValueError, RecursionError, TypeError):
            return _TyveraVariant("Err", "invalid JSON document")

    @staticmethod
    def _json_from_python(value: object) -> _TyveraJson:
        if value is None:
            return _TyveraJson("null", None)
        if type(value) is bool:
            return _TyveraJson("bool", value)
        if type(value) is int:
            if not INT_MIN <= value <= INT_MAX:
                raise ValueError("number outside Tyvera Int range")
            return _TyveraJson("int", value)
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("non-finite number")
            return _TyveraJson("float", value)
        if isinstance(value, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                raise ValueError("JSON string contains an invalid Unicode surrogate")
            return _TyveraJson("string", value)
        if isinstance(value, list):
            return _TyveraJson("array", [Interpreter._json_from_python(item) for item in value])
        if isinstance(value, dict):
            if any(any(0xD800 <= ord(character) <= 0xDFFF for character in key) for key in value):
                raise ValueError("JSON object key contains an invalid Unicode surrogate")
            return _TyveraJson("object", {key: Interpreter._json_from_python(item) for key, item in value.items()})
        raise ValueError("unsupported JSON value")

    @staticmethod
    def _json_to_python(value: object) -> object:
        if not isinstance(value, _TyveraJson):
            raise TypeError("jsonStringify expects a Json value")
        if value.kind == "array":
            return [Interpreter._json_to_python(item) for item in value.value]  # type: ignore[union-attr]
        if value.kind == "object":
            return {key: Interpreter._json_to_python(item) for key, item in value.value.items()}  # type: ignore[union-attr]
        return value.value

    @staticmethod
    def _call_json_method(
        receiver: _TyveraJson, method: str, arguments: list[object],
    ) -> object:
        value = receiver.value
        if method == "kind":
            return receiver.kind
        if method == "isNull":
            return receiver.kind == "null"
        if method == "asString":
            return _TyveraVariant("Some", value) if receiver.kind == "string" else _TyveraVariant("None")
        if method == "asInt":
            return _TyveraVariant("Some", value) if receiver.kind == "int" else _TyveraVariant("None")
        if method == "asFloat":
            return _TyveraVariant("Some", value) if receiver.kind == "float" else _TyveraVariant("None")
        if method == "asBool":
            return _TyveraVariant("Some", value) if receiver.kind == "bool" else _TyveraVariant("None")
        if method == "arrayLength":
            return _TyveraVariant("Some", len(value)) if receiver.kind == "array" else _TyveraVariant("None")
        if method == "objectLength":
            return _TyveraVariant("Some", len(value)) if receiver.kind == "object" else _TyveraVariant("None")
        if method == "arrayAt":
            index = arguments[0]
            if receiver.kind != "array" or type(index) is not int or index < 0 or index >= len(value):
                return _TyveraVariant("None")
            return _TyveraVariant("Some", value[index])
        if method == "objectGet":
            if receiver.kind != "object":
                return _TyveraVariant("None")
            found = value.get(arguments[0], _MISSING)
            return _TyveraVariant("None") if found is _MISSING else _TyveraVariant("Some", found)
        if method == "objectKeys":
            if receiver.kind == "object":
                return _TyveraVariant("Some", _TyveraList(list(value.keys())))
            return _TyveraVariant("None")
        raise AssertionError(f"checked Json method not implemented: {method}")

    @staticmethod
    def _matches(pattern, value: object) -> tuple[bool, dict[str, object]]:
        if isinstance(pattern, WildcardPattern):
            return True, {}
        if isinstance(pattern, BindingPattern):
            return True, {pattern.name: value}
        if isinstance(pattern, LiteralPattern):
            return _tyvera_equal(value, pattern.value), {}
        if isinstance(pattern, TuplePattern):
            if not isinstance(value, _TyveraTuple) or len(value.values) != len(pattern.elements):
                return False, {}
            bindings: dict[str, object] = {}
            for child, child_value in zip(pattern.elements, value.values):
                matched, child_bindings = Interpreter._matches(child, child_value)
                if not matched:
                    return False, {}
                bindings.update(child_bindings)
            return True, bindings
        if isinstance(pattern, RecordPattern):
            if isinstance(value, _TyveraNewtype) and value.type_name == pattern.name:
                if len(pattern.fields) != 1:
                    return False, {}
                return Interpreter._matches(pattern.fields[0], value.value)
            if not isinstance(value, _TyveraRecord) or value.type_name != pattern.name:
                return False, {}
            if len(value.fields) != len(pattern.fields):
                return False, {}
            bindings: dict[str, object] = {}
            for child, child_value in zip(pattern.fields, value.fields.values()):
                matched, child_bindings = Interpreter._matches(child, child_value)
                if not matched:
                    return False, {}
                bindings.update(child_bindings)
            return True, bindings
        if isinstance(pattern, EnumPattern):
            if (not isinstance(value, _TyveraVariant) or value.enum_name != pattern.enum_name
                    or value.variant != pattern.variant):
                return False, {}
            if value.payload_arity == 0:
                payloads = ()
            elif value.payload_arity == 1:
                payloads = (value.payload,)
            elif isinstance(value.payload, _TyveraTuple):
                payloads = value.payload.values
            elif pattern.payload_patterns:
                payloads = (value.payload,)
            else:
                payloads = ()
            if len(payloads) != len(pattern.payload_patterns):
                return False, {}
            bindings: dict[str, object] = {}
            for child, child_value in zip(pattern.payload_patterns, payloads):
                matched, child_bindings = Interpreter._matches(child, child_value)
                if not matched:
                    return False, {}
                bindings.update(child_bindings)
            return True, bindings
        if isinstance(pattern, VariantPattern):
            if not isinstance(value, _TyveraVariant) or value.variant != pattern.variant:
                return False, {}
            if pattern.variant == "None":
                return True, {}
            if pattern.payload_pattern is not None:
                return Interpreter._matches(pattern.payload_pattern, value.payload)
            if pattern.binding is not None:
                return True, {pattern.binding: value.payload}
            return True, {}
        raise AssertionError(f"unhandled pattern node: {type(pattern).__name__}")

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
            index = arguments[0]
            if type(index) is not int:
                raise AssertionError("checked collection index is not Int")
            if index < 0 or index >= len(values):
                return _TyveraVariant("None")
            return _TyveraVariant("Some", values[index])
        if method == "set":
            values[self._list_index(values, arguments[0], location)] = arguments[1]
            return None
        if method == "removeAt":
            index = arguments[0]
            if type(index) is not int:
                raise AssertionError("checked collection index is not Int")
            if index < 0 or index >= len(values):
                return _TyveraVariant("None")
            return _TyveraVariant("Some", values.pop(index))
        if method == "contains":
            return any(_tyvera_equal(arguments[0], item) for item in values)
        if method == "clear":
            values.clear()
            return None
        raise AssertionError(f"checked collection method not implemented: {method}")

    @staticmethod
    def _call_map_method(
        receiver: _TyveraMap, method: str, arguments: list[object], location: Location,
    ) -> object:
        try:
            if method == "put":
                receiver.put(arguments[0], arguments[1])
                return None
            if method == "get":
                value = receiver.get(arguments[0])
                return _TyveraVariant("None") if value is _MISSING else _TyveraVariant("Some", value)
            if method == "remove":
                value = receiver.remove(arguments[0])
                return _TyveraVariant("None") if value is _MISSING else _TyveraVariant("Some", value)
            if method == "containsKey":
                return receiver.contains_key(arguments[0])
            if method == "keys":
                return _TyveraList(receiver.keys())
            if method == "values":
                return _TyveraList(receiver.values())
            if method == "clear":
                receiver.clear()
                return None
        except TypeError as error:
            raise Diagnostic(location, "runtime", "Map key is not hashable under Tyvera's Map key rules") from error
        raise AssertionError(f"checked collection method not implemented: {method}")

    def _construct(
        self, declaration: ClassDeclaration, arguments: list[object], call_location: Location, depth: int,
    ) -> _TyveraInstance:
        instance = _TyveraInstance(declaration.name, {})
        constructor = next((method for method in declaration.methods if method.name == "init"), None)
        if constructor is not None:
            self._call_method(constructor, instance, arguments, call_location, depth)
        return instance

    def _spawn_actor(
        self, declaration: ActorDeclaration, arguments: list[object], call_location: Location, depth: int,
        supervisor: _TyveraSupervisor | None = None,
        restart_policy: RestartPolicy = RestartPolicy.PERMANENT,
    ) -> _TyveraActorRef:
        instance = _TyveraInstance(declaration.name, {})
        actor = _TyveraActor(
            instance,
            supervisor=supervisor,
            restart_policy=restart_policy,
            declaration=declaration,
            constructor_arguments=[self._copy_actor_value(value) for value in arguments],
        )
        self._actors.append(actor)
        instance.actor = actor
        if supervisor is not None:
            supervisor.children.append(actor)
        try:
            self._initialize_actor(actor, arguments, call_location, depth)
        except Diagnostic as error:
            actor.status = "crashed"
            actor.mailbox.clear()
            self._emit_runtime_event(RuntimeEvent(
                RuntimeEventKind.ACTOR_CRASHED,
                f"{declaration.name}#{actor.identity}",
                None if supervisor is None else supervisor.strategy,
                reason=CrashReason.runtime_error(error.message, error.location.filename,
                                                 error.location.line, error.location.column),
            ))
            if supervisor is None:
                raise
            failure = self._restart_supervised_actor(
                actor, self._format_actor_failure(declaration, error), call_location, depth,
            )
            if failure is not None:
                raise Diagnostic(call_location, "runtime", failure) from error
        return _TyveraActorRef(actor)

    def _initialize_actor(
        self, actor: _TyveraActor, arguments: list[object], call_location: Location, depth: int,
    ) -> None:
        declaration = actor.declaration
        if declaration is None:
            raise AssertionError("actor has no declaration")
        actor.status = "starting"
        actor.instance = _TyveraInstance(declaration.name, {}, actor)
        constructor = next((method for method in declaration.handlers if method.name == "init"), None)
        previous_actor = self._current_actor
        self._current_actor = actor
        try:
            if constructor is not None:
                self._call_method(constructor, actor.instance, arguments, call_location, depth)
        finally:
            self._current_actor = previous_actor
        actor.status = "alive"
        self._emit_runtime_event(RuntimeEvent(
            RuntimeEventKind.ACTOR_STARTED, f"{declaration.name}#{actor.identity}",
            None if actor.supervisor is None else actor.supervisor.strategy,
        ))

    @staticmethod
    def _copy_actor_value(value: object) -> object:
        if isinstance(value, _TyveraActorRef):
            return _TyveraActorRef(value.actor)
        if isinstance(value, _TyveraDuration):
            return value
        if isinstance(value, _TyveraReplySender):
            return _TyveraReplySender(value.cell)
        if isinstance(value, _TyveraBytes):
            # Bytes is an immutable value, so an actor receives an independent
            # Tyvera wrapper without sharing any mutable payload state.
            return _TyveraBytes(value.value)
        if isinstance(value, _TyveraVariant):
            return _TyveraVariant(value.variant, Interpreter._copy_actor_value(value.payload),
                                  value.enum_name, value.payload_arity)
        if value is None or type(value) in {bool, int, float, str} or isinstance(value, _TyveraByte):
            return value
        raise AssertionError("checked actor constructor value is not transferable")

    @staticmethod
    def _format_actor_failure(declaration: ActorDeclaration, error: Diagnostic) -> str:
        return (
            f"actor '{declaration.name}' crashed at {error.location.filename}:"
            f"{error.location.line}:{error.location.column}: {error.message}"
        )

    def _fail_pending_replies(self, actor: _TyveraActor, error: str) -> None:
        self._cancel_actor_tasks(actor)
        self._cancel_actor_timers(actor)
        for cell in actor.pending_replies:
            self._refresh_reply(cell)
            if cell.state == "pending":
                cell.state = "failed"
                cell.error = error
        actor.pending_replies.clear()

    def _stop_actor(self, actor: _TyveraActor, reply_error: str = "actor stopped before replying") -> None:
        was_live = actor.status in {"alive", "starting", "restarting"}
        self._cancel_actor_timers(actor)
        self._cancel_actor_tasks(actor)
        if actor.status in {"alive", "starting", "restarting"}:
            actor.status = "stopped"
        for cell in actor.pending_replies:
            self._refresh_reply(cell)
            if cell.state == "pending":
                cell.state = "failed"
                cell.error = reply_error
        actor.pending_replies.clear()
        actor.mailbox.clear()
        if was_live:
            name = actor.declaration.name if actor.declaration is not None else actor.instance.class_name
            self._emit_runtime_event(RuntimeEvent(
                RuntimeEventKind.ACTOR_STOPPED, f"{name}#{actor.identity}",
                None if actor.supervisor is None else actor.supervisor.strategy,
                reason=CrashReason.normal_stop(),
            ))

    def _cancel_actor_tasks(self, actor: _TyveraActor) -> None:
        for task in self._tasks:
            if task.owner_actor is actor:
                self._cancel_task(task)

    def _cancel_actor_timers(self, actor: _TyveraActor) -> None:
        for timer in self._scheduled_timers:
            if timer.scheduled_actor is actor and not timer.delivered:
                timer.cancelled = True
        self._prune_scheduled_timers()

    def _prune_scheduled_timers(self) -> None:
        self._scheduled_timers = [
            timer for timer in self._scheduled_timers if not timer.cancelled and not timer.delivered
        ]

    def _fail_supervisor(
        self, supervisor: _TyveraSupervisor, failure: str,
        call_location: Location, depth: int,
    ) -> str | None:
        supervisor.status = "failed"
        for child in reversed(supervisor.children):
            self._stop_supervised_child(child, "supervisor escalation")
        reason = CrashReason.escalation("restart intensity exceeded")
        self._emit_runtime_event(RuntimeEvent(
            RuntimeEventKind.SUPERVISOR_ESCALATED,
            f"Supervisor#{supervisor.identity}", supervisor.strategy,
            len(supervisor.restart_times), reason,
        ))
        if supervisor.parent is not None:
            return self._restart_supervised_supervisor(supervisor, failure, call_location, depth)
        return failure

    def _stop_supervised_child(self, child: _TyveraActor | _TyveraSupervisor, reason: str) -> None:
        if isinstance(child, _TyveraActor):
            self._stop_actor(child, reason)
        else:
            self._stop_supervisor_tree(child, reason)

    def _stop_supervisor_tree(self, supervisor: _TyveraSupervisor, reason: str) -> None:
        was_alive = supervisor.status == "alive"
        for child in reversed(supervisor.children):
            self._stop_supervised_child(child, reason)
        if supervisor.status in {"alive", "restarting"}:
            supervisor.status = "stopped"
        if was_alive:
            self._emit_runtime_event(RuntimeEvent(
                RuntimeEventKind.SUPERVISOR_STOPPED, f"Supervisor#{supervisor.identity}",
                supervisor.strategy, reason=CrashReason.normal_stop(),
            ))

    def _restart_targets(
        self, supervisor: _TyveraSupervisor, child: _TyveraActor | _TyveraSupervisor,
    ) -> tuple[_TyveraActor | _TyveraSupervisor, ...]:
        try:
            index = supervisor.children.index(child)
        except ValueError as error:
            raise AssertionError("supervised child is missing from its parent") from error
        if supervisor.strategy is RestartStrategy.ONE_FOR_ONE:
            indexes = (index,)
        elif supervisor.strategy is RestartStrategy.ONE_FOR_ALL:
            indexes = tuple(range(len(supervisor.children)))
        else:
            indexes = tuple(range(index, len(supervisor.children)))
        return tuple(supervisor.children[item] for item in indexes)

    def _schedule_supervisor_restart(
        self,
        supervisor: _TyveraSupervisor,
        trigger: _TyveraActor | _TyveraSupervisor,
        failure: str,
        call_location: Location,
        depth: int,
    ) -> str | None:
        if supervisor.status != "alive":
            return failure
        affected = self._restart_targets(supervisor, trigger)
        for child in reversed(affected):
            self._stop_supervised_child(child, "supervisor restart strategy")
            if isinstance(child, _TyveraActor):
                child.status = "restarting"
            else:
                child.status = "restarting"

        now = self._clock()
        window_seconds = supervisor.window_milliseconds / 1000.0
        while supervisor.restart_times and now - supervisor.restart_times[0] > window_seconds:
            supervisor.restart_times.popleft()
        if len(supervisor.restart_times) >= supervisor.max_restarts:
            label = (trigger.declaration.name if isinstance(trigger, _TyveraActor)
                     and trigger.declaration is not None else f"Supervisor#{trigger.identity}")
            limit_failure = (
                f"supervisor restart limit exceeded for actor '{label}' "
                f"within {supervisor.window_milliseconds}ms: {failure}"
            )
            return self._fail_supervisor(supervisor, limit_failure, call_location, depth)

        supervisor.restart_times.append(now)
        delay_ms = (0 if supervisor.backoff is None
                    else supervisor.backoff.delay_for(len(supervisor.restart_times)))
        self._pending_supervisor_restarts.append(_PendingSupervisorRestart(
            supervisor, affected, trigger, failure,
            now + delay_ms / 1000.0, call_location, depth,
        ))
        return self._settle_supervisor_restarts(wait=False)

    def _settle_supervisor_restarts(self, *, wait: bool) -> str | None:
        first_failure: str | None = None
        while self._pending_supervisor_restarts:
            self._pending_supervisor_restarts.sort(key=lambda pending: pending.due_at)
            pending = self._pending_supervisor_restarts[0]
            now = self._clock()
            if pending.due_at > now:
                if not wait:
                    break
                self._sleep(pending.due_at - now)
                continue
            self._pending_supervisor_restarts.pop(0)
            supervisor = pending.supervisor
            if supervisor.status != "alive":
                continue
            restart_failed: str | None = None
            for child in pending.children:
                if child.restart_policy is RestartPolicy.TEMPORARY:
                    if isinstance(child, _TyveraActor):
                        child.status = "stopped"
                    else:
                        child.status = "stopped"
                    continue
                try:
                    if isinstance(child, _TyveraActor):
                        declaration = child.declaration
                        if declaration is None:
                            raise AssertionError("supervised actor has no declaration")
                        self._initialize_actor(
                            child,
                            [self._copy_actor_value(value) for value in child.constructor_arguments],
                            pending.location,
                            pending.depth + 1,
                        )
                    else:
                        restart_failed = self._restart_supervisor_tree(
                            child, pending.location, pending.depth + 1,
                        )
                        if restart_failed is not None:
                            break
                except Diagnostic as error:
                    if isinstance(child, _TyveraActor):
                        child.status = "crashed"
                        declaration = child.declaration
                        label = declaration.name if declaration is not None else child.instance.class_name
                        restart_failed = f"actor '{label}' crashed at {error.location.filename}:{error.location.line}:{error.location.column}: {error.message}"
                        self._emit_runtime_event(RuntimeEvent(
                            RuntimeEventKind.ACTOR_CRASHED, f"{label}#{child.identity}",
                            supervisor.strategy,
                            reason=CrashReason.runtime_error(error.message, error.location.filename,
                                                             error.location.line, error.location.column),
                        ))
                    else:
                        restart_failed = f"nested supervisor failed to restart: {error.message}"
                    break
            if restart_failed is not None:
                trigger = pending.trigger
                if isinstance(trigger, _TyveraActor):
                    first_failure = self._restart_supervised_actor(
                        trigger, restart_failed, pending.location, pending.depth + 1,
                    ) or first_failure
                else:
                    first_failure = self._restart_supervised_supervisor(
                        trigger, restart_failed, pending.location, pending.depth + 1,
                    ) or first_failure
                continue
            reason = (
                CrashReason.normal_stop()
                if pending.failure.endswith("stopped normally")
                else CrashReason.runtime_error(
                    pending.failure, pending.location.filename,
                    pending.location.line, pending.location.column,
                )
            )
            for child in pending.children:
                if child.restart_policy is RestartPolicy.TEMPORARY:
                    continue
                if isinstance(child, _TyveraActor):
                    name = (child.declaration.name if child.declaration is not None
                            else child.instance.class_name)
                    entity = f"{name}#{child.identity}"
                    kind = RuntimeEventKind.ACTOR_RESTARTED
                else:
                    entity = f"Supervisor#{child.identity}"
                    kind = RuntimeEventKind.SUPERVISOR_RESTARTED
                self._emit_runtime_event(RuntimeEvent(
                    kind, entity, supervisor.strategy,
                    len(supervisor.restart_times), reason,
                ))
        return first_failure

    def _restart_supervisor_tree(
        self, supervisor: _TyveraSupervisor, call_location: Location, depth: int,
    ) -> str | None:
        supervisor.status = "alive"
        supervisor.restart_times.clear()
        self._emit_runtime_event(RuntimeEvent(
            RuntimeEventKind.SUPERVISOR_RESTARTED, f"Supervisor#{supervisor.identity}", supervisor.strategy,
        ))
        for child in reversed(supervisor.children):
            self._stop_supervised_child(child, "parent supervisor restart")
            if isinstance(child, _TyveraActor):
                child.status = "restarting"
            else:
                child.status = "restarting"
        for child in supervisor.children:
            if child.restart_policy is RestartPolicy.TEMPORARY:
                child.status = "stopped"
                continue
            if isinstance(child, _TyveraActor):
                declaration = child.declaration
                if declaration is None:
                    raise AssertionError("supervised actor has no declaration")
                try:
                    self._initialize_actor(
                        child, [self._copy_actor_value(value) for value in child.constructor_arguments],
                        call_location, depth + 1,
                    )
                except Diagnostic as error:
                    child.status = "crashed"
                    return self._restart_supervised_actor(
                        child,
                        f"actor '{declaration.name}' crashed at {error.location.filename}:{error.location.line}:{error.location.column}: {error.message}",
                        call_location, depth + 1,
                    )
            else:
                failure = self._restart_supervisor_tree(child, call_location, depth + 1)
                if failure is not None:
                    return failure
        return None

    def _create_async_task(
        self, function: Function, arguments: list[object], location: Location, depth: int,
    ) -> _TyveraTask:
        if not function.is_async:
            raise AssertionError("only async functions create Task values")
        if depth >= self.max_call_depth:
            error = Diagnostic(location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
            self._cleanup_failed_argument_transfer(arguments, error)
            raise error
        variables = ChainMap({parameter.name: value for parameter, value in zip(function.parameters, arguments)})
        task = _TyveraTask(function, variables, location, depth + 1, canonical_type(function.return_type))
        task.parent = self._current_task
        task.owner_actor = self._current_actor
        if task.parent is not None:
            task.parent.children.append(task)
        task.frames.append(_AsyncBlockFrame(function.body, variables))
        self._tasks.append(task)
        self._ready_tasks.append(task)
        return task

    def _create_sleep_task(self, duration: _TyveraDuration, location: Location) -> _TyveraTask:
        now = self._clock()
        task = _TyveraTask(None, None, location, state="sleeping",
                           due_at=now + max(0, duration.milliseconds) / 1000.0,
                           parent=self._current_task, owner_actor=self._current_actor)
        if task.parent is not None:
            task.parent.children.append(task)
        self._tasks.append(task)
        return task

    def _create_io_task(self, result_type: str, location: Location) -> _TyveraTask:
        self._get_io_backend()
        task = _TyveraTask(None, None, location, state="io_wait", result_type=canonical_type(result_type),
                           parent=self._current_task, owner_actor=self._current_actor)
        if task.parent is not None:
            task.parent.children.append(task)
        self._tasks.append(task)
        return task

    def _create_native_task(
        self, result_type: str, location: Location, step: Callable[[_TyveraTask], None],
        state: dict[str, object], *, parent: _TyveraTask | None = None,
        resources: list[object] | None = None,
    ) -> _TyveraTask:
        task = _TyveraTask(
            None, None, location, result_type=canonical_type(result_type),
            parent=self._current_task if parent is None else parent,
            owner_actor=(parent.owner_actor if parent is not None else self._current_actor),
            native_step=step, native_state=state,
            native_resources=[] if resources is None else resources.copy(),
        )
        if task.parent is not None:
            task.parent.children.append(task)
        self._tasks.append(task)
        self._ready_tasks.append(task)
        return task

    def _native_wait(self, task: _TyveraTask, target: _TyveraTask) -> None:
        state, error = self._await_state(target)
        if state == "pending":
            task.wait_target = target
            task.state = "waiting"
            return
        if state == "failed":
            task.native_wait_error = error or "HTTP operation failed"
            return
        value = target.result
        if self._resource_handles(value):
            target.result = None
        task.native_wait_result = value

    @staticmethod
    def _take_native_wait(task: _TyveraTask) -> tuple[bool, object, str | None]:
        if task.native_wait_result is _MISSING and task.native_wait_error is None:
            return False, None, None
        value, error = task.native_wait_result, task.native_wait_error
        task.native_wait_result = _MISSING
        task.native_wait_error = None
        return True, value, error

    def _timeout_task_for_native_wait(
        self, target: _TyveraTask, duration: _TyveraDuration, location: Location,
    ) -> _TyveraTask:
        timed = self._create_timeout_task(target, duration, location)
        if timed.parent is not None:
            timed.parent.children.remove(timed)
            timed.parent = None
        return timed

    def _get_io_backend(self) -> IOBackend:
        if self._io_backend is None:
            self._io_backend = IOBackend()
        return self._io_backend

    @staticmethod
    def _task_is_active(task: _TyveraTask) -> bool:
        return task.state not in {"completed", "failed", "cancelled"}

    def _complete_io_task(self, task: _TyveraTask, result: object) -> None:
        task.io_cancel = None
        resource = task.io_resource
        if isinstance(resource, _TyveraNetworkResource):
            if resource.pending_read_task is task:
                resource.pending_read_task = None
                resource.pending_read = False
            if resource.pending_write_task is task:
                resource.pending_write_task = None
                resource.pending_write = False
        task.io_resource = None
        if not self._task_is_active(task):
            for resource in self._resource_handles(result):
                self._close_resource(resource)
            return
        task.result = result
        task.state = "completed"

    @staticmethod
    def _network_error(error: BaseException) -> str:
        if isinstance(error, IOBackendError):
            if "capacity" in str(error):
                return "I/O worker capacity is full"
            return "I/O runtime is closed"
        if isinstance(error, ssl.SSLCertVerificationError):
            return "TLS certificate verification failed"
        if isinstance(error, ssl.SSLError):
            return "TLS operation failed"
        if isinstance(error, socket.gaierror):
            return "DNS resolution failed"
        code = getattr(error, "errno", None)
        if code == errno.ECONNREFUSED:
            return "connection refused"
        if code in {errno.ECONNRESET, errno.EPIPE, errno.ESHUTDOWN}:
            return "connection reset"
        if code == errno.EADDRINUSE:
            return "address already in use"
        if code in {errno.EACCES, errno.EPERM}:
            return "permission denied"
        if isinstance(error, (TimeoutError, socket.timeout)):
            return "operation timed out"
        if isinstance(error, (ValueError, OverflowError)) or code == errno.EINVAL:
            return "invalid address"
        return "network operation failed"

    @staticmethod
    def _io_runtime_error(error: BaseException) -> str:
        if isinstance(error, IOBackendError) and "capacity" in str(error):
            return "I/O worker capacity is full"
        return "I/O runtime is closed"

    @staticmethod
    def _socket_address(address: object, family: int) -> _TyveraSocketAddress:
        if not isinstance(address, tuple) or len(address) < 2:
            raise ValueError("backend returned an invalid socket address")
        return _TyveraSocketAddress(str(address[0]), int(address[1]), "IPv6" if family == socket.AF_INET6 else "IPv4")

    def _resolve_host_task(self, host: str, port: int, location: Location) -> _TyveraTask:
        task = self._create_io_task("Result<List<SocketAddress>,String>", location)
        if not host.strip() or not 0 <= port <= 65535:
            self._complete_io_task(task, _TyveraVariant("Err", "invalid address", "Result", 1))
            return task
        try:
            future = self._get_io_backend().submit(
                lambda: socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM),
                lambda addresses, error: self._finish_resolution(task, addresses, error),
            )
        except RuntimeError as error:
            self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
        else:
            task.io_cancel = future.cancel
        return task

    def _finish_resolution(self, task: _TyveraTask, addresses: object, error: BaseException | None) -> None:
        if error is not None:
            self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
            return
        if not isinstance(addresses, list):
            self._complete_io_task(task, _TyveraVariant("Err", "DNS resolution failed", "Result", 1))
            return
        values: list[_TyveraSocketAddress] = []
        seen: set[tuple[str, int, str]] = set()
        for family, _, _, _, sockaddr in addresses:
            try:
                address = self._socket_address(sockaddr, family)
            except (TypeError, ValueError):
                continue
            key = (address.host, address.port, address.family)
            if key not in seen:
                seen.add(key)
                values.append(address)
        self._complete_io_task(task, _TyveraVariant("Ok", _TyveraList(values), "Result", 1))

    def _tcp_connect_task(
        self, host: str, port: int, location: Location, *, secure: bool = False,
    ) -> _TyveraTask:
        resource_type = "TlsConnection" if secure else "TcpConnection"
        task = self._create_io_task(f"Result<{resource_type},String>", location)
        if not host.strip() or not 1 <= port <= 65535:
            self._complete_io_task(task, _TyveraVariant("Err", "invalid address", "Result", 1))
            return task
        task_state: dict[str, object] = {
            "addresses": [], "index": 0, "stream": None, "watch": None, "watch_event": None,
        }

        def cancel() -> None:
            stream = task_state.get("stream")
            watch = task_state.get("watch")
            watch_event = task_state.get("watch_event")
            if isinstance(stream, socket.socket):
                if callable(watch) and isinstance(watch_event, int):
                    self._get_io_backend().unwatch(stream, watch_event, watch)
                try:
                    stream.close()
                except OSError:
                    pass
            task_state["stream"] = None

        task.io_cancel = cancel

        def resolved(addresses: object, error: BaseException | None) -> None:
            if not self._task_is_active(task):
                return
            if error is not None:
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
                return
            task_state["addresses"] = addresses if isinstance(addresses, list) else []
            try_next()

        def try_next() -> None:
            if not self._task_is_active(task):
                return
            addresses = task_state["addresses"]
            index = int(task_state["index"])
            if not isinstance(addresses, list) or index >= len(addresses):
                self._complete_io_task(task, _TyveraVariant("Err", "connection failed", "Result", 1))
                return
            family, socktype, protocol, _, address = addresses[index]
            task_state["index"] = index + 1
            try:
                stream = socket.socket(family, socktype, protocol)
                stream.setblocking(False)
                status = stream.connect_ex(address)
            except OSError as error:
                try:
                    stream.close()
                except (UnboundLocalError, OSError):
                    pass
                if index + 1 < len(addresses):
                    try_next()
                else:
                    self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
                return
            task_state["stream"] = stream
            if status in {0, errno.EISCONN}:
                succeed(stream)
                return
            if status not in {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR,
                              getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK)}:
                stream.close()
                task_state["stream"] = None
                if index + 1 < len(addresses):
                    try_next()
                else:
                    self._complete_io_task(task, _TyveraVariant("Err", self._network_error(OSError(status, "connect")), "Result", 1))
                return

            def connected() -> None:
                task_state["watch"] = None
                if not self._task_is_active(task):
                    return
                try:
                    connect_error = stream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                except OSError as error:
                    connect_error = error.errno or errno.EIO
                if connect_error == 0:
                    succeed(stream)
                    return
                stream.close()
                task_state["stream"] = None
                if int(task_state["index"]) < len(addresses):
                    try_next()
                else:
                    self._complete_io_task(task, _TyveraVariant("Err", self._network_error(OSError(connect_error, "connect")), "Result", 1))

            task_state["watch"] = connected
            try:
                self._get_io_backend().watch(stream, selectors.EVENT_WRITE, connected)
            except (OSError, RuntimeError) as error:
                stream.close()
                task_state["stream"] = None
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))

        def succeed(stream: socket.socket) -> None:
            if not self._task_is_active(task):
                stream.close()
                return
            if secure:
                try:
                    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
                    stream = context.wrap_socket(
                        stream, server_hostname=host, do_handshake_on_connect=False,
                    )
                    stream.setblocking(False)
                except (OSError, ValueError, ssl.SSLError) as error:
                    try:
                        stream.close()
                    except OSError:
                        pass
                    message = ("TLS certificate verification failed"
                               if isinstance(error, ssl.SSLCertVerificationError)
                               else "TLS connection setup failed")
                    self._complete_io_task(task, _TyveraVariant("Err", message, "Result", 1))
                    return
                task_state["stream"] = stream
                tls_handshake(stream)
                return
            task_state["stream"] = None
            self._complete_io_task(task, _TyveraVariant(
                "Ok", _TyveraNetworkResource(stream, resource_type, location=location), "Result", 1,
            ))

        def tls_handshake(stream: ssl.SSLSocket) -> None:
            if not self._task_is_active(task):
                stream.close()
                task_state["stream"] = None
                return
            try:
                stream.do_handshake()
            except ssl.SSLWantReadError:
                wait_for_tls(stream, selectors.EVENT_READ)
            except ssl.SSLWantWriteError:
                wait_for_tls(stream, selectors.EVENT_WRITE)
            except ssl.SSLCertVerificationError:
                stream.close()
                task_state["stream"] = None
                self._complete_io_task(task, _TyveraVariant(
                    "Err", "TLS certificate verification failed", "Result", 1,
                ))
            except (ssl.SSLError, OSError):
                stream.close()
                task_state["stream"] = None
                self._complete_io_task(task, _TyveraVariant(
                    "Err", "TLS handshake failed", "Result", 1,
                ))
            else:
                task_state["stream"] = None
                self._complete_io_task(task, _TyveraVariant(
                    "Ok", _TyveraNetworkResource(stream, "TlsConnection", location=location),
                    "Result", 1,
                ))

        def wait_for_tls(stream: ssl.SSLSocket, event: int) -> None:
            def ready() -> None:
                task_state["watch"] = None
                task_state["watch_event"] = None
                tls_handshake(stream)

            task_state["watch"] = ready
            task_state["watch_event"] = event
            try:
                self._get_io_backend().watch(stream, event, ready)
            except (OSError, RuntimeError) as error:
                stream.close()
                task_state["stream"] = None
                self._complete_io_task(task, _TyveraVariant(
                    "Err", self._network_error(error), "Result", 1,
                ))

        try:
            future = self._get_io_backend().submit(
                lambda: socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM), resolved,
            )
        except RuntimeError as error:
            self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
        else:
            task.io_cancel = lambda: (cancel(), future.cancel())
        return task

    def _tcp_listen_task(self, host: str, port: int, backlog: int, location: Location) -> _TyveraTask:
        task = self._create_io_task("Result<TcpListener,String>", location)
        if not host.strip() or not 0 <= port <= 65535:
            self._complete_io_task(task, _TyveraVariant("Err", "invalid address", "Result", 1))
            return task
        if not 1 <= backlog <= 4096:
            self._complete_io_task(task, _TyveraVariant("Err", "invalid listener backlog", "Result", 1))
            return task

        def resolved(addresses: object, error: BaseException | None) -> None:
            if not self._task_is_active(task):
                return
            if error is not None or not isinstance(addresses, list):
                self._complete_io_task(task, _TyveraVariant(
                    "Err", self._network_error(error or OSError("no address")), "Result", 1,
                ))
                return
            last_error: BaseException = OSError("no address")
            for family, socktype, protocol, _, address in addresses:
                stream: socket.socket | None = None
                try:
                    stream = socket.socket(family, socktype, protocol)
                    stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                        stream.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    stream.bind(address)
                    stream.listen(backlog)
                    stream.setblocking(False)
                    self._complete_io_task(task, _TyveraVariant(
                        "Ok", _TyveraNetworkResource(stream, "TcpListener", location=location), "Result", 1,
                    ))
                    return
                except OSError as caught:
                    last_error = caught
                    if stream is not None:
                        stream.close()
            self._complete_io_task(task, _TyveraVariant("Err", self._network_error(last_error), "Result", 1))

        try:
            future = self._get_io_backend().submit(
                lambda: socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE),
                resolved,
            )
        except RuntimeError as error:
            self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
        else:
            task.io_cancel = future.cancel
        return task

    def _tcp_accept_task(self, listener: _TyveraNetworkResource, location: Location) -> _TyveraTask:
        task = self._create_io_task("Result<TcpConnection,String>", location)
        task.io_resource = listener
        if listener.pending_read:
            self._complete_io_task(task, _TyveraVariant("Err", "listener already has a pending accept", "Result", 1))
            return task

        def attempt() -> None:
            if not self._task_is_active(task):
                return
            try:
                stream, _ = listener.stream.accept()
                stream.setblocking(False)
            except BlockingIOError:
                register()
                return
            except OSError as error:
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
                return
            self._complete_io_task(task, _TyveraVariant(
                "Ok", _TyveraNetworkResource(stream, "TcpConnection", location=location), "Result", 1,
            ))

        def register() -> None:
            if listener.pending_read:
                self._complete_io_task(task, _TyveraVariant("Err", "listener already has a pending accept", "Result", 1))
                return
            listener.pending_read = True
            listener.pending_read_task = task

            def ready() -> None:
                listener.pending_read = False
                listener.pending_read_task = None
                attempt()

            task.io_cancel = lambda: self._cancel_socket_wait(listener, selectors.EVENT_READ, ready, "read", task)
            try:
                self._get_io_backend().watch(listener.stream, selectors.EVENT_READ, ready)
            except (OSError, RuntimeError) as error:
                listener.pending_read = False
                listener.pending_read_task = None
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))

        attempt()
        return task

    def _tcp_read_task(self, connection: _TyveraNetworkResource, max_bytes: int, location: Location) -> _TyveraTask:
        task = self._create_io_task("Result<Option<Bytes>,String>", location)
        task.io_resource = connection
        if max_bytes < 1 or max_bytes > _MAX_NETWORK_READ:
            self._complete_io_task(task, _TyveraVariant("Err", f"read size must be between 1 and {_MAX_NETWORK_READ} bytes", "Result", 1))
            return task
        if connection.pending_read:
            self._complete_io_task(task, _TyveraVariant("Err", "connection already has a pending read", "Result", 1))
            return task

        def attempt() -> None:
            if not self._task_is_active(task):
                return
            try:
                data = connection.stream.recv(max_bytes)
            except ssl.SSLWantReadError:
                register(selectors.EVENT_READ)
                return
            except ssl.SSLWantWriteError:
                register(selectors.EVENT_WRITE)
                return
            except ssl.SSLZeroReturnError:
                data = b""
            except BlockingIOError:
                register()
                return
            except ssl.SSLEOFError:
                self._complete_io_task(task, _TyveraVariant(
                    "Err", "TLS connection closed without close_notify", "Result", 1,
                ))
                return
            except OSError as error:
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
                return
            value = _TyveraVariant("None", None, "Option", 1) if not data else _TyveraVariant(
                "Some", _TyveraBytes(data), "Option", 1,
            )
            self._complete_io_task(task, _TyveraVariant("Ok", value, "Result", 1))

        def register(event: int = selectors.EVENT_READ) -> None:
            if connection.pending_read:
                self._complete_io_task(task, _TyveraVariant("Err", "connection already has a pending read", "Result", 1))
                return
            connection.pending_read = True
            connection.pending_read_task = task

            def ready() -> None:
                connection.pending_read = False
                connection.pending_read_task = None
                attempt()

            task.io_cancel = lambda: self._cancel_socket_wait(connection, event, ready, "read", task)
            try:
                self._get_io_backend().watch(connection.stream, event, ready)
            except (OSError, RuntimeError) as error:
                connection.pending_read = False
                connection.pending_read_task = None
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))

        attempt()
        return task

    def _tcp_write_task(self, connection: _TyveraNetworkResource, data: bytes, write_all: bool,
                        location: Location) -> _TyveraTask:
        task = self._create_io_task("Result<Int,String>", location)
        task.io_resource = connection
        offset = 0
        payload = memoryview(data)
        if connection.pending_write:
            self._complete_io_task(task, _TyveraVariant("Err", "connection already has a pending write", "Result", 1))
            return task

        def attempt() -> None:
            nonlocal offset
            if not self._task_is_active(task):
                return
            try:
                sent = connection.stream.send(payload[offset:])
            except ssl.SSLWantReadError:
                register(selectors.EVENT_READ)
                return
            except ssl.SSLWantWriteError:
                register(selectors.EVENT_WRITE)
                return
            except BlockingIOError:
                register()
                return
            except OSError as error:
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))
                return
            if sent <= 0:
                self._complete_io_task(task, _TyveraVariant("Err", "connection reset", "Result", 1))
                return
            if not write_all:
                self._complete_io_task(task, _TyveraVariant("Ok", sent, "Result", 1))
                return
            offset += sent
            if offset >= len(data):
                self._complete_io_task(task, _TyveraVariant("Ok", offset, "Result", 1))
            else:
                register()

        def register(event: int = selectors.EVENT_WRITE) -> None:
            if connection.pending_write:
                self._complete_io_task(task, _TyveraVariant("Err", "connection already has a pending write", "Result", 1))
                return
            connection.pending_write = True
            connection.pending_write_task = task

            def ready() -> None:
                connection.pending_write = False
                connection.pending_write_task = None
                attempt()

            task.io_cancel = lambda: self._cancel_socket_wait(connection, event, ready, "write", task)
            try:
                self._get_io_backend().watch(connection.stream, event, ready)
            except (OSError, RuntimeError) as error:
                connection.pending_write = False
                connection.pending_write_task = None
                self._complete_io_task(task, _TyveraVariant("Err", self._network_error(error), "Result", 1))

        if not data:
            self._complete_io_task(task, _TyveraVariant("Ok", 0, "Result", 1))
        else:
            attempt()
        return task

    def _websocket_connect_task(self, address: str, timeout: _TyveraDuration,
                                location: Location) -> _TyveraTask:
        state: dict[str, object] = {
            "phase": "connect", "operation": None, "url": None, "connection": None,
            "key": None, "request": None, "decoder": None, "frame_decoder": None,
            "handshake_bytes": 0, "timeout": timeout.milliseconds, "failure": None,
        }
        try:
            state["url"] = parse_websocket_url(address)
        except WebSocketProtocolError as error:
            state["failure"] = str(error)
        if timeout.milliseconds <= 0:
            state["failure"] = "WebSocket timeout must be greater than zero"
        return self._create_native_task(
            "Result<WebSocket,String>", location,
            self._advance_websocket_connect_task, state,
        )

    def _schedule_websocket_connect_io(self, task: _TyveraTask) -> None:
        state = task.native_state
        phase = state["phase"]
        if phase == "connect":
            url = state["url"]
            assert isinstance(url, WebSocketUrl)
            operation = self._tcp_connect_task(
                url.transport.host, url.transport.port, task.location,
                secure=url.scheme == "wss",
            )
        elif phase == "handshake-write":
            connection = state["connection"]
            request = state["request"]
            assert isinstance(connection, _TyveraNetworkResource) and isinstance(request, bytes)
            operation = self._tcp_write_task(connection, request, True, task.location)
        elif phase == "handshake-read":
            connection = state["connection"]
            assert isinstance(connection, _TyveraNetworkResource)
            operation = self._tcp_read_task(connection, 4096, task.location)
        else:
            raise AssertionError("unknown WebSocket connection phase")
        timed = self._timeout_task_for_native_wait(
            operation, _TyveraDuration(int(state["timeout"])), task.location,
        )
        task.native_resources.append(timed)
        state["operation"] = timed
        self._native_wait(task, timed)

    def _finish_websocket_connect(self, task: _TyveraTask, result: _TyveraVariant) -> None:
        state = task.native_state
        connection = state.get("connection")
        if isinstance(connection, _TyveraNetworkResource):
            state["connection"] = None
            self._close_resource(connection)
        task.result = result
        self._complete_task(task)

    def _advance_websocket_connect_task(self, task: _TyveraTask) -> None:
        state = task.native_state
        failure = state.get("failure")
        if isinstance(failure, str):
            self._finish_websocket_connect(task, _TyveraVariant("Err", failure, "Result", 1))
            return
        operation = state.get("operation")
        if not isinstance(operation, _TyveraTask):
            self._schedule_websocket_connect_io(task)
            return
        ready, value, error = self._take_native_wait(task)
        if not ready:
            self._native_wait(task, operation)
            ready, value, error = self._take_native_wait(task)
            if not ready:
                return
        state["operation"] = None
        phase = state["phase"]
        if error is not None:
            detail = "timed out" if error == "task timed out" else error
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", f"WebSocket {phase} failed: {detail}", "Result", 1,
            ))
            return
        if not isinstance(value, _TyveraVariant) or value.variant != "Ok":
            detail = str(value.payload) if isinstance(value, _TyveraVariant) else "invalid operation completion"
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", f"WebSocket {phase} failed: {detail}", "Result", 1,
            ))
            return
        result = value.payload
        if phase == "connect":
            if not isinstance(result, _TyveraNetworkResource):
                self._finish_websocket_connect(task, _TyveraVariant(
                    "Err", "WebSocket transport returned no connection", "Result", 1,
                ))
                return
            url = state["url"]
            assert isinstance(url, WebSocketUrl)
            try:
                key, request = create_client_handshake(url)
            except WebSocketProtocolError as error:
                state["connection"] = result
                self._finish_websocket_connect(task, _TyveraVariant("Err", str(error), "Result", 1))
                return
            result.resource_type = "WebSocket"
            result.operation_timeout_milliseconds = int(state["timeout"])
            state["connection"] = result
            state["key"] = key
            state["request"] = request
            state["decoder"] = HttpMessageDecoder(
                HttpLimits(request_line_bytes=2048, header_bytes=16_384,
                           header_count=64, body_bytes=0, max_connections=1),
                response=True, request_method="GET",
            )
            state["frame_decoder"] = WebSocketFrameDecoder()
            state["phase"] = "handshake-write"
            self._advance_websocket_connect_task(task)
            return
        if phase == "handshake-write":
            if not isinstance(result, int):
                self._finish_websocket_connect(task, _TyveraVariant(
                    "Err", "WebSocket handshake write failed", "Result", 1,
                ))
                return
            state["phase"] = "handshake-read"
            self._schedule_websocket_connect_io(task)
            return
        if not isinstance(result, _TyveraVariant):
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", "WebSocket handshake read failed", "Result", 1,
            ))
            return
        if result.variant == "None":
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", "WebSocket server closed during handshake", "Result", 1,
            ))
            return
        payload = result.payload
        if not isinstance(payload, _TyveraBytes):
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", "invalid WebSocket handshake bytes", "Result", 1,
            ))
            return
        received = int(state["handshake_bytes"]) + len(payload.value)
        if received > MAX_WEBSOCKET_HANDSHAKE_BYTES:
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", "WebSocket handshake exceeds size limit", "Result", 1,
            ))
            return
        state["handshake_bytes"] = received
        decoder = state["decoder"]
        key = state["key"]
        assert isinstance(decoder, HttpMessageDecoder) and isinstance(key, str)
        try:
            decoder.feed(payload.value)
            response = validate_server_handshake(decoder, key)
        except (HttpProtocolError, WebSocketProtocolError) as protocol_error:
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", str(protocol_error), "Result", 1,
            ))
            return
        if response is None:
            self._schedule_websocket_connect_io(task)
            return
        connection = state.get("connection")
        frame_decoder = state["frame_decoder"]
        assert isinstance(connection, _TyveraNetworkResource)
        assert isinstance(frame_decoder, WebSocketFrameDecoder)
        try:
            if decoder.buffer:
                frame_decoder.feed(bytes(decoder.buffer))
                decoder.buffer.clear()
        except WebSocketProtocolError as protocol_error:
            self._finish_websocket_connect(task, _TyveraVariant(
                "Err", str(protocol_error), "Result", 1,
            ))
            return
        connection.websocket_decoder = frame_decoder
        state["connection"] = None
        self._finish_websocket_connect(task, _TyveraVariant("Ok", connection, "Result", 1))

    def _websocket_send_task(self, connection: _TyveraNetworkResource, payload: bytes,
                             opcode: int, location: Location) -> _TyveraTask:
        state: dict[str, object] = {
            "connection": connection, "payload": payload, "operation": None,
            "failure": None, "phase": "write",
        }
        try:
            state["frame"] = encode_client_frame(opcode, payload)
        except WebSocketProtocolError as error:
            state["failure"] = str(error)
        return self._create_native_task(
            "Result<Int,String>", location, self._advance_websocket_send_task, state,
        )

    def _advance_websocket_send_task(self, task: _TyveraTask) -> None:
        state = task.native_state
        failure = state.get("failure")
        if isinstance(failure, str):
            task.result = _TyveraVariant("Err", failure, "Result", 1)
            self._complete_task(task)
            return
        operation = state.get("operation")
        if not isinstance(operation, _TyveraTask):
            connection, frame = state["connection"], state["frame"]
            assert isinstance(connection, _TyveraNetworkResource) and isinstance(frame, bytes)
            writing = self._tcp_write_task(connection, frame, True, task.location)
            duration = max(1, connection.operation_timeout_milliseconds)
            timed = self._timeout_task_for_native_wait(writing, _TyveraDuration(duration), task.location)
            task.native_resources.append(timed)
            state["operation"] = timed
            self._native_wait(task, timed)
            return
        ready, value, error = self._take_native_wait(task)
        if not ready:
            self._native_wait(task, operation)
            ready, value, error = self._take_native_wait(task)
            if not ready:
                return
        if error is not None:
            message = "WebSocket write timed out" if error == "task timed out" else f"WebSocket write failed: {error}"
        elif isinstance(value, _TyveraVariant) and value.variant == "Ok":
            payload = state["payload"]
            assert isinstance(payload, bytes)
            task.result = _TyveraVariant("Ok", len(payload), "Result", 1)
            self._complete_task(task)
            return
        else:
            message = (f"WebSocket write failed: {value.payload}"
                       if isinstance(value, _TyveraVariant) else "WebSocket write failed")
        task.result = _TyveraVariant("Err", message, "Result", 1)
        self._complete_task(task)

    def _websocket_receive_task(
        self, connection: _TyveraNetworkResource, location: Location,
        *, timeout_milliseconds: int | None = None,
    ) -> _TyveraTask:
        selected_timeout = (connection.operation_timeout_milliseconds
                            if timeout_milliseconds is None else timeout_milliseconds)
        if selected_timeout <= 0:
            task = self._create_io_task("Result<Option<WebSocketMessage>,String>", location)
            self._complete_io_task(task, _TyveraVariant(
                "Err", "WebSocket receive timeout must be greater than zero", "Result", 1,
            ))
            return task
        return self._create_native_task(
            "Result<Option<WebSocketMessage>,String>", location,
            self._advance_websocket_receive_task,
            {"connection": connection, "operation": None, "phase": "read", "failure": None,
             "timeout_milliseconds": selected_timeout,
             "keep_open_on_timeout": timeout_milliseconds is not None},
        )

    def _advance_websocket_receive_task(self, task: _TyveraTask) -> None:
        state = task.native_state
        connection = state["connection"]
        assert isinstance(connection, _TyveraNetworkResource)
        if isinstance(state.get("failure"), str):
            task.result = _TyveraVariant("Err", state["failure"], "Result", 1)
            self._complete_task(task)
            return
        operation = state.get("operation")
        if not isinstance(operation, _TyveraTask):
            if state["phase"] == "read":
                decoder = connection.websocket_decoder
                if decoder is None:
                    state["failure"] = "WebSocket decoder is unavailable"
                    self._advance_websocket_receive_task(task)
                    return
                try:
                    event = decoder.next_event()
                except WebSocketProtocolError as error:
                    state["failure"] = str(error)
                    self._advance_websocket_receive_task(task)
                    return
                if event is not None:
                    self._handle_websocket_event(task, event)
                    return
                reading = self._tcp_read_task(connection, 65_536, task.location)
                duration = int(state["timeout_milliseconds"])
                timed = self._timeout_task_for_native_wait(reading, _TyveraDuration(duration), task.location)
                task.native_resources.append(timed)
                state["operation"] = timed
                self._native_wait(task, timed)
                return
            frame = state.get("frame")
            assert isinstance(frame, bytes)
            writing = self._tcp_write_task(connection, frame, True, task.location)
            duration = int(state["timeout_milliseconds"])
            timed = self._timeout_task_for_native_wait(writing, _TyveraDuration(duration), task.location)
            task.native_resources.append(timed)
            state["operation"] = timed
            self._native_wait(task, timed)
            return
        ready, value, error = self._take_native_wait(task)
        if not ready:
            self._native_wait(task, operation)
            ready, value, error = self._take_native_wait(task)
            if not ready:
                return
        state["operation"] = None
        if error is not None:
            timed_out = error == "task timed out"
            if not (timed_out and state.get("keep_open_on_timeout")):
                self._close_resource(connection)
            message = ("WebSocket receive timed out" if timed_out and state.get("keep_open_on_timeout")
                       else "WebSocket operation timed out" if timed_out
                       else f"WebSocket operation failed: {error}")
            task.result = _TyveraVariant("Err", message, "Result", 1)
            self._complete_task(task)
            return
        if not isinstance(value, _TyveraVariant) or value.variant != "Ok":
            self._close_resource(connection)
            message = (f"WebSocket operation failed: {value.payload}"
                       if isinstance(value, _TyveraVariant) else "WebSocket operation failed")
            task.result = _TyveraVariant("Err", message, "Result", 1)
            self._complete_task(task)
            return
        if state["phase"] == "write-close":
            self._close_resource(connection)
            close_error = value.variant != "Ok"
            task.result = (_TyveraVariant("Err", "WebSocket close handshake failed", "Result", 1)
                           if close_error else _TyveraVariant(
                               "Ok", _TyveraVariant("None", None, "Option", 1), "Result", 1,
                           ))
            self._complete_task(task)
            return
        if state["phase"] == "write-control":
            if value.variant != "Ok":
                self._close_resource(connection)
                task.result = _TyveraVariant("Err", "WebSocket control frame write failed", "Result", 1)
                self._complete_task(task)
                return
            state["phase"] = "read"
            self._advance_websocket_receive_task(task)
            return
        received = value.payload
        if not isinstance(received, _TyveraVariant):
            task.result = _TyveraVariant("Err", "invalid WebSocket read completion", "Result", 1)
            self._complete_task(task)
            return
        if received.variant == "None":
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", "WebSocket connection closed without a close frame", "Result", 1)
            self._complete_task(task)
            return
        payload = received.payload
        decoder = connection.websocket_decoder
        assert isinstance(payload, _TyveraBytes) and isinstance(decoder, WebSocketFrameDecoder)
        try:
            decoder.feed(payload.value)
        except WebSocketProtocolError as error:
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", str(error), "Result", 1)
            self._complete_task(task)
            return
        state["phase"] = "read"
        self._advance_websocket_receive_task(task)

    def _handle_websocket_event(self, task: _TyveraTask, event: WebSocketEvent) -> None:
        state = task.native_state
        connection = state["connection"]
        assert isinstance(connection, _TyveraNetworkResource)
        if event.opcode in {1, 2}:
            task.result = _TyveraVariant(
                "Ok", _TyveraVariant("Some", _TyveraWebSocketMessage(event.opcode, event.payload), "Option", 1),
                "Result", 1,
            )
            self._complete_task(task)
            return
        if event.opcode == 10:
            self._advance_websocket_receive_task(task)
            return
        if event.opcode == 9:
            try:
                state["frame"] = encode_client_frame(10, event.payload)
            except WebSocketProtocolError as error:
                task.result = _TyveraVariant("Err", str(error), "Result", 1)
                self._complete_task(task)
                return
            state["phase"] = "write-control"
            self._advance_websocket_receive_task(task)
            return
        if event.opcode == 8:
            try:
                state["frame"] = encode_client_frame(8, event.payload)
            except WebSocketProtocolError:
                state["frame"] = encode_client_frame(8)
            state["phase"] = "write-close"
            self._advance_websocket_receive_task(task)

    def _websocket_close_task(self, connection: _TyveraNetworkResource,
                              location: Location) -> _TyveraTask:
        try:
            frame = encode_client_frame(8, (1000).to_bytes(2, "big"))
            failure = None
        except WebSocketProtocolError as error:
            frame, failure = b"", str(error)
        return self._create_native_task(
            "Result<Bool,String>", location,
            self._advance_websocket_close_task,
            {"connection": connection, "frame": frame, "operation": None, "failure": failure,
             "phase": "write-close"},
        )

    def _advance_websocket_close_task(self, task: _TyveraTask) -> None:
        state = task.native_state
        connection = state["connection"]
        assert isinstance(connection, _TyveraNetworkResource)
        phase = str(state.get("phase", "write-close"))
        failure = state.get("failure")
        if isinstance(failure, str):
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", failure, "Result", 1)
            self._complete_task(task)
            return
        operation = state.get("operation")
        if not isinstance(operation, _TyveraTask):
            if phase == "read-close":
                operation = self._tcp_read_task(connection, 65_536, task.location)
            else:
                frame = state["frame"]
                assert isinstance(frame, bytes)
                operation = self._tcp_write_task(connection, frame, True, task.location)
            timed = self._timeout_task_for_native_wait(
                operation, _TyveraDuration(max(1, connection.operation_timeout_milliseconds)), task.location,
            )
            task.native_resources.append(timed)
            state["operation"] = timed
            self._native_wait(task, timed)
            return
        ready, value, error = self._take_native_wait(task)
        if not ready:
            self._native_wait(task, operation)
            ready, value, error = self._take_native_wait(task)
            if not ready:
                return
        if error is not None:
            self._close_resource(connection)
            message = "WebSocket close timed out" if error == "task timed out" else f"WebSocket close failed: {error}"
            task.result = _TyveraVariant("Err", message, "Result", 1)
            self._complete_task(task)
            return
        if not isinstance(value, _TyveraVariant) or value.variant != "Ok":
            self._close_resource(connection)
            message = value.payload if isinstance(value, _TyveraVariant) else "WebSocket close failed"
            task.result = _TyveraVariant("Err", f"WebSocket close failed: {message}", "Result", 1)
            self._complete_task(task)
            return

        if phase == "write-close":
            state["phase"] = "read-close"
            state["operation"] = None
            self._advance_websocket_close_task(task)
            return
        if phase == "write-pong":
            state["phase"] = "read-close"
            state["operation"] = None
            self._advance_websocket_close_task(task)
            return

        read_result = value.payload
        if not isinstance(read_result, _TyveraVariant):
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", "invalid WebSocket close read completion", "Result", 1)
            self._complete_task(task)
            return
        if read_result.variant == "None":
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", "WebSocket peer closed before completing the close handshake", "Result", 1)
            self._complete_task(task)
            return
        if read_result.variant == "Err":
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", f"WebSocket close failed: {read_result.payload}", "Result", 1)
            self._complete_task(task)
            return
        received = read_result.payload
        decoder = connection.websocket_decoder
        if not isinstance(received, _TyveraBytes) or not isinstance(decoder, WebSocketFrameDecoder):
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", "invalid WebSocket close frame data", "Result", 1)
            self._complete_task(task)
            return
        try:
            decoder.feed(received.value)
            event = decoder.next_event()
            while event is not None and event.opcode not in {8, 9}:
                event = decoder.next_event()
        except WebSocketProtocolError as protocol_error:
            self._close_resource(connection)
            task.result = _TyveraVariant("Err", str(protocol_error), "Result", 1)
            self._complete_task(task)
            return
        if event is not None and event.opcode == 8:
            self._close_resource(connection)
            task.result = _TyveraVariant("Ok", True, "Result", 1)
            self._complete_task(task)
            return
        if event is not None and event.opcode == 9:
            try:
                state["frame"] = encode_client_frame(10, event.payload)
            except WebSocketProtocolError as protocol_error:
                self._close_resource(connection)
                task.result = _TyveraVariant("Err", str(protocol_error), "Result", 1)
                self._complete_task(task)
                return
            state["phase"] = "write-pong"
            state["operation"] = None
            self._advance_websocket_close_task(task)
            return
        state["operation"] = None
        self._advance_websocket_close_task(task)

    def _start_http_server_task(self, server: _TyveraHttpServer, location: Location) -> _TyveraTask:
        task = self._create_native_task(
            "Result<Bool,String>", location, self._advance_http_server_task,
            {"server": server, "accept": None},
        )
        server.run_task = task
        return task

    def _http_client_task(
        self, url: HttpUrl, request: HttpRequest, timeout: _TyveraDuration, location: Location,
    ) -> _TyveraTask:
        state: dict[str, object] = {
            "url": url, "request": request, "timeout": timeout.milliseconds,
            "phase": "connect", "operation": None, "connection": None,
            "decoder": HttpMessageDecoder(HttpLimits(), response=True,
                                           request_method=request.method.name),
        }
        if timeout.milliseconds <= 0:
            state["failure"] = "HTTP timeout must be greater than zero"
        return self._create_native_task(
            "Result<HttpResponse,String>", location, self._advance_http_client_task, state,
        )

    def _schedule_http_client_io(self, task: _TyveraTask) -> None:
        state = task.native_state
        phase = state["phase"]
        if phase == "connect":
            url = state["url"]
            assert isinstance(url, HttpUrl)
            operation = self._tcp_connect_task(
                url.host, url.port, task.location, secure=url.scheme == "https",
            )
        elif phase == "write":
            connection = state["connection"]
            payload = state["request_bytes"]
            assert isinstance(connection, _TyveraNetworkResource) and isinstance(payload, bytes)
            operation = self._tcp_write_task(connection, payload, True, task.location)
        elif phase == "read":
            connection = state["connection"]
            assert isinstance(connection, _TyveraNetworkResource)
            operation = self._tcp_read_task(connection, 65_536, task.location)
        else:
            raise AssertionError("unknown HTTP client phase")
        timed = self._timeout_task_for_native_wait(
            operation, _TyveraDuration(int(state["timeout"])), task.location,
        )
        task.native_resources.append(timed)
        state["operation"] = timed
        self._native_wait(task, timed)

    def _advance_http_client_task(self, task: _TyveraTask) -> None:
        state = task.native_state
        failure = state.get("failure")
        if isinstance(failure, str):
            self._finish_http_client(task, _TyveraVariant("Err", failure, "Result", 1))
            return
        operation = state.get("operation")
        if not isinstance(operation, _TyveraTask):
            if state["phase"] == "write":
                request = state["request"]
                url = state["url"]
                assert isinstance(request, HttpRequest) and isinstance(url, HttpUrl)
                headers = request.headers.copy()
                try:
                    headers.set("Host", url.authority)
                    headers.set("Connection", "close")
                    request_bytes = encode_request(
                        request.method, request.target, headers, request.body,
                    )
                except (HttpProtocolError, ValueError, UnicodeEncodeError) as error:
                    self._finish_http_client(task, _TyveraVariant(
                        "Err", f"HTTP request encoding failed: {error}", "Result", 1,
                    ))
                    return
                state["request_bytes"] = request_bytes
            self._schedule_http_client_io(task)
            return

        ready, value, error = self._take_native_wait(task)
        if not ready:
            self._native_wait(task, operation)
            ready, value, error = self._take_native_wait(task)
            if not ready:
                return
        state["operation"] = None
        phase = state["phase"]
        if error is not None:
            if error == "task timed out":
                operation.state = "completed"
                operation.error = None
                message = "HTTP timeout"
            else:
                message = f"HTTP {phase} failed: {error}"
            self._finish_http_client(task, _TyveraVariant("Err", message, "Result", 1))
            return
        if not isinstance(value, _TyveraVariant):
            self._finish_http_client(task, _TyveraVariant(
                "Err", "invalid HTTP operation completion", "Result", 1,
            ))
            return
        if value.variant == "Err":
            self._finish_http_client(task, _TyveraVariant(
                "Err", f"HTTP {phase} failed: {value.payload}", "Result", 1,
            ))
            return

        result = value.payload
        if phase == "connect":
            if not isinstance(result, _TyveraNetworkResource):
                self._finish_http_client(task, _TyveraVariant(
                    "Err", "HTTP connect returned no connection", "Result", 1,
                ))
                return
            state["connection"] = result
            task.native_resources.append(result)
            state["phase"] = "write"
            self._advance_http_client_task(task)
            return
        if phase == "write":
            state["phase"] = "read"
            self._schedule_http_client_io(task)
            return

        if not isinstance(result, _TyveraVariant):
            self._finish_http_client(task, _TyveraVariant(
                "Err", "invalid HTTP response read completion", "Result", 1,
            ))
            return
        decoder = state["decoder"]
        assert isinstance(decoder, HttpMessageDecoder)
        if result.variant == "None":
            decoder.finish()
        else:
            payload = result.payload
            if not isinstance(payload, _TyveraBytes):
                self._finish_http_client(task, _TyveraVariant(
                    "Err", "invalid HTTP response byte chunk", "Result", 1,
                ))
                return
            try:
                decoder.feed(payload.value)
            except HttpProtocolError as protocol_error:
                self._finish_http_client(task, _TyveraVariant(
                    "Err", f"HTTP response parse error: {protocol_error}", "Result", 1,
                ))
                return
        try:
            response = decoder.next_message()
        except HttpProtocolError as protocol_error:
            self._finish_http_client(task, _TyveraVariant(
                "Err", f"HTTP response parse error: {protocol_error}", "Result", 1,
            ))
            return
        if isinstance(response, HttpResponse):
            if 100 <= response.status < 200 and response.status != 101:
                state["phase"] = "read"
                self._schedule_http_client_io(task)
            else:
                self._finish_http_client(task, _TyveraVariant("Ok", response, "Result", 1))
            return
        if result.variant == "None":
            self._finish_http_client(task, _TyveraVariant(
                "Err", "HTTP connection closed before a complete response", "Result", 1,
            ))
            return
        self._schedule_http_client_io(task)

    def _finish_http_client(self, task: _TyveraTask, result: _TyveraVariant) -> None:
        task.result = result
        self._complete_task(task)

    def _advance_http_server_task(self, task: _TyveraTask) -> None:
        server = task.native_state["server"]
        assert isinstance(server, _TyveraHttpServer)
        ready, value, error = self._take_native_wait(task)
        if not ready:
            if server.stopped or server.listener.closed:
                task.result = _TyveraVariant("Ok", True, "Result", 1)
                self._complete_task(task)
                return
            accept = task.native_state.get("accept")
            if not isinstance(accept, _TyveraTask):
                accept = self._tcp_accept_task(server.listener, task.location)
                task.native_state["accept"] = accept
            self._native_wait(task, accept)
            ready, value, error = self._take_native_wait(task)
            if not ready:
                return
        accept_task = task.native_state.get("accept")
        if isinstance(accept_task, _TyveraTask) and accept_task.state in {"completed", "failed", "cancelled"}:
            if accept_task in task.children:
                task.children.remove(accept_task)
            accept_task.parent = None
        task.native_state["accept"] = None
        if error is not None:
            if server.stopped or server.listener.closed:
                task.result = _TyveraVariant("Ok", True, "Result", 1)
            else:
                server.stopped = True
                if not server.listener.closed:
                    self._close_resource(server.listener)
                task.result = _TyveraVariant("Err", error, "Result", 1)
            self._complete_task(task)
            return
        if not isinstance(value, _TyveraVariant):
            server.stopped = True
            task.result = _TyveraVariant("Err", "invalid TCP accept completion", "Result", 1)
            self._complete_task(task)
            return
        if value.variant == "Err":
            if server.stopped or server.listener.closed:
                task.result = _TyveraVariant("Ok", True, "Result", 1)
            else:
                server.stopped = True
                if not server.listener.closed:
                    self._close_resource(server.listener)
                task.result = _TyveraVariant("Err", str(value.payload), "Result", 1)
            self._complete_task(task)
            return
        connection = value.payload
        if not isinstance(connection, _TyveraNetworkResource):
            server.stopped = True
            task.result = _TyveraVariant("Err", "invalid accepted TCP connection", "Result", 1)
            self._complete_task(task)
            return
        active_connections = sum(
            1 for child in task.children
            if child.native_state.get("http_connection") and self._task_is_active(child)
        )
        if active_connections >= server.limits.max_connections:
            self._close_resource(connection)
            return
        if server.stopped:
            self._close_resource(connection)
            task.result = _TyveraVariant("Ok", True, "Result", 1)
            self._complete_task(task)
            return
        self._create_native_task(
            "Void", task.location, self._advance_http_connection_task,
            {
                "connection": connection,
                "server": server,
                "router": server.router,
                "limits": server.limits,
                "timeout": server.timeout_milliseconds,
                "decoder": HttpMessageDecoder(server.limits),
                "operation": None,
                "phase": "read",
                "keep_alive": False,
                "request_method": None,
                "deadline": None,
                "http_connection": True,
            },
            parent=task,
            resources=[connection],
        )

    def _advance_http_connection_task(self, task: _TyveraTask) -> None:
        state = task.native_state
        connection = state["connection"]
        decoder = state["decoder"]
        assert isinstance(connection, _TyveraNetworkResource)
        assert isinstance(decoder, HttpMessageDecoder)
        operation = state.get("operation")
        if isinstance(operation, _TyveraTask):
            ready, value, error = self._take_native_wait(task)
            if not ready:
                self._native_wait(task, operation)
                ready, value, error = self._take_native_wait(task)
                if not ready:
                    return
            state["operation"] = None
            if error is not None:
                if state["phase"] == "read" and error == "task timed out":
                    if isinstance(operation, _TyveraTask):
                        operation.state = "completed"
                        operation.error = None
                    self._http_connection_write_response(task, text_response(408, "Request Timeout"), close=True)
                    return
                if error == "task timed out" and isinstance(operation, _TyveraTask):
                    operation.state = "completed"
                    operation.error = None
                self._complete_task(task)
                return
            if not isinstance(value, _TyveraVariant) or value.variant != "Ok":
                self._complete_task(task)
                return
            result = value.payload
            if state["phase"] == "read":
                if not isinstance(result, _TyveraVariant):
                    self._complete_task(task)
                    return
                if result.variant == "None":
                    self._complete_task(task)
                    return
                payload = result.payload
                if not isinstance(payload, _TyveraBytes):
                    self._complete_task(task)
                    return
                try:
                    decoder.feed(payload.value)
                except HttpProtocolError as protocol_error:
                    self._http_protocol_error_response(task, protocol_error)
                    return
                request, protocol_error = self._http_take_request(decoder)
                if protocol_error is not None:
                    self._http_protocol_error_response(task, protocol_error)
                    return
                if request is None:
                    self._http_connection_read(task)
                    return
                self._http_dispatch_request(task, request)
                return
            if state["phase"] == "write":
                if state.get("keep_alive") and not state.get("closing"):
                    state["deadline"] = None
                    state["phase"] = "read"
                    request, protocol_error = self._http_take_request(decoder)
                    if protocol_error is not None:
                        self._http_protocol_error_response(task, protocol_error)
                    elif request is not None:
                        self._http_dispatch_request(task, request)
                    else:
                        self._http_connection_read(task)
                    return
                self._complete_task(task)
                return

        if state["phase"] == "read":
            request, protocol_error = self._http_take_request(decoder)
            if protocol_error is not None:
                self._http_protocol_error_response(task, protocol_error)
            elif request is not None:
                self._http_dispatch_request(task, request)
            else:
                self._http_connection_read(task)

    @staticmethod
    def _http_take_request(decoder: HttpMessageDecoder) -> tuple[HttpRequest | None, HttpProtocolError | None]:
        try:
            message = decoder.next_message()
        except HttpProtocolError as error:
            return None, error
        return (message if isinstance(message, HttpRequest) else None), None

    def _http_connection_read(self, task: _TyveraTask) -> None:
        state = task.native_state
        connection = state["connection"]
        deadline = state.get("deadline")
        if deadline is None:
            deadline = self._clock() + int(state["timeout"]) / 1000.0
            state["deadline"] = deadline
        remaining = int((float(deadline) - self._clock()) * 1000)
        if remaining <= 0:
            self._http_connection_write_response(task, text_response(408, "Request Timeout"), close=True)
            return
        read = self._tcp_read_task(connection, 65_536, task.location)
        state["phase"] = "read"
        timed = self._timeout_task_for_native_wait(read, _TyveraDuration(remaining), task.location)
        task.native_resources.append(timed)
        state["operation"] = timed
        self._native_wait(task, timed)

    def _http_dispatch_request(self, task: _TyveraTask, request: HttpRequest) -> None:
        state = task.native_state
        connection = state["connection"]
        assert isinstance(connection, _TyveraNetworkResource)
        try:
            request._remote_address = self._socket_address(
                connection.stream.getpeername(), connection.stream.family,
            )
        except (OSError, ValueError):
            request._remote_address = None
        router = state["router"]
        assert isinstance(router, HttpRouter)
        keep_alive = request.is_keep_alive()
        server = state["server"]
        if isinstance(server, _TyveraHttpServer) and server.stopped:
            keep_alive = False
        try:
            response = router.dispatch(request)
            encoded = encode_response(
                response, keep_alive=keep_alive, request_method=request.method.name,
                limits=state["limits"],
            )
        except (HttpProtocolError, ValueError, UnicodeEncodeError):
            response = text_response(500, "Internal Server Error")
            encoded = encode_response(response, keep_alive=False)
            keep_alive = False
        state["request_method"] = request.method.name
        state["keep_alive"] = keep_alive
        state["closing"] = not keep_alive
        self._http_connection_write(task, encoded)

    def _http_protocol_error_response(self, task: _TyveraTask, error: HttpProtocolError) -> None:
        message = str(error)
        status = 414 if "request line" in message or "start line" in message else (
            431 if "header" in message else 413 if "size limit" in message else 400
        )
        response = text_response(status, message)
        self._http_connection_write_response(task, response, close=True)

    def _http_connection_write_response(self, task: _TyveraTask, response: HttpResponse, *, close: bool) -> None:
        state = task.native_state
        try:
            payload = encode_response(
                response, keep_alive=not close,
                request_method=state.get("request_method"),
            )
        except (HttpProtocolError, ValueError, UnicodeEncodeError):
            self._complete_task(task)
            return
        state["keep_alive"] = not close
        state["closing"] = close
        self._http_connection_write(task, payload)

    def _http_connection_write(self, task: _TyveraTask, payload: bytes) -> None:
        state = task.native_state
        connection = state["connection"]
        write = self._tcp_write_task(connection, payload, True, task.location)
        state["phase"] = "write"
        timed = self._timeout_task_for_native_wait(
            write, _TyveraDuration(int(state["timeout"])), task.location,
        )
        task.native_resources.append(timed)
        state["operation"] = timed
        self._native_wait(task, timed)

    def _cancel_socket_wait(self, resource: _TyveraNetworkResource, event: int,
                            callback: Callable[[], None], direction: str,
                            task: _TyveraTask) -> None:
        self._get_io_backend().unwatch(resource.stream, event, callback)
        if direction == "read":
            resource.pending_read = False
            if resource.pending_read_task is task:
                resource.pending_read_task = None
        else:
            resource.pending_write = False
            if resource.pending_write_task is task:
                resource.pending_write_task = None

    def _async_open_text_task(self, path: Path, mode: str, location: Location) -> _TyveraTask:
        task = self._create_io_task("Result<TextFile,String>", location)
        if mode not in {"read", "write", "append"}:
            self._complete_io_task(task, _TyveraVariant("Err", "text file mode must be 'read', 'write', or 'append'", "Result", 1))
            return task
        if self._is_secret_storage_path(path):
            self._complete_io_task(task, _TyveraVariant("Err", "secret file can only be accessed through the secrets provider", "Result", 1))
            return task
        python_mode = {"read": "r", "write": "w", "append": "a"}[mode]

        def opened(stream: object, error: BaseException | None) -> None:
            if error is not None or not hasattr(stream, "read"):
                self._complete_io_task(task, _TyveraVariant("Err", "cannot open text file", "Result", 1))
                return
            resource = _TyveraTextFile(stream, mode, location=location)
            self._complete_io_task(task, _TyveraVariant("Ok", resource, "Result", 1))

        try:
            future = self._get_io_backend().submit(
                lambda: path.open(python_mode, encoding="utf-8", newline=""), opened,
            )
        except RuntimeError as error:
            self._complete_io_task(task, _TyveraVariant("Err", self._io_runtime_error(error), "Result", 1))
        else:
            task.io_cancel = future.cancel
        return task

    def _async_text_file_task(
        self, resource: _TyveraTextFile, method: str, arguments: list[object], location: Location,
    ) -> _TyveraTask:
        result_type = "Result<Bool,String>" if method in {"writeAsync", "appendAsync", "flushAsync", "closeAsync"} else "Result<String,String>"
        task = self._create_io_task(result_type, location)
        task.io_resource = resource
        if resource.closed:
            self._complete_io_task(task, _TyveraVariant("Err", "text file is closed", "Result", 1))
            return task
        if resource.pending_operations:
            self._complete_io_task(task, _TyveraVariant("Err", "text file already has an asynchronous operation pending", "Result", 1))
            return task
        if method == "readAllAsync" and resource.mode != "read":
            self._complete_io_task(task, _TyveraVariant("Err", "text file is not open for reading", "Result", 1))
            return task
        if method == "writeAsync" and resource.mode == "read":
            self._complete_io_task(task, _TyveraVariant("Err", "text file is not open for writing", "Result", 1))
            return task
        if method == "appendAsync" and resource.mode != "append":
            self._complete_io_task(task, _TyveraVariant("Err", "text file is not open for appending", "Result", 1))
            return task
        if method == "closeAsync" and resource.closed:
            self._complete_io_task(task, _TyveraVariant("Ok", False, "Result", 1))
            return task

        def operation() -> object:
            if method == "readAllAsync":
                return resource.stream.read()
            if method in {"writeAsync", "appendAsync"}:
                resource.stream.write(arguments[0])
                return True
            if method == "flushAsync":
                resource.stream.flush()
                return True
            if method == "closeAsync":
                resource.stream.close()
                return True
            raise AssertionError(f"unknown async text operation {method}")

        resource.pending_operations += 1
        if method == "closeAsync":
            resource.close_requested = True

        def completed(value: object, error: BaseException | None) -> None:
            resource.pending_operations -= 1
            if resource.close_requested and resource.pending_operations == 0:
                resource.closed = True
                try:
                    if not resource.stream.closed:
                        resource.stream.close()
                except OSError:
                    error = error or OSError("close failed")
            if error is not None:
                label = "cannot read text file" if method == "readAllAsync" else "cannot close text file" if method == "closeAsync" else "cannot write text file"
                self._complete_io_task(task, _TyveraVariant("Err", label, "Result", 1))
                return
            if method == "closeAsync":
                resource.closed = True
            self._complete_io_task(task, _TyveraVariant("Ok", value, "Result", 1))

        try:
            future = self._get_io_backend().submit(operation, completed)
        except RuntimeError as error:
            resource.pending_operations -= 1
            self._complete_io_task(task, _TyveraVariant("Err", self._io_runtime_error(error), "Result", 1))
        else:
            task.io_cancel = future.cancel
        return task

    def _create_timeout_task(
        self, target: _TyveraTask, duration: _TyveraDuration, location: Location,
    ) -> _TyveraTask:
        task = _TyveraTask(
            None, None, location, state="timed_wait", result_type=target.result_type,
            due_at=self._clock() + duration.milliseconds / 1000.0,
            timeout_target=target, parent=self._current_task, owner_actor=self._current_actor,
        )
        if task.parent is not None:
            task.parent.children.append(task)
        self._tasks.append(task)
        return task

    def _run_tasks(self, location: Location, depth: int) -> _TyveraVariant:
        if self._runtime_state != "running":
            raise Diagnostic(location, "runtime", "runTasks requires an active interpreter runtime")
        if self._current_actor is not None:
            raise Diagnostic(location, "runtime", "runTasks cannot be called from an actor message handler")
        if self._running_tasks:
            raise Diagnostic(location, "runtime", "runTasks cannot be called recursively")

        self._running_tasks = True
        self._scheduler_state = "running"
        steps = 0
        scheduler_steps = 0
        failure: str | None = None
        try:
            while True:
                self._check_shutdown(location)
                restart_failure = self._settle_supervisor_restarts(wait=False)
                if restart_failure is not None:
                    failure = failure or restart_failure
                self._promote_sleeping_tasks()
                self._settle_timed_tasks()
                self._wake_waiting_tasks()
                self._settle_joining_tasks()
                if self._ready_tasks:
                    if scheduler_steps >= _MAX_TASK_STEPS:
                        failure = (
                            f"task scheduler reached the {_MAX_TASK_STEPS}-step safety limit; "
                            "cooperative work remains queued"
                        )
                        break
                    task = self._ready_tasks.popleft()
                    if task.state != "runnable":
                        continue
                    task.state = "running"
                    previous_task = self._current_task
                    self._current_task = task
                    try:
                        if task.native_step is not None:
                            task.native_step(task)
                        else:
                            self._advance_async_task(task)
                    except _ReturnSignal as signal:
                        task.result = signal.value
                        self._complete_task(task)
                    except ShutdownDiagnostic:
                        raise
                    except Diagnostic as error:
                        self._fail_task(task, error.render())
                    except _LoopSignal as signal:
                        self._handle_async_loop_control(task, signal)
                    finally:
                        self._current_task = previous_task
                    if task.state == "running" and task.native_step is not None:
                        task.state = "runnable"
                        self._ready_tasks.append(task)
                    elif task.state == "running":
                        if task.frames:
                            task.state = "runnable"
                            self._ready_tasks.append(task)
                        else:
                            self._complete_task(task)
                    steps += 1
                    scheduler_steps += 1
                    self._settle_timed_tasks()
                    self._wake_waiting_tasks()
                    self._settle_joining_tasks()
                    if self._has_waiting_actor_reply() and self._ready_actors:
                        actor_result = self._run_actors(
                            location, depth + 1, delivery_limit=1, fail_on_limit=False,
                        )
                        if actor_result.variant == "Err" and failure is None:
                            failure = str(actor_result.payload)
                        scheduler_steps += int(actor_result.payload) if actor_result.variant == "Ok" else 1
                        self._promote_due_timers()
                        self._wake_waiting_tasks()
                        self._settle_joining_tasks()
                    continue

                active = [task for task in self._tasks
                          if task.state in {"runnable", "running", "waiting", "sleeping", "joining", "timed_wait", "io_wait"}]
                if not active:
                    break

                if self._has_waiting_actor_reply(active) and self._ready_actors:
                    if scheduler_steps >= _MAX_TASK_STEPS:
                        failure = (
                            f"task scheduler reached the {_MAX_TASK_STEPS}-step safety limit; "
                            "cooperative work remains queued"
                        )
                        break
                    actor_result = self._run_actors(
                        location, depth + 1, delivery_limit=1, fail_on_limit=False,
                    )
                    if actor_result.variant == "Err":
                        failure = failure or str(actor_result.payload)
                        scheduler_steps += 1
                    else:
                        scheduler_steps += int(actor_result.payload)
                    self._promote_due_timers()
                    self._wake_waiting_tasks()
                    self._settle_joining_tasks()
                    if self._ready_tasks or (self._ready_actors and self._has_waiting_actor_reply()):
                        continue

                deadline = self._next_task_deadline()
                if self._io_backend is not None and self._io_backend.has_pending:
                    timeout = None if deadline is None else max(0.0, deadline - self._clock())
                    self._io_backend.wait(timeout)
                    continue
                if deadline is not None:
                    delay = max(0.0, deadline - self._clock())
                    if delay > 0:
                        self._sleep(delay)
                    continue

                waiting_on_reply = self._has_waiting_actor_reply(active)
                if waiting_on_reply:
                    failure = failure or "task scheduler has an unresolved actor reply with no runnable work or deadline"
                else:
                    failure = failure or "task scheduler is deadlocked: pending tasks have no runnable or timed work"
                break

            failed_task = next((task for task in self._tasks if task.state == "failed"), None)
            if failed_task is not None:
                failure = failure or failed_task.error or "async task failed"
            if failure is not None:
                return _TyveraVariant("Err", failure)
            return _TyveraVariant("Ok", steps)
        finally:
            self._running_tasks = False
            self._scheduler_state = "idle" if self._runtime_state == "running" else "closed"

    def _has_waiting_actor_reply(self, tasks: list[_TyveraTask] | None = None) -> bool:
        candidates = self._tasks if tasks is None else tasks
        return any(
            task.state == "waiting" and isinstance(task.wait_target, _TyveraReplyReceiver)
            and self._await_state(task.wait_target)[0] == "pending"
            for task in candidates
        )

    def _advance_async_task(self, task: _TyveraTask) -> None:
        if not task.frames:
            return
        frame = task.frames[-1]
        if isinstance(frame, _AsyncResourceFrame):
            task.frames.pop()
            cleanup_error = self._close_resource(frame.resource)
            if cleanup_error is not None:
                self._fail_task(task, self._resource_cleanup_diagnostic(frame.location, cleanup_error))
            return
        if isinstance(frame, _AsyncLoopFrame):
            statement = frame.statement
            if isinstance(statement, WhileStatement):
                if self._evaluate(statement.condition, frame.variables, task.depth):
                    task.frames.append(_AsyncBlockFrame(statement.body, frame.variables.new_child()))
                else:
                    task.frames.pop()
                return
            if frame.items is None:
                raise AssertionError("for-loop continuation has no snapshotted iterable")
            if frame.index >= len(frame.items):
                task.frames.pop()
                return
            scope = frame.variables.new_child()
            scope[statement.name] = frame.items[frame.index]
            frame.index += 1
            task.frames.append(_AsyncBlockFrame(statement.body, scope))
            return

        if frame.index >= len(frame.body):
            task.frames.pop()
            cleanup_errors = self._cleanup_owned_scope(frame.variables.maps[0])
            if cleanup_errors:
                diagnostics = [self._resource_cleanup_diagnostic(location, message)
                               for location, message in cleanup_errors]
                self._fail_task(task, "; ".join(diagnostics))
            return
        statement = frame.body[frame.index]
        if isinstance(statement, AwaitStatement):
            target = self._evaluate(statement.value, frame.variables, task.depth)
            if isinstance(target, _TyveraTaskGroup) and target.owner is not task:
                raise Diagnostic(statement.location, "runtime", "TaskGroup may only be awaited by its creating task")
            state, error = self._await_state(target)
            if state == "ready":
                self._complete_task_await(task, statement, target, frame.variables)
                frame.index += 1
            elif state == "failed":
                self._fail_task(task, error or "awaited operation failed")
            else:
                task.state = "waiting"
                task.wait_target = target
                task.wait_frame = frame
                task.wait_statement = statement
            return
        if isinstance(statement, Block):
            frame.index += 1
            task.frames.append(_AsyncBlockFrame(statement.body, frame.variables.new_child()))
            return
        if isinstance(statement, UsingStatement):
            acquisition = self._evaluate(statement.resource, frame.variables, task.depth)
            if not isinstance(acquisition, _TyveraVariant):
                raise AssertionError("checked using acquisition did not return Result")
            frame.index += 1
            if acquisition.variant == "Err":
                if statement.error_name is None:
                    raise Diagnostic(statement.location, "runtime", str(acquisition.payload))
                scope = frame.variables.new_child()
                scope[statement.error_name] = acquisition.payload
                task.frames.append(_AsyncBlockFrame(statement.else_body, scope))
            elif acquisition.variant == "Ok":
                resource = acquisition.payload
                if not isinstance(resource, (_TyveraTextFile, _TyveraNetworkResource)):
                    raise AssertionError("checked using acquisition is not a Resource")
                resource.scoped = True
                scope = frame.variables.new_child()
                scope[statement.name] = resource
                task.frames.append(_AsyncResourceFrame(resource, statement.location))
                task.frames.append(_AsyncBlockFrame(statement.body, scope))
            else:
                raise AssertionError("checked using acquisition returned neither Ok nor Err")
            return
        if isinstance(statement, IfStatement):
            body = statement.then_body if self._evaluate(statement.condition, frame.variables, task.depth) else statement.else_body
            frame.index += 1
            task.frames.append(_AsyncBlockFrame(body, frame.variables.new_child()))
            return
        if isinstance(statement, WhileStatement):
            frame.index += 1
            task.frames.append(_AsyncLoopFrame(statement, frame.variables, task.depth))
            return
        if isinstance(statement, ForStatement):
            iterable = self._evaluate(statement.iterable, frame.variables, task.depth)
            if isinstance(iterable, _TyveraList):
                items = tuple(iterable.values)
            elif isinstance(iterable, _TyveraMap):
                    items = tuple(iterable.keys())
            else:
                raise AssertionError("checked async for-loop value is not iterable")
            frame.index += 1
            task.frames.append(_AsyncLoopFrame(statement, frame.variables, task.depth, items))
            return
        if isinstance(statement, MatchStatement):
            value = self._evaluate(statement.value, frame.variables, task.depth)
            frame.index += 1
            for arm in statement.arms:
                matched, binding = self._matches(arm.pattern, value)
                if not matched:
                    continue
                scope = frame.variables.new_child()
                scope.update(binding)
                task.frames.append(_AsyncBlockFrame(arm.body, scope))
                break
            return

        self._execute_block_inner([statement], frame.variables, task.depth)
        frame.index += 1

    def _handle_async_loop_control(self, task: _TyveraTask, signal: _LoopSignal) -> None:
        loop_index = next(
            (index for index in range(len(task.frames) - 1, -1, -1)
             if isinstance(task.frames[index], _AsyncLoopFrame)),
            None,
        )
        if loop_index is None:
            self._fail_task(task, f"'{signal.kind}' escaped its loop in async task")
            return
        start = loop_index + (1 if signal.kind == "continue" else 0)
        discarded = task.frames[start:]
        del task.frames[start:]
        cleanup_errors = self._cleanup_resource_frames(discarded)
        if cleanup_errors:
            self._fail_task(task, "; ".join(cleanup_errors))

    def _promote_sleeping_tasks(self) -> None:
        now = self._clock()
        for task in self._tasks:
            if task.state == "sleeping" and task.due_at is not None and now + 1e-12 >= task.due_at:
                task.state = "completed"
        self._wake_waiting_tasks()

    def _settle_timed_tasks(self) -> None:
        now = self._clock()
        for task in self._tasks:
            if task.state != "timed_wait" or task.timeout_target is None:
                continue
            target = task.timeout_target
            if target.state == "completed":
                task.result = target.result
                if self._resource_handles(target.result):
                    target.result = None
                task.timeout_target = None
                task.due_at = None
                task.state = "completed"
            elif target.state == "failed":
                task.timeout_target = None
                task.due_at = None
                self._fail_task(task, target.error or "timed task failed")
            elif target.state == "cancelled":
                task.timeout_target = None
                task.due_at = None
                self._fail_task(task, "timed task was cancelled")
            elif task.due_at is not None and now + 1e-12 >= task.due_at:
                task.timeout_target = None
                task.due_at = None
                self._cancel_task(target)
                self._fail_task(task, "task timed out")

    def _await_state(self, target: object) -> tuple[str, str | None]:
        if isinstance(target, _TyveraTask):
            if target.state == "completed":
                return "ready", None
            if target.state == "failed":
                return "failed", target.error or "awaited task failed"
            if target.state == "cancelled":
                return "failed", "awaited task was cancelled"
            return "pending", None
        if isinstance(target, _TyveraTaskGroup):
            failed_task = next((task for task in target.tasks if task.state == "failed"), None)
            if failed_task is not None:
                return "failed", failed_task.error or "task group member failed"
            if all(task.state in {"completed", "failed", "cancelled"} for task in target.tasks):
                return "ready", None
            return "pending", None
        if isinstance(target, _TyveraReplyReceiver):
            cell = target.cell
            self._refresh_reply(cell)
            if cell.state == "sent":
                return "ready", None
            if cell.state == "pending":
                return "pending", None
            return "failed", cell.error or (
                "reply was already consumed" if cell.state == "consumed" else "actor request did not complete"
            )
        raise AssertionError("type-checked await target is not awaitable")

    def _wake_waiting_tasks(self) -> None:
        for task in self._tasks:
            if task.state != "waiting" or task.wait_target is None:
                continue
            target = task.wait_target
            state, error = self._await_state(target)
            if state == "pending":
                continue
            task.wait_target = None
            if task.native_step is not None:
                if state == "failed":
                    task.native_wait_error = error or "HTTP operation failed"
                else:
                    task.native_wait_result = target.result
                    if self._resource_handles(target.result):
                        target.result = None
                task.state = "runnable"
                self._ready_tasks.append(task)
                continue
            frame = task.wait_frame
            statement = task.wait_statement
            task.wait_frame = None
            task.wait_statement = None
            if state == "failed":
                self._fail_task(task, error or "awaited operation failed")
            else:
                if frame is None or statement is None:
                    raise AssertionError("waiting task has no suspended await continuation")
                self._complete_task_await(task, statement, target, frame.variables)
                frame.index += 1
                task.state = "runnable"
                self._ready_tasks.append(task)

    def _complete_task_await(
        self, task: _TyveraTask, statement: AwaitStatement, target: object,
        variables: ChainMap[str, object],
    ) -> None:
        if statement.binding_name is None:
            return
        if isinstance(target, _TyveraTask):
            value = target.result
            if self._resource_handles(value):
                target.result = None
        elif isinstance(target, _TyveraTaskGroup):
            return
        elif isinstance(target, _TyveraReplyReceiver):
            cell = target.cell
            if cell.state != "sent":
                raise AssertionError("type-checked reply await is not ready")
            value = cell.value
            cell.value = None
            cell.state = "consumed"
        else:
            raise AssertionError("type-checked await target is not awaitable")
        variables[statement.binding_name] = value

    def _complete_task(self, task: _TyveraTask) -> None:
        cleanup_errors = self._release_task_frames(task)
        if cleanup_errors:
            self._fail_task(task, "; ".join(cleanup_errors))
            return
        task.wait_target = None
        task.wait_frame = None
        task.wait_statement = None
        if any(child.state in {"runnable", "running", "waiting", "sleeping", "joining", "timed_wait", "io_wait"}
               for child in task.children):
            task.state = "joining"
        else:
            task.state = "completed"
        self._forget_http_connection_child(task)

    @staticmethod
    def _forget_http_connection_child(task: _TyveraTask) -> None:
        if not task.native_state.get("http_connection") or task.state != "completed":
            return
        parent = task.parent
        if parent is not None and task in parent.children:
            parent.children.remove(task)
        task.parent = None

    def _settle_joining_tasks(self) -> None:
        for task in self._tasks:
            if task.state != "joining":
                continue
            failed_child = next((child for child in task.children if child.state == "failed"), None)
            if failed_child is not None:
                self._fail_task(task, f"owned child task failed: {failed_child.error or 'async task failed'}")
            elif not any(child.state in {"runnable", "running", "waiting", "sleeping", "joining", "timed_wait", "io_wait"}
                         for child in task.children):
                task.state = "completed"
                self._forget_http_connection_child(task)

    def _cancel_task(self, task: _TyveraTask) -> None:
        if task.state in {"completed", "failed", "cancelled"}:
            return
        task.state = "cancelled"
        if task.io_cancel is not None:
            cancel, task.io_cancel = task.io_cancel, None
            cancel()
        task.wait_target = None
        task.wait_frame = None
        task.wait_statement = None
        cleanup_errors = self._release_task_frames(task)
        if cleanup_errors:
            task.error = "; ".join(cleanup_errors)
        if task.timeout_target is not None:
            self._cancel_task(task.timeout_target)
            task.timeout_target = None
        for child in task.children:
            self._cancel_task(child)
        self._cleanup_task_result(task)

    def _fail_task(self, task: _TyveraTask, error: str) -> None:
        if task.state in {"completed", "failed", "cancelled"}:
            return
        task.state = "failed"
        task.wait_target = None
        task.wait_frame = None
        task.wait_statement = None
        cleanup_errors = self._release_task_frames(task)
        if cleanup_errors:
            error = f"{error}; resource cleanup also failed: {'; '.join(cleanup_errors)}"
        task.error = error
        self._cleanup_task_result(task)
        for child in task.children:
            self._cancel_task(child)
        if task.parent is not None:
            self._fail_task(task.parent, f"owned child task failed: {error}")

    def _next_task_deadline(self) -> float | None:
        deadlines = [
            task.due_at for task in self._tasks
            if task.state in {"sleeping", "timed_wait"} and task.due_at is not None
        ]
        waiting_for_reply = any(
            task.state == "waiting" and isinstance(task.wait_target, _TyveraReplyReceiver)
            and self._await_state(task.wait_target)[0] == "pending"
            for task in self._tasks
        )
        if waiting_for_reply:
            deadlines.extend(
                timer.started_at + timer.duration_milliseconds / 1000.0
                for timer in self._scheduled_timers
                if not timer.cancelled and not timer.delivered and timer.scheduled_actor is not None
            )
        for task in self._tasks:
            target = task.wait_target
            if task.state != "waiting" or not isinstance(target, _TyveraReplyReceiver):
                continue
            cell = target.cell
            if cell.state == "pending" and cell.started_at is not None and cell.timeout_milliseconds is not None:
                deadlines.append(cell.started_at + cell.timeout_milliseconds / 1000.0)
        deadlines.extend(pending.due_at for pending in self._pending_supervisor_restarts)
        return min(deadlines) if deadlines else None

    def _restart_supervised_actor(
        self, actor: _TyveraActor, failure: str, call_location: Location, depth: int,
    ) -> str | None:
        self._fail_pending_replies(actor, failure)
        actor.mailbox.clear()
        supervisor = actor.supervisor
        if supervisor is None:
            return failure
        if supervisor.status != "alive":
            actor.status = "stopped"
            return failure
        if actor.restart_policy is RestartPolicy.TEMPORARY:
            actor.status = "stopped"
            return failure
        return self._schedule_supervisor_restart(supervisor, actor, failure, call_location, depth)

    def _restart_supervised_supervisor(
        self, child: _TyveraSupervisor, failure: str, call_location: Location, depth: int,
    ) -> str | None:
        parent = child.parent
        if parent is None:
            return failure
        if parent.status != "alive":
            return failure
        if child.restart_policy is RestartPolicy.TEMPORARY:
            self._stop_supervisor_tree(child, "temporary supervisor failed")
            return failure
        return self._schedule_supervisor_restart(parent, child, failure, call_location, depth)

    def _stop_supervised_actor(self, actor: _TyveraActor, location: Location, depth: int) -> None:
        if actor.status != "alive":
            return
        self._stop_actor(actor)
        supervisor = actor.supervisor
        if supervisor is None or supervisor.status != "alive":
            return
        if actor.restart_policy is RestartPolicy.PERMANENT:
            failure = self._schedule_supervisor_restart(
                supervisor, actor, "actor stopped normally", location, depth,
            )
            if failure is not None:
                raise Diagnostic(location, "runtime", failure)

    def _stop_supervised_supervisor(
        self, supervisor: _TyveraSupervisor, location: Location, depth: int,
    ) -> None:
        if supervisor.status != "alive":
            return
        self._stop_supervisor_tree(supervisor, "supervisor stopped")
        parent = supervisor.parent
        if parent is None or parent.status != "alive":
            return
        if supervisor.restart_policy is RestartPolicy.PERMANENT:
            failure = self._schedule_supervisor_restart(
                parent, supervisor, "nested supervisor stopped normally", location, depth,
            )
            if failure is not None:
                raise Diagnostic(location, "runtime", failure)

    def _enqueue_actor_message(
        self, actor: _TyveraActor, method: str, arguments: list[object], location: Location,
    ) -> None:
        if actor.status != "alive":
            name = actor.declaration.name if actor.declaration is not None else actor.instance.class_name
            dead_letter = RuntimeEvent(
                RuntimeEventKind.DEAD_LETTER, f"{name}#{actor.identity}",
                None if actor.supervisor is None else actor.supervisor.strategy,
                reason=CrashReason(CrashKind.CHILD_FAILURE,
                                   f"message '{method}' rejected while actor was {actor.status}",
                                   location.filename, location.line, location.column),
            )
            self._dead_letters.append(dead_letter)
            self._emit_runtime_event(dead_letter)
            raise Diagnostic(location, "runtime", f"cannot send a message to a {actor.status} actor")
        if len(actor.mailbox) >= _MAX_ACTOR_MAILBOX:
            raise Diagnostic(location, "runtime", f"actor mailbox capacity ({_MAX_ACTOR_MAILBOX}) exceeded")
        for argument in arguments:
            if isinstance(argument, _TyveraReplySender):
                actor.pending_replies.add(argument.cell)
        actor.mailbox.append(_ActorMessage(method, arguments, location))
        if not actor.queued:
            actor.queued = True
            self._ready_actors.append(actor)

    def _run_actors(
        self, location: Location, depth: int, *, delivery_limit: int = _MAX_ACTOR_DELIVERIES,
        fail_on_limit: bool = True,
    ) -> _TyveraVariant:
        if self._current_actor is not None:
            raise Diagnostic(location, "runtime", "runActors cannot be called from an actor message handler")
        processed = 0
        first_crash: str | None = None
        self._promote_due_timers()
        while processed < delivery_limit:
            self._check_shutdown(location)
            pending_failure = self._settle_supervisor_restarts(wait=False)
            if pending_failure is not None and first_crash is None:
                first_crash = pending_failure
            if not self._ready_actors:
                if fail_on_limit and self._pending_supervisor_restarts:
                    pending_failure = self._settle_supervisor_restarts(wait=True)
                    if pending_failure is not None and first_crash is None:
                        first_crash = pending_failure
                    if self._ready_actors:
                        continue
                break
            actor = self._ready_actors.popleft()
            actor.queued = False
            if actor.status != "alive" or not actor.mailbox:
                continue
            message = actor.mailbox.popleft()
            processed += 1
            declaration = self.actors[actor.instance.class_name]
            method = next(handler for handler in declaration.handlers if handler.name == message.method)
            self._current_actor = actor
            try:
                self._call_method(method, actor.instance, message.arguments, message.location, depth + 1)
            except ShutdownDiagnostic:
                raise
            except Diagnostic as error:
                actor.status = "crashed"
                self._emit_runtime_event(RuntimeEvent(
                    RuntimeEventKind.ACTOR_CRASHED, f"{declaration.name}#{actor.identity}",
                    None if actor.supervisor is None else actor.supervisor.strategy,
                    reason=CrashReason.runtime_error(error.message, error.location.filename,
                                                     error.location.line, error.location.column),
                ))
                failure = self._restart_supervised_actor(
                    actor, self._format_actor_failure(declaration, error), message.location, depth + 1,
                )
                if failure is not None and first_crash is None:
                    first_crash = failure
            except _LoopSignal as signal:
                actor.status = "crashed"
                self._emit_runtime_event(RuntimeEvent(
                    RuntimeEventKind.ACTOR_CRASHED, f"{declaration.name}#{actor.identity}",
                    None if actor.supervisor is None else actor.supervisor.strategy,
                    reason=CrashReason(CrashKind.RUNTIME_ERROR,
                                       f"'{signal.kind}' escaped actor loop",
                                       message.location.filename, message.location.line,
                                       message.location.column),
                ))
                failure = self._restart_supervised_actor(
                    actor, f"actor '{declaration.name}' crashed: '{signal.kind}' escaped its loop",
                    message.location, depth + 1,
                )
                if failure is not None and first_crash is None:
                    first_crash = failure
            finally:
                self._current_actor = None
            actor.pending_replies = {cell for cell in actor.pending_replies if cell.state == "pending"}
            self._promote_due_timers()
            pending_failure = self._settle_supervisor_restarts(wait=False)
            if pending_failure is not None and first_crash is None:
                first_crash = pending_failure
            if actor.status == "alive" and actor.mailbox and not actor.queued:
                actor.queued = True
                self._ready_actors.append(actor)
        if fail_on_limit and self._ready_actors and processed >= delivery_limit:
            if first_crash is None:
                first_crash = f"actor scheduler reached the {delivery_limit}-message safety limit; messages remain queued"
        if first_crash is not None:
            return _TyveraVariant("Err", first_crash)
        return _TyveraVariant("Ok", processed)

    def _promote_due_timers(self) -> None:
        for timer in self._scheduled_timers:
            if timer.cancelled or timer.delivered or timer.scheduled_actor is None:
                continue
            if timer.clock() + 1e-12 < timer.started_at + timer.duration_milliseconds / 1000.0:
                continue
            timer.delivered = True
            method = timer.scheduled_method
            location = timer.scheduled_location
            if method is None or location is None:
                raise AssertionError("scheduled actor timer is missing its message")
            self._enqueue_actor_message(
                timer.scheduled_actor, method, timer.scheduled_arguments, location,
            )
        self._prune_scheduled_timers()

    def _call_method(
        self, method: Method, receiver: _TyveraInstance, arguments: list[object],
        call_location: Location, depth: int,
    ) -> object:
        if depth >= self.max_call_depth:
            error = Diagnostic(call_location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
            self._cleanup_failed_argument_transfer(arguments, error)
            raise error
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
        if isinstance(value, _TyveraDuration) and not INT_MIN <= value.milliseconds <= INT_MAX:
            raise Diagnostic(location, "runtime", "Duration overflow: result is outside signed 64-bit milliseconds")
        if isinstance(value, float) and not math.isfinite(value):
            raise Diagnostic(location, "runtime", "Float overflow: result is outside the supported range")

    @staticmethod
    def _make_duration(milliseconds: object, location: Location) -> _TyveraDuration:
        if type(milliseconds) is not int:
            raise AssertionError("checked Duration input is not Int")
        if not INT_MIN <= milliseconds <= INT_MAX:
            raise Diagnostic(location, "runtime", "Duration overflow: value is outside signed 64-bit milliseconds")
        return _TyveraDuration(milliseconds)

    @staticmethod
    def _refresh_reply(cell: _TyveraReplyCell) -> None:
        if (cell.state == "pending" and cell.started_at is not None
                and cell.timeout_milliseconds is not None and cell.clock is not None
                and cell.clock() + 1e-12 >= cell.started_at + cell.timeout_milliseconds / 1000.0):
            cell.state = "timed_out"
            cell.error = "request timed out"

    @classmethod
    def _send_reply(cls, cell: _TyveraReplyCell, value: object) -> bool:
        cls._refresh_reply(cell)
        if cell.state != "pending":
            return False
        cell.state = "sent"
        cell.value = value
        return True

    @staticmethod
    def _apply_binary(operator: str, left: object, right: object) -> object:
        if operator == "and":
            return bool(left and right)
        if operator == "or":
            return bool(left or right)
        if operator == "+":
            if isinstance(left, _TyveraDuration) and isinstance(right, _TyveraDuration):
                return _TyveraDuration(left.milliseconds + right.milliseconds)
            return left + right  # type: ignore[operator]
        if operator == "-":
            if isinstance(left, _TyveraDuration) and isinstance(right, _TyveraDuration):
                return _TyveraDuration(left.milliseconds - right.milliseconds)
            return left - right  # type: ignore[operator]
        if operator == "*":
            return left * right  # type: ignore[operator]
        if operator == "/":
            return left / right  # type: ignore[operator]
        if operator == "%":
            if type(left) is int and type(right) is int:
                if right == 0:
                    raise ZeroDivisionError
                quotient = abs(left) // abs(right)
                if (left < 0) != (right < 0):
                    quotient = -quotient
                return left - quotient * right
            left_float = float(left)
            right_float = float(right)
            if right_float == 0.0:
                raise ZeroDivisionError
            return math.fmod(left_float, right_float)
        if operator == "==":
            return _tyvera_equal(left, right)
        if operator == "!=":
            return not _tyvera_equal(left, right)
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
        if isinstance(value, _TyveraByte):
            return str(value.value)
        return str(value)

