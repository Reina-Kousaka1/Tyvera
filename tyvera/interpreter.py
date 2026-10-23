from __future__ import annotations

import json
import math
import sys
from collections import ChainMap, deque
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import TextIO

from .ast import (
    ActorDeclaration,
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
from .typesys import INT_MAX, INT_MIN


_MISSING = object()
_MAX_ACTOR_MAILBOX = 100_000
_MAX_ACTOR_DELIVERIES = 100_000


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


@dataclass(slots=True, eq=False)
class _TyveraActor:
    instance: _TyveraInstance
    status: str = "alive"
    mailbox: deque[_ActorMessage] = dataclass_field(default_factory=deque)
    queued: bool = False


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
    ) -> None:
        self.functions = {function.name: function for function in program.functions}
        self.classes: dict[str, ClassDeclaration] = {declaration.name: declaration for declaration in program.classes}
        self.actors: dict[str, ActorDeclaration] = {declaration.name: declaration for declaration in program.actors}
        self._ready_actors: deque[_TyveraActor] = deque()
        self._current_actor: _TyveraActor | None = None
        self.output = output
        self.error_output = error_output if error_output is not None else sys.stderr
        self.max_call_depth = max_call_depth
        self.project_root = Path.cwd() if project_root is None else Path(project_root)
        self.secret_provider = SecretProvider(self.project_root)

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
            if expression.callee == "spawn":
                construction = expression.arguments[0]
                if not isinstance(construction, Call):
                    raise AssertionError("checked spawn expression is not an actor construction")
                declaration = self.actors[construction.callee]
                arguments = [self._evaluate(argument, variables, depth) for argument in construction.arguments]
                return self._spawn_actor(declaration, arguments, expression.location, depth + 1)
            if expression.callee == "runActors":
                return self._run_actors(expression.location, depth)
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
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
            return self._call(self.functions[expression.callee], arguments, expression.location, depth + 1)
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
            arguments = [self._evaluate(argument, variables, depth) for argument in expression.arguments]
            if isinstance(receiver, _TyveraActorRef):
                actor = receiver.actor
                if expression.method == "isAlive":
                    return actor.status == "alive"
                if expression.method == "stop":
                    actor.status = "stopped"
                    actor.mailbox.clear()
                    return None
                self._enqueue_actor_message(actor, expression.method, arguments, expression.location)
                return None
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
    ) -> _TyveraActorRef:
        instance = _TyveraInstance(declaration.name, {})
        actor = _TyveraActor(instance)
        instance.actor = actor
        constructor = next((method for method in declaration.handlers if method.name == "init"), None)
        if constructor is not None:
            try:
                self._call_method(constructor, instance, arguments, call_location, depth)
            except Diagnostic:
                actor.status = "crashed"
                actor.mailbox.clear()
                raise
        return _TyveraActorRef(actor)

    def _enqueue_actor_message(
        self, actor: _TyveraActor, method: str, arguments: list[object], location: Location,
    ) -> None:
        if actor.status != "alive":
            raise Diagnostic(location, "runtime", f"cannot send a message to a {actor.status} actor")
        if len(actor.mailbox) >= _MAX_ACTOR_MAILBOX:
            raise Diagnostic(location, "runtime", f"actor mailbox capacity ({_MAX_ACTOR_MAILBOX}) exceeded")
        actor.mailbox.append(_ActorMessage(method, arguments, location))
        if not actor.queued:
            actor.queued = True
            self._ready_actors.append(actor)

    def _run_actors(self, location: Location, depth: int) -> _TyveraVariant:
        if self._current_actor is not None:
            raise Diagnostic(location, "runtime", "runActors cannot be called from an actor message handler")
        processed = 0
        first_crash: str | None = None
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
                actor.mailbox.clear()
                if first_crash is None:
                    first_crash = (
                        f"actor '{declaration.name}' crashed at {error.location.filename}:"
                        f"{error.location.line}:{error.location.column}: {error.message}"
                    )
            except _LoopSignal as signal:
                actor.status = "crashed"
                actor.mailbox.clear()
                if first_crash is None:
                    first_crash = f"actor '{declaration.name}' crashed: '{signal.kind}' escaped its loop"
            finally:
                self._current_actor = None
            if actor.status == "alive" and actor.mailbox and not actor.queued:
                actor.queued = True
                self._ready_actors.append(actor)
        if self._ready_actors and processed >= _MAX_ACTOR_DELIVERIES:
            if first_crash is None:
                first_crash = f"actor scheduler reached the {_MAX_ACTOR_DELIVERIES}-message safety limit; messages remain queued"
        if first_crash is not None:
            return _TyveraVariant("Err", first_crash)
        return _TyveraVariant("Ok", processed)

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
