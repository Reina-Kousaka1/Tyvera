from __future__ import annotations

import json
import math
import sys
import time
from collections import ChainMap, deque
from dataclasses import dataclass, field as dataclass_field
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
    Statement,
    Unary,
    VariantPattern,
    WildcardPattern,
    WhileStatement,
)
from .source import Diagnostic, Location
from .secrets import SecretProvider, SecretProviderError
from .typesys import INT_MAX, INT_MIN, canonical_type


_MISSING = object()
_MAX_ACTOR_MAILBOX = 100_000
_MAX_ACTOR_DELIVERIES = 100_000
_MAX_SUPERVISOR_RESTARTS = 1_000
_MAX_TASK_STEPS = 100_000


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

    def __repr__(self) -> str:
        return "<Timer>"


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


@dataclass(slots=True, eq=False, repr=False)
class _TyveraTask:
    function: Function | None
    variables: ChainMap[str, object] | None
    location: Location
    depth: int = 0
    result_type: str = "Void"
    state: str = "runnable"
    frames: list[_AsyncBlockFrame | _AsyncLoopFrame] = dataclass_field(default_factory=list)
    wait_target: object | None = None
    wait_frame: _AsyncBlockFrame | None = None
    wait_statement: AwaitStatement | None = None
    due_at: float | None = None
    timeout_target: _TyveraTask | None = None
    error: str | None = None
    result: object = None
    parent: _TyveraTask | None = None
    children: list[_TyveraTask] = dataclass_field(default_factory=list)

    def __repr__(self) -> str:
        return "<Task>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraTaskGroup:
    owner: _TyveraTask | None
    tasks: list[_TyveraTask] = dataclass_field(default_factory=list)

    def __repr__(self) -> str:
        return "<TaskGroup>"


@dataclass(slots=True, eq=False, repr=False)
class _TyveraSupervisor:
    max_restarts: int
    window_milliseconds: int
    clock: Callable[[], float]
    restart_times: deque[float] = dataclass_field(default_factory=deque)
    children: list[_TyveraActor] = dataclass_field(default_factory=list)
    status: str = "alive"

    def __repr__(self) -> str:
        return "<Supervisor>"


@dataclass(slots=True, eq=False)
class _TyveraActor:
    instance: _TyveraInstance
    status: str = "alive"
    mailbox: deque[_ActorMessage] = dataclass_field(default_factory=deque)
    queued: bool = False
    supervisor: _TyveraSupervisor | None = None
    declaration: ActorDeclaration | None = None
    constructor_arguments: list[object] = dataclass_field(default_factory=list)
    pending_replies: set[_TyveraReplyCell] = dataclass_field(default_factory=set)


@dataclass(slots=True, eq=False, repr=False)
class _TyveraActorRef:
    actor: _TyveraActor

    def __repr__(self) -> str:
        return "<ActorRef>"


@dataclass(slots=True, eq=False)
class _TyveraList:
    values: list[object]


@dataclass(slots=True, eq=False)
class _TyveraMap:
    values: dict[object, object]


@dataclass(slots=True, eq=False)
class _TyveraVariant:
    variant: str
    payload: object = None


@dataclass(slots=True, eq=False, repr=False)
class _TyveraSecret:
    value: str

    def __repr__(self) -> str:
        return "<Secret redacted>"


@dataclass(slots=True, frozen=True, repr=False)
class _TyveraJson:
    kind: str
    value: object

    def __repr__(self) -> str:
        return "<Json value redacted>"


class Interpreter:
    def __init__(
        self, program: Program, output: TextIO, max_call_depth: int = 256, *,
        project_root: str | Path | None = None, error_output: TextIO | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.functions = {function.name: function for function in program.functions}
        self.classes: dict[str, ClassDeclaration] = {declaration.name: declaration for declaration in program.classes}
        self.actors: dict[str, ActorDeclaration] = {declaration.name: declaration for declaration in program.actors}
        self._ready_actors: deque[_TyveraActor] = deque()
        self._scheduled_timers: list[_TyveraTimer] = []
        self._ready_tasks: deque[_TyveraTask] = deque()
        self._tasks: list[_TyveraTask] = []
        self._task_groups: list[_TyveraTaskGroup] = []
        self._current_actor: _TyveraActor | None = None
        self._current_task: _TyveraTask | None = None
        self._running_tasks = False
        self.output = output
        self.error_output = error_output if error_output is not None else sys.stderr
        self.max_call_depth = max_call_depth
        self.project_root = Path.cwd() if project_root is None else Path(project_root)
        self.secret_provider = SecretProvider(self.project_root)
        self._clock = time.monotonic if clock is None else clock
        self._sleep = time.sleep if sleeper is None else sleeper

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
            elif isinstance(statement, AwaitStatement):
                raise AssertionError("await statements are dispatched by the cooperative task scheduler")
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
            elif isinstance(statement, MatchStatement):
                value = self._evaluate(statement.value, variables, depth)
                for arm in statement.arms:
                    matched, binding = self._matches(arm.pattern, value)
                    if not matched:
                        continue
                    arm_variables = variables.new_child()
                    if binding is not _MISSING:
                        pattern = arm.pattern
                        name = pattern.name if isinstance(pattern, BindingPattern) else pattern.binding
                        if name is not None:
                            arm_variables[name] = binding
                    self._execute_block(arm.body, arm_variables, depth)
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
            if expression.callee == "taskGroup":
                if self._current_actor is not None:
                    raise Diagnostic(expression.location, "runtime", "task groups cannot be created inside actor message handlers")
                group = _TyveraTaskGroup(self._current_task)
                self._task_groups.append(group)
                return group
            if expression.callee == "spawn":
                construction = expression.arguments[0]
                if not isinstance(construction, Call):
                    raise AssertionError("checked spawn expression is not an actor construction")
                declaration = self.actors[construction.callee]
                arguments = [self._evaluate(argument, variables, depth) for argument in construction.arguments]
                return self._spawn_actor(declaration, arguments, expression.location, depth + 1)
            if expression.callee == "oneForOne":
                arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
                max_restarts, window_milliseconds = arguments
                if max_restarts < 0:
                    raise Diagnostic(expression.location, "runtime", "oneForOne maxRestarts must be zero or greater")
                if max_restarts > _MAX_SUPERVISOR_RESTARTS:
                    raise Diagnostic(expression.location, "runtime", f"oneForOne maxRestarts cannot exceed {_MAX_SUPERVISOR_RESTARTS}")
                if window_milliseconds <= 0:
                    raise Diagnostic(expression.location, "runtime", "oneForOne windowMilliseconds must be greater than zero")
                return _TyveraSupervisor(max_restarts, window_milliseconds, self._clock)
            if expression.callee == "runActors":
                return self._run_actors(expression.location, depth)
            if expression.callee == "runTasks":
                return self._run_tasks(expression.location, depth)
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
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
                except OSError:
                    return _TyveraVariant("Err", "cannot read text file")
            if expression.callee == "writeText":
                path = self._project_path(arguments[0])
                if self._is_secret_storage_path(path):
                    return _TyveraVariant("Err", "secret file cannot be changed by the text-file API")
                try:
                    path.write_text(arguments[1], encoding="utf-8")
                    return _TyveraVariant("Ok", True)
                except OSError:
                    return _TyveraVariant("Err", "cannot write text file")
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
                return _TyveraJson("object", dict(arguments[0].values))
            if expression.callee == "List":
                return _TyveraList([])
            if expression.callee == "Map":
                return _TyveraMap({})
            if expression.callee == "Some":
                return _TyveraVariant("Some", arguments[0])
            if expression.callee == "None":
                return _TyveraVariant("None")
            if expression.callee == "Ok":
                return _TyveraVariant("Ok", arguments[0])
            if expression.callee == "Err":
                return _TyveraVariant("Err", arguments[0])
            if expression.callee in self.classes:
                return self._construct(self.classes[expression.callee], arguments, expression.location, depth + 1)
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
            receiver = self._evaluate(expression.receiver, variables, depth)
            if expression.member == "length" and isinstance(receiver, (_TyveraList, _TyveraMap)):
                return len(receiver.values)
            if not isinstance(receiver, _TyveraInstance):
                raise AssertionError("checked field access receiver is not an instance")
            return receiver.fields[expression.member]
        if isinstance(expression, MemberCall):
            receiver = self._evaluate(expression.receiver, variables, depth)
            if isinstance(receiver, _TyveraSupervisor):
                if expression.method == "start":
                    if receiver.status != "alive":
                        raise Diagnostic(expression.location, "runtime", f"cannot start a child on a {receiver.status} supervisor")
                    construction = expression.arguments[0]
                    if not isinstance(construction, Call):
                        raise AssertionError("checked supervisor.start argument is not an actor construction")
                    declaration = self.actors[construction.callee]
                    arguments = [self._evaluate(argument, variables, depth) for argument in construction.arguments]
                    return self._spawn_actor(declaration, arguments, expression.location, depth + 1, receiver)
                if expression.method == "isAlive":
                    return receiver.status == "alive"
                if expression.method == "stop":
                    if receiver.status == "alive":
                        receiver.status = "stopped"
                        for child in receiver.children:
                            self._stop_actor(child)
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
                request_arguments = [
                    self._evaluate(argument, variables, depth)
                    for argument in expression.arguments[prefix + 1:]
                ]
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
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
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
                    self._stop_actor(actor)
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
                    return _TyveraVariant("Ok", _TyveraVariant("None"))
            if isinstance(receiver, _TyveraTask):
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
                        return _TyveraVariant("Err", "task was cancelled")
                    if receiver.result_type == "Void":
                        return _TyveraVariant("Ok", receiver.state == "completed")
                    if receiver.state == "completed":
                        return _TyveraVariant("Ok", _TyveraVariant("Some", receiver.result))
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

    def _project_path(self, value: object) -> Path:
        if not isinstance(value, str):
            raise AssertionError("checked path argument is not String")
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    def _is_secret_storage_path(self, path: Path) -> bool:
        try:
            return path.resolve() == (self.project_root / ".tyvera.secrets").resolve()
        except (OSError, RuntimeError):
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
    def _matches(pattern, value: object) -> tuple[bool, object]:
        if isinstance(pattern, WildcardPattern):
            return True, _MISSING
        if isinstance(pattern, BindingPattern):
            return True, value
        if isinstance(pattern, LiteralPattern):
            return type(value) is type(pattern.value) and value == pattern.value, _MISSING
        if isinstance(pattern, VariantPattern):
            if not isinstance(value, _TyveraVariant) or value.variant != pattern.variant:
                return False, _MISSING
            if pattern.variant == "None":
                return True, _MISSING
            return True, value.payload
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
                    return _TyveraVariant("Some", values[arguments[0]])
                except KeyError as error:
                    return _TyveraVariant("None")
            if method == "remove":
                value = values.pop(arguments[0], _MISSING)
                return _TyveraVariant("None") if value is _MISSING else _TyveraVariant("Some", value)
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

    def _spawn_actor(
        self, declaration: ActorDeclaration, arguments: list[object], call_location: Location, depth: int,
        supervisor: _TyveraSupervisor | None = None,
    ) -> _TyveraActorRef:
        instance = _TyveraInstance(declaration.name, {})
        actor = _TyveraActor(
            instance,
            supervisor=supervisor,
            declaration=declaration,
            constructor_arguments=[self._copy_actor_value(value) for value in arguments],
        )
        instance.actor = actor
        if supervisor is not None:
            supervisor.children.append(actor)
        try:
            self._initialize_actor(actor, arguments, call_location, depth)
        except Diagnostic as error:
            actor.status = "crashed"
            actor.mailbox.clear()
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
        actor.instance = _TyveraInstance(declaration.name, {}, actor)
        constructor = next((method for method in declaration.handlers if method.name == "init"), None)
        if constructor is not None:
            self._call_method(constructor, actor.instance, arguments, call_location, depth)
        actor.status = "alive"

    @staticmethod
    def _copy_actor_value(value: object) -> object:
        if isinstance(value, _TyveraActorRef):
            return _TyveraActorRef(value.actor)
        if isinstance(value, _TyveraDuration):
            return value
        if isinstance(value, _TyveraReplySender):
            return _TyveraReplySender(value.cell)
        if isinstance(value, _TyveraVariant):
            return _TyveraVariant(value.variant, Interpreter._copy_actor_value(value.payload))
        if value is None or type(value) in {bool, int, float, str}:
            return value
        raise AssertionError("checked actor constructor value is not transferable")

    @staticmethod
    def _format_actor_failure(declaration: ActorDeclaration, error: Diagnostic) -> str:
        return (
            f"actor '{declaration.name}' crashed at {error.location.filename}:"
            f"{error.location.line}:{error.location.column}: {error.message}"
        )

    def _fail_pending_replies(self, actor: _TyveraActor, error: str) -> None:
        self._cancel_actor_timers(actor)
        for cell in actor.pending_replies:
            self._refresh_reply(cell)
            if cell.state == "pending":
                cell.state = "failed"
                cell.error = error
        actor.pending_replies.clear()

    def _stop_actor(self, actor: _TyveraActor) -> None:
        self._cancel_actor_timers(actor)
        if actor.status == "alive":
            actor.status = "stopped"
        for cell in actor.pending_replies:
            self._refresh_reply(cell)
            if cell.state == "pending":
                cell.state = "failed"
                cell.error = "actor stopped before replying"
        actor.pending_replies.clear()
        actor.mailbox.clear()

    def _cancel_actor_timers(self, actor: _TyveraActor) -> None:
        for timer in self._scheduled_timers:
            if timer.scheduled_actor is actor and not timer.delivered:
                timer.cancelled = True
        self._prune_scheduled_timers()

    def _prune_scheduled_timers(self) -> None:
        self._scheduled_timers = [
            timer for timer in self._scheduled_timers if not timer.cancelled and not timer.delivered
        ]

    def _fail_supervisor(self, supervisor: _TyveraSupervisor) -> None:
        supervisor.status = "failed"
        for child in supervisor.children:
            self._stop_actor(child)

    def _create_async_task(
        self, function: Function, arguments: list[object], location: Location, depth: int,
    ) -> _TyveraTask:
        if not function.is_async:
            raise AssertionError("only async functions create Task values")
        if depth >= self.max_call_depth:
            raise Diagnostic(location, "runtime", f"maximum call depth ({self.max_call_depth}) exceeded")
        variables = ChainMap({parameter.name: value for parameter, value in zip(function.parameters, arguments)})
        task = _TyveraTask(function, variables, location, depth + 1, canonical_type(function.return_type))
        task.parent = self._current_task
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
                           parent=self._current_task)
        if task.parent is not None:
            task.parent.children.append(task)
        self._tasks.append(task)
        return task

    def _create_timeout_task(
        self, target: _TyveraTask, duration: _TyveraDuration, location: Location,
    ) -> _TyveraTask:
        task = _TyveraTask(
            None, None, location, state="timed_wait", result_type=target.result_type,
            due_at=self._clock() + duration.milliseconds / 1000.0,
            timeout_target=target, parent=self._current_task,
        )
        if task.parent is not None:
            task.parent.children.append(task)
        self._tasks.append(task)
        return task

    def _run_tasks(self, location: Location, depth: int) -> _TyveraVariant:
        if self._current_actor is not None:
            raise Diagnostic(location, "runtime", "runTasks cannot be called from an actor message handler")
        if self._running_tasks:
            raise Diagnostic(location, "runtime", "runTasks cannot be called recursively")

        self._running_tasks = True
        steps = 0
        failure: str | None = None
        try:
            while True:
                self._promote_sleeping_tasks()
                self._settle_timed_tasks()
                self._wake_waiting_tasks()
                self._settle_joining_tasks()
                if self._ready_tasks:
                    if steps >= _MAX_TASK_STEPS:
                        failure = (
                            f"task scheduler reached the {_MAX_TASK_STEPS}-step safety limit; "
                            "tasks remain queued"
                        )
                        break
                    task = self._ready_tasks.popleft()
                    if task.state != "runnable":
                        continue
                    task.state = "running"
                    previous_task = self._current_task
                    self._current_task = task
                    try:
                        self._advance_async_task(task)
                    except _ReturnSignal as signal:
                        task.result = signal.value
                        self._complete_task(task)
                    except Diagnostic as error:
                        self._fail_task(task, error.render())
                    except _LoopSignal as signal:
                        self._handle_async_loop_control(task, signal)
                    finally:
                        self._current_task = previous_task
                    if task.state == "running":
                        if task.frames:
                            task.state = "runnable"
                            self._ready_tasks.append(task)
                        else:
                            self._complete_task(task)
                    steps += 1
                    self._settle_timed_tasks()
                    self._wake_waiting_tasks()
                    self._settle_joining_tasks()
                    continue

                active = [task for task in self._tasks
                          if task.state in {"runnable", "running", "waiting", "sleeping", "joining", "timed_wait"}]
                if not active:
                    break

                if any(
                    task.state == "waiting" and isinstance(task.wait_target, _TyveraReplyReceiver)
                    and self._await_state(task.wait_target)[0] == "pending"
                    for task in active
                ):
                    actor_result = self._run_actors(location, depth + 1)
                    if actor_result.variant == "Err" and failure is None:
                        failure = str(actor_result.payload)
                    self._promote_due_timers()
                    self._wake_waiting_tasks()
                    self._settle_joining_tasks()
                    if self._ready_tasks:
                        continue

                deadline = self._next_task_deadline()
                if deadline is not None:
                    delay = max(0.0, deadline - self._clock())
                    if delay > 0:
                        self._sleep(delay)
                    continue

                waiting_on_reply = any(
                    task.state == "waiting" and isinstance(task.wait_target, _TyveraReplyReceiver)
                    and self._await_state(task.wait_target)[0] == "pending"
                    for task in active
                )
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

    def _advance_async_task(self, task: _TyveraTask) -> None:
        if not task.frames:
            return
        frame = task.frames[-1]
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
                items = tuple(iterable.values.keys())
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
                if binding is not _MISSING:
                    pattern = arm.pattern
                    name = pattern.name if isinstance(pattern, BindingPattern) else pattern.binding
                    if name is not None:
                        scope[name] = binding
                task.frames.append(_AsyncBlockFrame(arm.body, scope))
                break
            return

        self._execute_block([statement], frame.variables, task.depth)
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
        del task.frames[loop_index + (1 if signal.kind == "continue" else 0):]

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

    @staticmethod
    def _complete_task_await(
        task: _TyveraTask, statement: AwaitStatement, target: object,
        variables: ChainMap[str, object],
    ) -> None:
        if statement.binding_name is None:
            return
        if isinstance(target, _TyveraTask):
            value = target.result
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
        task.frames.clear()
        task.wait_target = None
        task.wait_frame = None
        task.wait_statement = None
        if any(child.state in {"runnable", "running", "waiting", "sleeping", "joining", "timed_wait"}
               for child in task.children):
            task.state = "joining"
        else:
            task.state = "completed"

    def _settle_joining_tasks(self) -> None:
        for task in self._tasks:
            if task.state != "joining":
                continue
            failed_child = next((child for child in task.children if child.state == "failed"), None)
            if failed_child is not None:
                self._fail_task(task, f"owned child task failed: {failed_child.error or 'async task failed'}")
            elif not any(child.state in {"runnable", "running", "waiting", "sleeping", "joining", "timed_wait"}
                         for child in task.children):
                task.state = "completed"

    def _cancel_task(self, task: _TyveraTask) -> None:
        if task.state in {"completed", "failed", "cancelled"}:
            return
        task.state = "cancelled"
        task.wait_target = None
        task.wait_frame = None
        task.wait_statement = None
        task.frames.clear()
        if task.timeout_target is not None:
            self._cancel_task(task.timeout_target)
            task.timeout_target = None
        for child in task.children:
            self._cancel_task(child)

    def _fail_task(self, task: _TyveraTask, error: str) -> None:
        if task.state in {"completed", "failed", "cancelled"}:
            return
        task.state = "failed"
        task.error = error
        task.wait_target = None
        task.wait_frame = None
        task.wait_statement = None
        task.frames.clear()
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

        declaration = actor.declaration
        if declaration is None:
            raise AssertionError("supervised actor has no declaration")
        window_seconds = supervisor.window_milliseconds / 1000.0
        while True:
            now = self._clock()
            while supervisor.restart_times and now - supervisor.restart_times[0] > window_seconds:
                supervisor.restart_times.popleft()
            if len(supervisor.restart_times) >= supervisor.max_restarts:
                self._fail_supervisor(supervisor)
                return (
                    f"supervisor restart limit exceeded for actor '{declaration.name}' "
                    f"within {supervisor.window_milliseconds}ms: {failure}"
                )

            supervisor.restart_times.append(now)
            actor.mailbox.clear()
            try:
                self._initialize_actor(
                    actor,
                    [self._copy_actor_value(value) for value in actor.constructor_arguments],
                    call_location,
                    depth + 1,
                )
            except Diagnostic as error:
                actor.status = "crashed"
                failure = self._format_actor_failure(declaration, error)
                continue
            except _LoopSignal as signal:
                actor.status = "crashed"
                failure = f"actor '{declaration.name}' crashed: '{signal.kind}' escaped its constructor"
                continue
            return None

    def _enqueue_actor_message(
        self, actor: _TyveraActor, method: str, arguments: list[object], location: Location,
    ) -> None:
        if actor.status != "alive":
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

    def _run_actors(self, location: Location, depth: int) -> _TyveraVariant:
        if self._current_actor is not None:
            raise Diagnostic(location, "runtime", "runActors cannot be called from an actor message handler")
        processed = 0
        first_crash: str | None = None
        self._promote_due_timers()
        while self._ready_actors and processed < _MAX_ACTOR_DELIVERIES:
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
            except Diagnostic as error:
                actor.status = "crashed"
                failure = self._restart_supervised_actor(
                    actor, self._format_actor_failure(declaration, error), message.location, depth + 1,
                )
                if failure is not None and first_crash is None:
                    first_crash = failure
            except _LoopSignal as signal:
                actor.status = "crashed"
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
            if actor.status == "alive" and actor.mailbox and not actor.queued:
                actor.queued = True
                self._ready_actors.append(actor)
        if self._ready_actors and processed >= _MAX_ACTOR_DELIVERIES:
            if first_crash is None:
                first_crash = f"actor scheduler reached the {_MAX_ACTOR_DELIVERIES}-message safety limit; messages remain queued"
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
