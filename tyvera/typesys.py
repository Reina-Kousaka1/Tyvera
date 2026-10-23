from __future__ import annotations

import math
from collections import ChainMap
from dataclasses import dataclass

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
    EnumDeclaration,
    EnumPattern,
    Field,
    ForStatement,
    Function,
    IfStatement,
    InterfaceDeclaration,
    LetStatement,
    Literal,
    LoopControl,
    BindingPattern,
    LiteralPattern,
    MatchStatement,
    MemberAccess,
    MemberCall,
    Method,
    MethodSignature,
    Name,
    Program,
    ReturnStatement,
    RecordDeclaration,
    RecordPattern,
    NewtypeDeclaration,
    TypeAliasDeclaration,
    Statement,
    Unary,
    TupleExpression,
    TuplePattern,
    VariantPattern,
    UsingStatement,
    WildcardPattern,
    WhileStatement,
)
from .source import Diagnostic, Location
from .type_names import make_type, split_type, substitute_type


_BUILTIN_TYPES = {
    "Int", "Float", "Bool", "String", "Void", "Unit", "List", "Map", "Option", "Result",
    "Secret", "Json", "Tuple", "ActorRef", "Supervisor", "Duration", "Timer", "ReplySender", "ReplyReceiver", "Task", "TaskGroup", "TextFile",
}
_BUILTIN_FUNCTIONS = {
    "println", "eprintln", "readText", "writeText", "openText", "pathJoin", "pathExists",
    "parseJson", "jsonStringify", "jsonString", "jsonInt", "jsonFloat", "jsonBool",
    "jsonNull", "jsonArray", "jsonObject", "spawn", "runActors", "oneForOne",
    "milliseconds", "seconds", "timerAfter", "sleepFor", "runTasks", "taskGroup",
}
_DISPLAYABLE_TYPES = {"Int", "Float", "Bool", "String"}
_NUMERIC_TYPES = {"Int", "Float"}
INT_MIN = -(1 << 63)
INT_MAX = (1 << 63) - 1


def canonical_type(name: str) -> str:
    if name == "Unit":
        return "Void"
    base, arguments = split_type(name)
    if base == "Task" and not arguments:
        arguments = ["Void"]
    return make_type(base, [canonical_type(argument) for argument in arguments])


@dataclass(frozen=True, slots=True)
class Binding:
    type_name: str
    mutable: bool = False


@dataclass(frozen=True, slots=True)
class Signature:
    parameter_types: tuple[str, ...]
    return_type: str
    location: Location
    type_parameters: tuple[str, ...] = ()
    is_async: bool = False
    type_constraints: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(slots=True)
class ClassInfo:
    declaration: ClassDeclaration | ActorDeclaration
    fields: dict[str, Field]
    methods: dict[str, Signature]
    type_parameters: tuple[str, ...] = ()


@dataclass(slots=True)
class InterfaceInfo:
    declaration: InterfaceDeclaration
    methods: dict[str, Signature]


@dataclass(slots=True)
class RecordInfo:
    declaration: RecordDeclaration
    fields: dict[str, Field]
    type_parameters: tuple[str, ...] = ()


@dataclass(slots=True)
class EnumInfo:
    declaration: EnumDeclaration
    variants: dict[str, tuple[str, ...]]
    type_parameters: tuple[str, ...] = ()


class TypeChecker:
    def __init__(self, program: Program, filename: str = "<memory>") -> None:
        self.program = program
        self.filename = filename
        self.signatures: dict[str, Signature] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.interfaces: dict[str, InterfaceInfo] = {}
        self.records: dict[str, RecordInfo] = {}
        self.enums: dict[str, EnumInfo] = {}
        self.newtypes: dict[str, NewtypeDeclaration] = {}
        self.type_aliases: dict[str, TypeAliasDeclaration] = {}
        self.expression_types: dict[Location, str] = {}
        self.generic_substitutions: dict[Location, dict[str, str]] = {}
        self.match_coverage: dict[Location, frozenset[str]] = {}
        self.match_exhaustiveness: dict[Location, bool] = {}
        self.active_type_constraints: dict[str, tuple[str, ...]] = {}
        self.actors: dict[str, ClassInfo] = {}
        self.actor_names: set[str] = set()
        self.type_names: set[str] = set(_BUILTIN_TYPES)
        self.generic_arities: dict[str, int] = {
            "List": 1, "Map": 2, "Option": 1, "Result": 2, "Secret": 1, "ActorRef": 1,
            "ReplySender": 1, "ReplyReceiver": 1, "Task": 1,
        }
        self.active_type_variables: set[str] = set()
        self._checking_using_resource = False

    def check(self) -> dict[str, Signature]:
        self._collect_types()
        self._collect_signatures()
        entry = self.signatures.get("main")
        if entry is None:
            location = self.program.functions[0].location if self.program.functions else Location(self.filename, 1, 1)
            raise Diagnostic(location, "type", "program must define 'fn main()'")
        if entry.parameter_types:
            raise Diagnostic(entry.location, "type", "entry point 'main' must not have parameters")
        if entry.return_type != "Void":
            raise Diagnostic(entry.location, "type", "entry point 'main' must return Void")
        if entry.is_async:
            raise Diagnostic(entry.location, "type", "entry point 'main' must be synchronous; start tasks from 'main' and call runTasks()")
        self._check_interface_implementations()
        for function in self.program.functions:
            self._check_function(function, self.signatures[function.name])
        for class_info in [*self.classes.values(), *self.actors.values()]:
            for method in class_info.declaration.methods:
                signature = class_info.methods[method.name]
                self._check_method(method, signature, class_info)
            constructor = next((method for method in class_info.declaration.methods if method.name == "init"), None)
            if constructor is not None:
                self._check_constructor_initialization(constructor, class_info)
        return self.signatures

    def _collect_types(self) -> None:
        names: dict[str, Location] = {}
        declarations = [*self.program.classes, *self.program.interfaces, *self.program.actors,
                        *self.program.records, *self.program.enums, *self.program.newtypes,
                        *self.program.type_aliases]
        for declaration in declarations:
            if declaration.name == "secrets":
                raise Diagnostic(declaration.location, "type", "'secrets' is a reserved namespace")
            if declaration.name in _BUILTIN_TYPES:
                raise Diagnostic(declaration.location, "type", f"type name '{declaration.name}' is reserved")
            if isinstance(declaration, ActorDeclaration) and declaration.name in _BUILTIN_FUNCTIONS:
                raise Diagnostic(declaration.location, "type", f"'{declaration.name}' is reserved for a built-in function")
            if declaration.name in names:
                raise Diagnostic(declaration.location, "type", f"type '{declaration.name}' is already declared")
            names[declaration.name] = declaration.location
        self.type_names.update(names)
        self.actor_names = {actor.name for actor in self.program.actors}
        for declaration in self.program.classes:
            self._validate_type_parameters(declaration.type_parameters, declaration.location)
            self.generic_arities[declaration.name] = len(declaration.type_parameters)
        for declaration in self.program.records:
            self._validate_type_parameters(declaration.type_parameters, declaration.location)
            self.generic_arities[declaration.name] = len(declaration.type_parameters)
        for declaration in self.program.enums:
            self._validate_type_parameters(declaration.type_parameters, declaration.location)
            self.generic_arities[declaration.name] = len(declaration.type_parameters)
        for declaration in self.program.newtypes:
            self._validate_type_parameters(declaration.type_parameters, declaration.location)
            self.generic_arities[declaration.name] = len(declaration.type_parameters)
        for declaration in self.program.type_aliases:
            self._validate_type_parameters(declaration.type_parameters, declaration.location)
            self.generic_arities[declaration.name] = len(declaration.type_parameters)
            self.type_aliases[declaration.name] = declaration
        for declaration in self.program.type_aliases:
            self._validate_alias_cycle(declaration.target_type, {declaration.name})
            self._require_known_type(declaration.target_type, declaration.location,
                                     set(declaration.type_parameters))
            if self._canonical_type(declaration.target_type) == "Void":
                raise Diagnostic(declaration.location, "type", "type aliases cannot target Void")
        for declaration in self.program.interfaces:
            methods: dict[str, Signature] = {}
            for method in declaration.methods:
                if method.name == "init":
                    raise Diagnostic(method.location, "type", "interfaces cannot declare constructors")
                if method.name in methods:
                    raise Diagnostic(method.location, "type", f"interface method '{method.name}' is already declared")
                methods[method.name] = self._method_signature(method)
            self.interfaces[declaration.name] = InterfaceInfo(declaration, methods)

        for declaration in self.program.records:
            record_type_variables = set(declaration.type_parameters)
            fields: dict[str, Field] = {}
            for member in declaration.fields:
                if member.name in fields:
                    raise Diagnostic(member.location, "type", f"record field '{member.name}' is already declared")
                self._require_known_type(member.type_name, member.location, record_type_variables)
                if self._canonical_type(member.type_name) == "Void":
                    raise Diagnostic(member.location, "type", "record fields cannot have type Void")
                if member.mutable:
                    raise Diagnostic(member.location, "type", "record fields are immutable")
                fields[member.name] = member
            self.records[declaration.name] = RecordInfo(declaration, fields, tuple(declaration.type_parameters))

        for declaration in self.program.enums:
            type_variables = set(declaration.type_parameters)
            variants: dict[str, tuple[str, ...]] = {}
            for variant in declaration.variants:
                if variant.name in variants:
                    raise Diagnostic(variant.location, "type", f"enum variant '{variant.name}' is already declared")
                payload_types: list[str] = []
                for payload_type in variant.payload_types:
                    self._require_known_type(payload_type, variant.location, type_variables)
                    if self._canonical_type(payload_type) == "Void":
                        raise Diagnostic(variant.location, "type", "enum payloads cannot have type Void")
                    payload_types.append(payload_type)
                variants[variant.name] = tuple(payload_types)
            self.enums[declaration.name] = EnumInfo(declaration, variants, tuple(declaration.type_parameters))

        for declaration in self.program.newtypes:
            type_variables = set(declaration.type_parameters)
            self._require_known_type(declaration.underlying_type, declaration.location, type_variables)
            if self._canonical_type(declaration.underlying_type) == "Void":
                raise Diagnostic(declaration.location, "type", "newtypes cannot wrap Void")
            self.newtypes[declaration.name] = declaration

        for declaration in self.program.classes:
            class_type_variables = set(declaration.type_parameters)
            fields: dict[str, Field] = {}
            for member in declaration.fields:
                if member.name in fields:
                    raise Diagnostic(member.location, "type", f"field '{member.name}' is already declared")
                self._require_known_type(member.type_name, member.location, class_type_variables)
                if self._canonical_type(member.type_name) == "Void":
                    raise Diagnostic(member.location, "type", "fields cannot have type Void")
                fields[member.name] = member
            methods: dict[str, Signature] = {}
            for method in declaration.methods:
                if method.name in methods:
                    raise Diagnostic(method.location, "type", f"method '{method.name}' is already declared")
                if method.name in fields:
                    raise Diagnostic(method.location, "type", f"method '{method.name}' conflicts with a field")
                methods[method.name] = self._method_signature(method, class_type_variables)
            constructor = methods.get("init")
            if declaration.fields and constructor is None:
                raise Diagnostic(declaration.location, "type", f"class '{declaration.name}' declares fields and requires an 'init' constructor")
            self.classes[declaration.name] = ClassInfo(declaration, fields, methods, tuple(declaration.type_parameters))

        for declaration in self.program.actors:
            fields: dict[str, Field] = {}
            for member in declaration.fields:
                if member.name in fields:
                    raise Diagnostic(member.location, "type", f"actor state field '{member.name}' is already declared")
                self._require_known_type(member.type_name, member.location)
                if self._canonical_type(member.type_name) == "Void":
                    raise Diagnostic(member.location, "type", "actor state fields cannot have type Void")
                fields[member.name] = member
            handlers: dict[str, Signature] = {}
            for method in declaration.handlers:
                if method.name in {"stop", "isAlive", "request", "requestWithin", "sendAfter"}:
                    raise Diagnostic(method.location, "type", f"actor handler name '{method.name}' is reserved by ActorRef methods")
                if method.name in handlers:
                    raise Diagnostic(method.location, "type", f"actor handler '{method.name}' is already declared")
                if method.name in fields:
                    raise Diagnostic(method.location, "type", f"actor handler '{method.name}' conflicts with a state field")
                signature = self._method_signature(method)
                if method.name != "init" and signature.return_type != "Void":
                    raise Diagnostic(method.location, "type", "actor message handlers must return Void")
                for index, (parameter, parameter_type) in enumerate(zip(method.parameters, signature.parameter_types)):
                    base, arguments = split_type(parameter_type)
                    is_reply_sender = base == "ReplySender" and len(arguments) == 1
                    if is_reply_sender and (method.name == "init" or index != len(signature.parameter_types) - 1):
                        raise Diagnostic(parameter.location, "type", "ReplySender<T> is only allowed as the final parameter of an actor message handler")
                    transferable = (
                        self._is_actor_handler_message_type(parameter_type)
                        if method.name != "init" else self._is_actor_message_type(parameter_type)
                    )
                    if not transferable:
                        purpose = "actor constructor" if method.name == "init" else "actor message"
                        raise Diagnostic(parameter.location, "type", f"{purpose} cannot carry mutable or process-local value type '{parameter_type}'")
                handlers[method.name] = signature
            if fields and "init" not in handlers:
                raise Diagnostic(declaration.location, "type", f"actor '{declaration.name}' declares state and requires an 'init' constructor")
            self.actors[declaration.name] = ClassInfo(declaration, fields, handlers)

    def _canonical_type(self, type_name: str) -> str:
        base, arguments = split_type(type_name)
        alias = self.type_aliases.get(base)
        if alias is not None:
            if len(arguments) != len(alias.type_parameters):
                return canonical_type(type_name)
            substitutions = dict(zip(alias.type_parameters, arguments))
            return self._canonical_type(substitute_type(alias.target_type, substitutions))
        if base == "Unit":
            base = "Void"
        if base == "Task" and not arguments:
            arguments = ["Void"]
        return make_type(base, [self._canonical_type(argument) for argument in arguments])

    def _validate_alias_cycle(self, type_name: str, active: set[str]) -> None:
        base, arguments = split_type(type_name)
        alias = self.type_aliases.get(base)
        if alias is not None:
            if base in active:
                raise Diagnostic(alias.location, "type", f"type alias cycle includes '{base}'")
            if len(arguments) != len(alias.type_parameters):
                return
            substitutions = dict(zip(alias.type_parameters, arguments))
            self._validate_alias_cycle(substitute_type(alias.target_type, substitutions), active | {base})
            return
        for argument in arguments:
            self._validate_alias_cycle(argument, active)

    def _is_actor_message_type(self, type_name: str) -> bool:
        base, arguments = split_type(type_name)
        if base in {"Int", "Float", "Bool", "String", "Duration"} and not arguments:
            return True
        if base == "ActorRef" and len(arguments) == 1:
            actor_base, actor_arguments = split_type(arguments[0])
            return actor_base in self.actor_names and not actor_arguments
        if base == "Option" and len(arguments) == 1:
            return self._is_actor_message_type(arguments[0])
        if base == "Result" and len(arguments) == 2:
            return all(self._is_actor_message_type(argument) for argument in arguments)
        return False

    def _is_actor_handler_message_type(self, type_name: str) -> bool:
        base, arguments = split_type(type_name)
        if base == "ReplySender" and len(arguments) == 1:
            return self._is_actor_message_type(arguments[0])
        return self._is_actor_message_type(type_name)

    def _collect_signatures(self) -> None:
        all_names = (set(self.classes) | set(self.interfaces) | set(self.actors) | set(self.records)
                     | set(self.enums) | set(self.newtypes) | set(self.type_aliases))
        for function in self.program.functions:
            if function.name in _BUILTIN_FUNCTIONS:
                raise Diagnostic(function.location, "type", f"'{function.name}' is a built-in function name")
            if function.name == "secrets":
                raise Diagnostic(function.location, "type", "'secrets' is a reserved namespace")
            if function.name in all_names or function.name in self.signatures:
                raise Diagnostic(function.location, "type", f"function or type '{function.name}' is already declared")
            self.signatures[function.name] = self._method_signature(function)

    def _validate_type_parameters(self, parameters: list[str], location: Location) -> None:
        seen: set[str] = set()
        for name in parameters:
            if name in seen:
                raise Diagnostic(location, "type", f"generic type parameter '{name}' is declared more than once")
            if name in self.type_names:
                raise Diagnostic(location, "type", f"generic type parameter '{name}' conflicts with a known type")
            seen.add(name)

    def _method_signature(
        self, function: Function | Method | MethodSignature,
        type_variables: set[str] | None = None,
    ) -> Signature:
        declared_parameters = list(getattr(function, "type_parameters", []))
        self._validate_type_parameters(declared_parameters, function.location)
        declared_constraints = self._validate_function_constraints(
            declared_parameters, getattr(function, "type_constraints", {}), function.location,
        )
        known_variables = set(type_variables or ()) | set(declared_parameters)
        parameter_names: set[str] = set()
        parameter_types: list[str] = []
        for parameter in function.parameters:
            if parameter.name == "self":
                raise Diagnostic(parameter.location, "type", "'self' is reserved for the current instance")
            if parameter.name in _BUILTIN_FUNCTIONS:
                raise Diagnostic(parameter.location, "type", f"'{parameter.name}' is reserved for a built-in function")
            if parameter.name == "secrets":
                raise Diagnostic(parameter.location, "type", "'secrets' is a reserved namespace")
            if parameter.name in parameter_names:
                raise Diagnostic(parameter.location, "type", f"parameter '{parameter.name}' is declared more than once")
            parameter_names.add(parameter.name)
            self._require_known_type(parameter.type_name, parameter.location, known_variables)
            if self._canonical_type(parameter.type_name) == "Void":
                raise Diagnostic(parameter.location, "type", "parameters cannot have type Void")
            parameter_types.append(self._canonical_type(parameter.type_name))
        self._require_known_type(function.return_type, function.location, known_variables)
        is_async = bool(getattr(function, "is_async", False))
        return Signature(tuple(parameter_types), self._canonical_type(function.return_type), function.location,
                         tuple(declared_parameters), is_async, declared_constraints)

    def _validate_function_constraints(
        self, parameters: list[str], constraints: dict[str, list[str]], location: Location,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        parameter_set = set(parameters)
        checked: list[tuple[str, tuple[str, ...]]] = []
        for parameter, bounds in constraints.items():
            if parameter not in parameter_set:
                raise Diagnostic(location, "type", f"constraint refers to undeclared type parameter '{parameter}'")
            seen: set[str] = set()
            for bound in bounds:
                base, arguments = split_type(bound)
                if arguments:
                    raise Diagnostic(location, "type", "generic function constraints must name non-generic interfaces")
                if base not in self.interfaces:
                    raise Diagnostic(location, "type", f"generic constraint '{bound}' is not a declared interface")
                if bound in seen:
                    raise Diagnostic(location, "type", f"generic constraint '{bound}' is repeated for '{parameter}'")
                seen.add(bound)
            if bounds:
                checked.append((parameter, tuple(bounds)))
        return tuple(checked)

    def _require_known_type(
        self, type_name: str, location: Location, type_variables: set[str] | None = None,
        allow_actor: bool = False,
    ) -> None:
        known_variables = self.active_type_variables if type_variables is None else type_variables
        if type_name in known_variables:
            return
        base, arguments = split_type(type_name)
        if base in known_variables:
            if arguments:
                raise Diagnostic(location, "type", f"type parameter '{base}' cannot take type arguments")
            return
        if base not in self.type_names:
            raise Diagnostic(location, "type", f"unknown type '{base}'")
        if base == "TextFile":
            raise Diagnostic(location, "type", "TextFile is a scoped resource and cannot appear in declared types")
        if base in self.actor_names and not allow_actor:
            raise Diagnostic(location, "type", f"actor type '{base}' can only be used inside ActorRef<{base}>")
        expected_arity = self.generic_arities.get(base, 0)
        if base == "Task" and not arguments:
            return
        if base == "Tuple":
            if len(arguments) < 2:
                raise Diagnostic(location, "type", "Tuple requires at least two element types")
        elif len(arguments) != expected_arity:
            if expected_arity:
                raise Diagnostic(location, "type", f"type '{base}' expects {expected_arity} type argument(s), got {len(arguments)}")
            if arguments:
                raise Diagnostic(location, "type", f"type '{base}' does not accept type arguments")
        for argument in arguments:
            self._require_known_type(argument, location, known_variables, allow_actor=(base == "ActorRef"))
            if self._canonical_type(argument) == "Void" and base != "Task":
                raise Diagnostic(location, "type", "type arguments cannot be Void")
        if base == "ActorRef" and arguments:
            actor_base, actor_arguments = split_type(arguments[0])
            if actor_arguments or actor_base not in self.actor_names:
                raise Diagnostic(location, "type", "ActorRef requires one actor type, such as ActorRef<Counter>")
        if base == "Map" and arguments:
            if not self._is_hashable_key_type(arguments[0], known_variables):
                raise Diagnostic(location, "type", f"Map key type '{arguments[0]}' is not a supported primitive or object-reference type")

    def _is_hashable_key_type(
        self,
        type_name: str,
        type_variables: set[str],
        visiting: frozenset[tuple[str, tuple[str, ...]]] = frozenset(),
    ) -> bool:
        type_name = self._canonical_type(type_name)
        base, arguments = split_type(type_name)
        if base in type_variables:
            return False
        key = (base, tuple(arguments))
        if key in visiting:
            return True
        if any(visited_base == base for visited_base, _ in visiting):
            # A recursive generic that changes its arguments cannot be proven
            # hashable without expanding an unbounded family of types.
            return False
        nested = visiting | {key}
        if base in {"Int", "Float", "Bool", "String", "Duration", "Json"}:
            return not arguments
        if base == "ActorRef":
            return len(arguments) == 1
        if base == "Tuple" and len(arguments) >= 2:
            return all(self._is_hashable_key_type(argument, type_variables, nested) for argument in arguments)
        if base in self.classes or base in self.interfaces:
            return len(arguments) == self.generic_arities.get(base, 0)
        record = self.records.get(base)
        if record is not None:
            if len(arguments) != len(record.type_parameters):
                return False
            substitutions = dict(zip(record.type_parameters, arguments))
            return all(self._is_hashable_key_type(substitute_type(field.type_name, substitutions), type_variables, nested)
                       for field in record.fields.values())
        newtype = self.newtypes.get(base)
        if newtype is not None:
            if len(arguments) != len(newtype.type_parameters):
                return False
            substitutions = dict(zip(newtype.type_parameters, arguments))
            return self._is_hashable_key_type(
                substitute_type(newtype.underlying_type, substitutions), type_variables, nested,
            )
        enum = self.enums.get(base)
        if enum is not None:
            if len(arguments) != len(enum.type_parameters):
                return False
            substitutions = dict(zip(enum.type_parameters, arguments))
            return all(self._is_hashable_key_type(substitute_type(payload, substitutions), type_variables, nested)
                       for payloads in enum.variants.values() for payload in payloads)
        if base == "Option" and len(arguments) == 1:
            return self._is_hashable_key_type(arguments[0], type_variables, nested)
        if base == "Result" and len(arguments) == 2:
            return all(self._is_hashable_key_type(argument, type_variables, nested) for argument in arguments)
        return False

    def _check_interface_implementations(self) -> None:
        for class_info in self.classes.values():
            declaration = class_info.declaration
            seen: set[str] = set()
            for interface_name in declaration.interfaces:
                if interface_name in seen:
                    raise Diagnostic(declaration.location, "type", f"interface '{interface_name}' is listed more than once")
                seen.add(interface_name)
                interface = self.interfaces.get(interface_name)
                if interface is None:
                    raise Diagnostic(declaration.location, "type", f"unknown interface '{interface_name}'")
                for method_name, required in interface.methods.items():
                    actual = class_info.methods.get(method_name)
                    if actual is None:
                        raise Diagnostic(declaration.location, "type", f"class '{declaration.name}' does not implement '{interface_name}.{method_name}'")
                    if actual.parameter_types != required.parameter_types:
                        raise Diagnostic(actual.location, "type", f"method '{method_name}' does not match '{interface_name}': parameter types differ")
                    if actual.return_type != required.return_type:
                        raise Diagnostic(actual.location, "type", f"method '{method_name}' does not match '{interface_name}': expected return type {required.return_type}, found {actual.return_type}")

    def _check_function(self, function: Function, signature: Signature) -> None:
        previous = self.active_type_variables
        previous_constraints = self.active_type_constraints
        self.active_type_variables = set(signature.type_parameters)
        self.active_type_constraints = dict(signature.type_constraints)
        variables = ChainMap({p.name: Binding(self._canonical_type(p.type_name)) for p in function.parameters})
        try:
            outcomes = self._check_block(function.body, variables, signature.return_type, 0,
                                         async_allowed=function.is_async)
            self._require_return_paths(function.name, signature.return_type, outcomes, function.location)
        finally:
            self.active_type_variables = previous
            self.active_type_constraints = previous_constraints

    def _check_method(self, method: Method, signature: Signature, owner: ClassInfo) -> None:
        if method.name == "init" and signature.return_type != "Void":
            raise Diagnostic(method.location, "type", "constructors must return Void")
        previous = self.active_type_variables
        self.active_type_variables = set(owner.type_parameters)
        variables = ChainMap({p.name: Binding(self._canonical_type(p.type_name)) for p in method.parameters})
        variables["self"] = Binding(owner.declaration.name)
        try:
            outcomes = self._check_block(method.body, variables, signature.return_type, 0, owner, method.name)
            self._require_return_paths(f"{owner.declaration.name}.{method.name}", signature.return_type, outcomes, method.location)
        finally:
            self.active_type_variables = previous

    @staticmethod
    def _require_return_paths(name: str, return_type: str, outcomes: set[str], location: Location) -> None:
        if return_type != "Void" and "next" in outcomes:
            raise Diagnostic(location, "type", f"function '{name}' must return {return_type} on all reachable paths")

    def _check_block(
        self, body: list[Statement], variables: ChainMap[str, Binding],
        return_type: str, loop_depth: int, owner: ClassInfo | None = None,
        method_name: str | None = None, async_allowed: bool = False,
    ) -> set[str]:
        outcomes = {"next"}
        for statement in body:
            exits = {"next"}
            if isinstance(statement, LetStatement):
                if statement.name == "println":
                    raise Diagnostic(statement.location, "type", "'println' is reserved for the built-in function")
                if statement.name in _BUILTIN_FUNCTIONS:
                    raise Diagnostic(statement.location, "type", f"'{statement.name}' is reserved for a built-in function")
                if statement.name == "secrets":
                    raise Diagnostic(statement.location, "type", "'secrets' is a reserved namespace")
                if statement.name == "self":
                    raise Diagnostic(statement.location, "type", "'self' is reserved for the current instance")
                if statement.name in variables.maps[0]:
                    raise Diagnostic(statement.location, "type", f"variable '{statement.name}' is already declared")
                value_type = self._expression_type(statement.value, variables, owner)
                if statement.annotation is not None:
                    self._require_known_type(statement.annotation, statement.location)
                    if self._canonical_type(statement.annotation) == "Void":
                        raise Diagnostic(statement.location, "type", "variables cannot have type Void")
                    self._require_same_type(self._canonical_type(statement.annotation), value_type, statement.location)
                    variable_type = self._canonical_type(statement.annotation)
                else:
                    if value_type == "Void":
                        raise Diagnostic(statement.location, "type", "cannot infer a variable type from a Void expression")
                    variable_type = value_type
                variables[statement.name] = Binding(variable_type, statement.mutable)
            elif isinstance(statement, Assignment):
                value_type = self._expression_type(statement.value, variables, owner)
                if isinstance(statement.target, str):
                    binding = variables.get(statement.target)
                    if binding is None:
                        raise Diagnostic(statement.location, "type", f"unknown variable '{statement.target}'")
                    if not binding.mutable:
                        raise Diagnostic(statement.location, "type", f"cannot assign to immutable binding '{statement.target}'")
                    self._require_same_type(binding.type_name, value_type, statement.location)
                else:
                    field_type, field = self._member_field(statement.target, variables, owner)
                    is_self_field = isinstance(statement.target.receiver, Name) and statement.target.receiver.identifier == "self"
                    if not field.mutable and not (method_name == "init" and is_self_field):
                        raise Diagnostic(statement.location, "type", f"cannot assign to immutable field '{field.name}'")
                    if method_name == "init" and not is_self_field and not field.mutable:
                        raise Diagnostic(statement.location, "type", "an immutable field can only be initialized through 'self' in its constructor")
                    self._require_same_type(field_type, value_type, statement.location)
            elif isinstance(statement, ReturnStatement):
                value_type = "Void" if statement.value is None else self._expression_type(statement.value, variables, owner)
                self._require_same_type(return_type, value_type, statement.location)
                exits = {"return"}
            elif isinstance(statement, AwaitStatement):
                if not async_allowed:
                    raise Diagnostic(statement.location, "type", "'await' is only valid inside an async function")
                awaited_type = self._expression_type(statement.value, variables, owner)
                base, arguments = split_type(awaited_type)
                if base == "Task" and len(arguments) == 1:
                    result_type = arguments[0]
                elif base == "ReplyReceiver" and len(arguments) == 1:
                    result_type = arguments[0]
                elif base == "TaskGroup" and not arguments:
                    result_type = "Void"
                else:
                    raise Diagnostic(statement.value.location, "type", f"'await' expects Task<T>, TaskGroup, or ReplyReceiver<T>, found {awaited_type}")
                if statement.binding_name is not None:
                    name = statement.binding_name
                    if name in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                        raise Diagnostic(statement.location, "type", f"'{name}' is reserved and cannot be bound by await")
                    if name in variables.maps[0]:
                        raise Diagnostic(statement.location, "type", f"variable '{name}' is already declared")
                    if self._canonical_type(result_type) == "Void":
                        raise Diagnostic(statement.location, "type", "a Void task cannot initialize a variable")
                    if statement.annotation is not None:
                        self._require_known_type(statement.annotation, statement.location)
                        if self._canonical_type(statement.annotation) == "Void":
                            raise Diagnostic(statement.location, "type", "variables cannot have type Void")
                        self._require_same_type(self._canonical_type(statement.annotation), result_type, statement.location)
                        result_type = self._canonical_type(statement.annotation)
                    variables[name] = Binding(result_type, statement.mutable)
            elif isinstance(statement, Block):
                exits = self._check_block(statement.body, variables.new_child(), return_type, loop_depth,
                                          owner, method_name, async_allowed)
            elif isinstance(statement, UsingStatement):
                if statement.name in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                    raise Diagnostic(statement.location, "type", f"'{statement.name}' is reserved and cannot name a resource")
                if statement.name in variables.maps[0]:
                    raise Diagnostic(statement.location, "type", f"variable '{statement.name}' is already declared")
                if not isinstance(statement.resource, Call) or statement.resource.callee != "openText":
                    raise Diagnostic(statement.location, "type", "using currently requires a direct openText(path, mode) acquisition")
                previous_resource_context = self._checking_using_resource
                self._checking_using_resource = True
                try:
                    resource_type = self._expression_type(statement.resource, variables, owner)
                finally:
                    self._checking_using_resource = previous_resource_context
                if resource_type != "Result<TextFile,String>":
                    raise Diagnostic(statement.resource.location, "type", "using requires a TextFile acquisition result")
                using_variables = variables.new_child()
                using_variables[statement.name] = Binding("TextFile")
                exits = self._check_block(statement.body, using_variables, return_type, loop_depth,
                                          owner, method_name, async_allowed)
                if statement.error_name is not None:
                    if statement.error_name in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                        raise Diagnostic(statement.location, "type", f"'{statement.error_name}' is reserved and cannot name an error")
                    error_variables = variables.new_child()
                    error_variables[statement.error_name] = Binding("String")
                    exits |= self._check_block(statement.else_body, error_variables, return_type, loop_depth,
                                               owner, method_name, async_allowed)
            elif isinstance(statement, IfStatement):
                self._require_same_type("Bool", self._expression_type(statement.condition, variables, owner), statement.condition.location)
                then_exits = self._check_block(statement.then_body, variables.new_child(), return_type, loop_depth,
                                               owner, method_name, async_allowed)
                else_exits = self._check_block(statement.else_body, variables.new_child(), return_type, loop_depth,
                                               owner, method_name, async_allowed)
                if isinstance(statement.condition, Literal):
                    exits = then_exits if statement.condition.value else else_exits
                else:
                    exits = then_exits | else_exits
            elif isinstance(statement, WhileStatement):
                self._require_same_type("Bool", self._expression_type(statement.condition, variables, owner), statement.condition.location)
                body_exits = self._check_block(statement.body, variables.new_child(), return_type, loop_depth + 1,
                                               owner, method_name, async_allowed)
                literal = isinstance(statement.condition, Literal)
                exits = set()
                if not literal or statement.condition.value:
                    exits |= body_exits & {"return"}
                if not literal or not statement.condition.value or "break" in body_exits:
                    exits.add("next")
            elif isinstance(statement, ForStatement):
                if statement.name in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                    raise Diagnostic(statement.location, "type", f"'{statement.name}' is reserved and cannot be a loop variable")
                iterable_type = self._expression_type(statement.iterable, variables, owner)
                iterable_base, iterable_arguments = split_type(iterable_type)
                if iterable_base == "List" and len(iterable_arguments) == 1:
                    element_type = iterable_arguments[0]
                elif iterable_base == "Map" and len(iterable_arguments) == 2:
                    element_type = iterable_arguments[0]
                else:
                    raise Diagnostic(statement.iterable.location, "type", f"for-loop value must be a List or Map, found {iterable_type}")
                loop_variables = variables.new_child()
                loop_variables[statement.name] = Binding(element_type)
                body_exits = self._check_block(statement.body, loop_variables, return_type, loop_depth + 1,
                                               owner, method_name, async_allowed)
                exits = {"next"} | (body_exits & {"return"})
            elif isinstance(statement, MatchStatement):
                subject_type = self._expression_type(statement.value, variables, owner)
                exits = set()
                seen: set[tuple[object, ...]] = set()
                coverage: set[str] = set()
                checked_patterns = []
                has_catchall = False
                for arm in statement.arms:
                    pattern = arm.pattern
                    if has_catchall:
                        raise Diagnostic(pattern.location, "type", "match arm is unreachable after a wildcard or binding pattern")
                    if self._patterns_are_exhaustive(subject_type, checked_patterns):
                        raise Diagnostic(pattern.location, "type", "match arm is unreachable after exhaustive patterns")
                    if isinstance(pattern, VariantPattern) and pattern.variant in coverage:
                        raise Diagnostic(pattern.location, "type", f"match arm for '{pattern.variant}' is unreachable")
                    if isinstance(pattern, EnumPattern) and pattern.variant in coverage:
                        raise Diagnostic(pattern.location, "type", f"match arm for '{pattern.variant}' is unreachable")
                    key = self._check_pattern(pattern, subject_type, variables)
                    if key in seen:
                        raise Diagnostic(pattern.location, "type", "duplicate match pattern")
                    if not self._pattern_is_useful(checked_patterns, pattern, subject_type):
                        raise Diagnostic(pattern.location, "type", "match arm is unreachable after previous patterns")
                    seen.add(key)
                    checked_patterns.append(pattern)
                    coverage = self._fully_covered_cases(subject_type, checked_patterns)
                    if key and key[0] == "catchall":
                        has_catchall = True
                    arm_variables = variables.new_child()
                    self._bind_pattern(pattern, subject_type, arm_variables)
                    exits |= self._check_block(arm.body, arm_variables, return_type, loop_depth,
                                               owner, method_name, async_allowed)
                exhaustive = self._patterns_are_exhaustive(subject_type, checked_patterns)
                if not exhaustive:
                    exits.add("next")
                self.match_coverage[statement.location] = frozenset(coverage)
                self.match_exhaustiveness[statement.location] = exhaustive
            elif isinstance(statement, LoopControl):
                if not loop_depth:
                    raise Diagnostic(statement.location, "type", f"'{statement.kind}' is only valid inside a loop")
                exits = {statement.kind}
            elif isinstance(statement, ExpressionStatement):
                self._expression_type(statement.expression, variables, owner)
            else:
                raise AssertionError(f"unhandled statement node: {type(statement).__name__}")
            if "next" in outcomes:
                outcomes = (outcomes - {"next"}) | exits
        return outcomes

    def _expression_type(
        self, expression: Expression, variables: ChainMap[str, Binding], owner: ClassInfo | None = None,
    ) -> str:
        result = self._infer_expression_type(expression, variables, owner)
        self.expression_types[expression.location] = result
        return result

    def _infer_expression_type(
        self, expression: Expression, variables: ChainMap[str, Binding], owner: ClassInfo | None = None,
    ) -> str:
        if isinstance(expression, Literal):
            if expression.type_name == "Int" and expression.value > INT_MAX:
                raise Diagnostic(expression.location, "type", "Int literal is outside the signed 64-bit range")
            if expression.type_name == "Float" and not math.isfinite(expression.value):
                raise Diagnostic(expression.location, "type", "Float literal is outside the supported range")
            return expression.type_name
        if isinstance(expression, TupleExpression):
            element_types = [self._expression_type(element, variables, owner) for element in expression.elements]
            return make_type("Tuple", element_types)
        if isinstance(expression, Name):
            binding = variables.get(expression.identifier)
            if binding is None:
                raise Diagnostic(expression.location, "type", f"unknown variable '{expression.identifier}'")
            if binding.type_name == "TextFile":
                raise Diagnostic(expression.location, "type", "a TextFile handle can only be used as the receiver of a scoped file operation")
            return binding.type_name
        if isinstance(expression, Call):
            if expression.callee == "openText":
                if not self._checking_using_resource:
                    raise Diagnostic(expression.location, "type", "openText resources must be acquired by a 'using' scope")
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'openText' does not accept type arguments")
                argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
                self._check_arguments(expression.arguments, argument_types, ("String", "String"),
                                      expression.location, "openText")
                return "Result<TextFile,String>"
            if expression.callee == "spawn":
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'spawn' does not accept type arguments")
                if len(expression.arguments) != 1 or not isinstance(expression.arguments[0], Call):
                    raise Diagnostic(expression.location, "type", "spawn expects one actor construction, for example spawn(Counter(0))")
                construction = expression.arguments[0]
                actor = self.actors.get(construction.callee)
                if actor is None:
                    raise Diagnostic(construction.location, "type", f"'{construction.callee}' is not an actor type")
                if construction.type_arguments:
                    raise Diagnostic(construction.location, "type", "actor types do not accept type arguments")
                constructor = actor.methods.get("init")
                expected = () if constructor is None else constructor.parameter_types
                argument_types = [self._expression_type(argument, variables, owner) for argument in construction.arguments]
                self._check_arguments(construction.arguments, argument_types, expected, construction.location,
                                      f"{construction.callee} actor constructor")
                return f"ActorRef<{construction.callee}>"
            if expression.callee in {"runActors", "runTasks"}:
                if owner is not None and isinstance(owner.declaration, ActorDeclaration):
                    raise Diagnostic(expression.location, "type", "scheduler pumps cannot be called inside actor handlers")
                if expression.type_arguments or expression.arguments:
                    raise Diagnostic(expression.location, "type", f"'{expression.callee}' expects no arguments")
                return "Result<Int,String>"
            if expression.callee == "taskGroup":
                if owner is not None and isinstance(owner.declaration, ActorDeclaration):
                    raise Diagnostic(expression.location, "type", "task groups cannot be created inside actor handlers")
                if expression.type_arguments or expression.arguments:
                    raise Diagnostic(expression.location, "type", "'taskGroup' expects no arguments")
                return "TaskGroup"
            argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
            if expression.callee == "oneForOne":
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'oneForOne' does not accept type arguments")
                self._check_arguments(expression.arguments, argument_types, ("Int", "Int"),
                                      expression.location, "oneForOne")
                return "Supervisor"
            if expression.callee in {"milliseconds", "seconds"}:
                self._check_arguments(expression.arguments, argument_types, ("Int",),
                                      expression.location, expression.callee)
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", f"'{expression.callee}' does not accept type arguments")
                return "Duration"
            if expression.callee == "timerAfter":
                self._check_arguments(expression.arguments, argument_types, ("Duration",),
                                      expression.location, "timerAfter")
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'timerAfter' does not accept type arguments")
                return "Timer"
            if expression.callee == "sleepFor":
                self._check_arguments(expression.arguments, argument_types, ("Duration",),
                                      expression.location, "sleepFor")
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'sleepFor' does not accept type arguments")
                return "Task<Void>"
            if expression.callee == "println":
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'println' does not accept type arguments")
                if len(argument_types) != 1:
                    raise Diagnostic(expression.location, "type", "'println' expects exactly one argument")
                if argument_types[0] not in _DISPLAYABLE_TYPES:
                    raise Diagnostic(expression.arguments[0].location, "type", f"'println' accepts Int, Float, Bool, or String, found {argument_types[0]}")
                return "Void"
            if expression.callee == "eprintln":
                self._check_output_call(expression, argument_types)
                return "Void"
            if expression.callee in {"readText", "writeText", "pathJoin", "pathExists"}:
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", f"'{expression.callee}' does not accept type arguments")
                signatures = {
                    "readText": (("String",), "Result<String,String>"),
                    "writeText": (("String", "String"), "Result<Bool,String>"),
                    "pathJoin": (("String", "String"), "String"),
                    "pathExists": (("String",), "Bool"),
                }
                parameter_types, result_type = signatures[expression.callee]
                self._check_arguments(expression.arguments, argument_types, parameter_types,
                                      expression.location, expression.callee)
                return result_type
            if expression.callee in {"parseJson", "jsonStringify", "jsonString", "jsonInt", "jsonFloat",
                                     "jsonBool", "jsonNull", "jsonArray", "jsonObject"}:
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", f"'{expression.callee}' does not accept type arguments")
                signatures = {
                    "parseJson": (("String",), "Result<Json,String>"),
                    "jsonStringify": (("Json",), "Result<String,String>"),
                    "jsonString": (("String",), "Json"),
                    "jsonInt": (("Int",), "Json"),
                    "jsonFloat": (("Float",), "Json"),
                    "jsonBool": (("Bool",), "Json"),
                    "jsonNull": ((), "Json"),
                    "jsonArray": (("List<Json>",), "Json"),
                    "jsonObject": (("Map<String,Json>",), "Json"),
                }
                parameter_types, result_type = signatures[expression.callee]
                self._check_arguments(expression.arguments, argument_types, parameter_types,
                                      expression.location, expression.callee)
                return result_type
            if expression.callee in {"List", "Map"}:
                arity = self.generic_arities[expression.callee]
                if len(expression.type_arguments) != arity:
                    raise Diagnostic(expression.location, "type", f"'{expression.callee}' expects {arity} type argument(s), got {len(expression.type_arguments)}")
                collection_type = make_type(expression.callee, [self._canonical_type(item) for item in expression.type_arguments])
                self._require_known_type(collection_type, expression.location)
                if expression.arguments:
                    raise Diagnostic(expression.location, "type", f"'{expression.callee}' currently constructs an empty collection and takes no value arguments")
                return collection_type
            if expression.callee in {"Some", "None", "Ok", "Err"}:
                constructor_types = {
                    "Some": (("T",), ("T",), "Option<T>"),
                    "None": (("T",), (), "Option<T>"),
                    "Ok": (("T", "E"), ("T",), "Result<T,E>"),
                    "Err": (("T", "E"), ("E",), "Result<T,E>"),
                }
                type_parameters, formal_types, result_type = constructor_types[expression.callee]
                substitutions = self._resolve_type_arguments(
                    type_parameters, expression.type_arguments, formal_types, argument_types,
                    expression.arguments, expression.location, expression.callee,
                )
                return substitute_type(result_type, substitutions)
            class_info = self.classes.get(expression.callee)
            if class_info is not None:
                constructor = class_info.methods.get("init")
                expected = () if constructor is None else constructor.parameter_types
                substitutions = self._resolve_type_arguments(
                    class_info.type_parameters, expression.type_arguments, expected, argument_types,
                    expression.arguments, expression.location, expression.callee,
                )
                return make_type(expression.callee, [substitutions[name] for name in class_info.type_parameters])
            record_info = self.records.get(expression.callee)
            if record_info is not None:
                formal_types = tuple(field.type_name for field in record_info.fields.values())
                substitutions = self._resolve_type_arguments(
                    record_info.type_parameters, expression.type_arguments, formal_types, argument_types,
                    expression.arguments, expression.location, expression.callee,
                )
                return make_type(expression.callee, [substitutions[name] for name in record_info.type_parameters])
            newtype = self.newtypes.get(expression.callee)
            if newtype is not None:
                substitutions = self._resolve_type_arguments(
                    tuple(newtype.type_parameters), expression.type_arguments,
                    (newtype.underlying_type,), argument_types, expression.arguments,
                    expression.location, expression.callee,
                )
                return make_type(expression.callee, [substitutions[name] for name in newtype.type_parameters])
            if expression.callee in self.actors:
                raise Diagnostic(expression.location, "type", f"actor '{expression.callee}' can only be created with spawn({expression.callee}(...))")
            signature = self.signatures.get(expression.callee)
            if signature is None:
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", f"unknown generic function '{expression.callee}'")
                if expression.callee in self.interfaces:
                    raise Diagnostic(expression.location, "type", "interfaces cannot be constructed")
                raise Diagnostic(expression.location, "type", f"unknown function '{expression.callee}'")
            substitutions = self._resolve_type_arguments(
                signature.type_parameters, expression.type_arguments, signature.parameter_types, argument_types,
                expression.arguments, expression.location, expression.callee, signature.type_constraints,
            )
            result_type = substitute_type(signature.return_type, substitutions)
            return make_type("Task", [result_type]) if signature.is_async else result_type
        if isinstance(expression, MemberAccess):
            if isinstance(expression.receiver, Name) and expression.receiver.identifier == "secrets":
                field_type, _ = self._member_field(expression, variables, owner)
                return field_type
            if isinstance(expression.receiver, Name) and expression.receiver.identifier in self.enums:
                enum = self.enums[expression.receiver.identifier]
                payload_types = enum.variants.get(expression.member)
                if payload_types is None:
                    raise Diagnostic(expression.location, "type", f"enum '{enum.declaration.name}' has no variant '{expression.member}'")
                if payload_types:
                    raise Diagnostic(expression.location, "type", f"variant '{expression.member}' requires constructor arguments")
                if enum.type_parameters:
                    raise Diagnostic(expression.location, "type", "generic enum variants require an explicit constructor call with type arguments")
                return enum.declaration.name
            receiver_type = self._expression_type(expression.receiver, variables, owner)
            receiver_base, receiver_arguments = split_type(receiver_type)
            if receiver_base == "Tuple":
                if not expression.member.isdecimal():
                    raise Diagnostic(expression.location, "type", f"tuple element access must use a numeric index, found '{expression.member}'")
                index = int(expression.member)
                if index >= len(receiver_arguments):
                    raise Diagnostic(expression.location, "type", f"tuple index {index} is out of range for {receiver_type}")
                return receiver_arguments[index]
            newtype = self.newtypes.get(receiver_base)
            if newtype is not None and expression.member == "value":
                substitutions = dict(zip(newtype.type_parameters, receiver_arguments))
                return self._canonical_type(substitute_type(newtype.underlying_type, substitutions))
            field_type, _ = self._member_field(expression, variables, owner)
            return field_type
        if isinstance(expression, MemberCall):
            if isinstance(expression.receiver, Name) and expression.receiver.identifier in self.enums:
                enum = self.enums[expression.receiver.identifier]
                payload_types = enum.variants.get(expression.method)
                if payload_types is None:
                    raise Diagnostic(expression.location, "type", f"enum '{enum.declaration.name}' has no variant '{expression.method}'")
                argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
                substitutions = self._resolve_type_arguments(
                    enum.type_parameters, expression.type_arguments, payload_types, argument_types,
                    expression.arguments, expression.location, f"{enum.declaration.name}.{expression.method}",
                )
                return make_type(enum.declaration.name, [substitutions[name] for name in enum.type_parameters])
            if expression.type_arguments:
                raise Diagnostic(expression.location, "type", "generic methods are not supported yet")
            resource_binding = (variables.get(expression.receiver.identifier)
                                if isinstance(expression.receiver, Name) else None)
            if resource_binding is not None and resource_binding.type_name == "TextFile":
                receiver_type = "TextFile"
            else:
                receiver_type = self._expression_type(expression.receiver, variables, owner)
            if expression.method == "init":
                raise Diagnostic(expression.location, "type", "constructors can only run when creating an instance")
            receiver_base, receiver_arguments = split_type(receiver_type)
            if receiver_base == "TextFile":
                argument_types = [self._expression_type(argument, variables, owner)
                                  for argument in expression.arguments]
                methods = {
                    "readAll": ((), "Result<String,String>"),
                    "write": (("String",), "Result<Bool,String>"),
                    "close": ((), "Bool"),
                    "isOpen": ((), "Bool"),
                }
                method_signature = methods.get(expression.method)
                if method_signature is None:
                    raise Diagnostic(expression.location, "type", f"TextFile has no method '{expression.method}'")
                expected_types, result_type = method_signature
                self._check_arguments(expression.arguments, argument_types, expected_types,
                                      expression.location, f"TextFile.{expression.method}")
                return result_type
            if receiver_base == "Supervisor":
                if receiver_arguments:
                    raise Diagnostic(expression.location, "type", "Supervisor does not accept type arguments")
                if expression.method == "start":
                    if len(expression.arguments) != 1 or not isinstance(expression.arguments[0], Call):
                        raise Diagnostic(expression.location, "type", "Supervisor.start expects one actor construction, for example supervisor.start(Counter(0))")
                    construction = expression.arguments[0]
                    actor = self.actors.get(construction.callee)
                    if actor is None:
                        raise Diagnostic(construction.location, "type", f"'{construction.callee}' is not an actor type")
                    if construction.type_arguments:
                        raise Diagnostic(construction.location, "type", "actor types do not accept type arguments")
                    constructor = actor.methods.get("init")
                    expected = () if constructor is None else constructor.parameter_types
                    argument_types = [self._expression_type(argument, variables, owner)
                                      for argument in construction.arguments]
                    self._check_arguments(construction.arguments, argument_types, expected,
                                          construction.location, f"{construction.callee} actor constructor")
                    return f"ActorRef<{construction.callee}>"
                if expression.method in {"stop", "isAlive"}:
                    argument_types = [self._expression_type(argument, variables, owner)
                                      for argument in expression.arguments]
                    self._check_arguments(expression.arguments, argument_types, (), expression.location,
                                          f"Supervisor.{expression.method}")
                    return "Void" if expression.method == "stop" else "Bool"
                raise Diagnostic(expression.location, "type", f"Supervisor has no method '{expression.method}'")
            if receiver_base == "ActorRef" and expression.method in {"request", "requestWithin", "sendAfter"}:
                if len(receiver_arguments) != 1 or receiver_arguments[0] not in self.actors:
                    raise Diagnostic(expression.location, "type", "invalid actor reference type")
                scheduled = expression.method == "sendAfter"
                timed = expression.method in {"requestWithin", "sendAfter"}
                prefix = 1 if timed else 0
                minimum = prefix + 1
                if len(expression.arguments) < minimum:
                    example = (
                        "worker.request(get)" if not timed
                        else "worker.sendAfter(milliseconds(500), tick)" if scheduled
                        else "worker.requestWithin(milliseconds(500), get)"
                    )
                    raise Diagnostic(expression.location, "type", f"ActorRef.{expression.method} expects a handler name and its arguments, for example {example}")
                if timed:
                    duration_type = self._expression_type(expression.arguments[0], variables, owner)
                    self._check_arguments(expression.arguments[:1], [duration_type], ("Duration",),
                                          expression.location, f"ActorRef.{expression.method}")
                selector = expression.arguments[prefix]
                if not isinstance(selector, Name):
                    raise Diagnostic(selector.location, "type", "actor message handler must be named directly, without a value")
                actor = self.actors[receiver_arguments[0]]
                signature = actor.methods.get(selector.identifier)
                if signature is None or selector.identifier == "init":
                    raise Diagnostic(selector.location, "type", f"actor '{actor.declaration.name}' has no message handler '{selector.identifier}'")
                if scheduled:
                    if signature.parameter_types and split_type(signature.parameter_types[-1])[0] == "ReplySender":
                        raise Diagnostic(selector.location, "type", "sendAfter cannot schedule a request handler that requires ReplySender<T>")
                    expected_types = signature.parameter_types
                else:
                    if not signature.parameter_types:
                        raise Diagnostic(selector.location, "type", f"handler '{selector.identifier}' must end with ReplySender<T> to be requested")
                    reply_base, reply_arguments = split_type(signature.parameter_types[-1])
                    if reply_base != "ReplySender" or len(reply_arguments) != 1:
                        raise Diagnostic(selector.location, "type", f"handler '{selector.identifier}' must end with ReplySender<T> to be requested")
                    expected_types = signature.parameter_types[:-1]
                supplied_arguments = expression.arguments[prefix + 1:]
                supplied_types = [self._expression_type(argument, variables, owner) for argument in supplied_arguments]
                self._check_arguments(supplied_arguments, supplied_types, expected_types,
                                      expression.location, f"{actor.declaration.name}.{selector.identifier} message")
                if scheduled:
                    return "Timer"
                return make_type("ReplyReceiver", [reply_arguments[0]])
            argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
            if receiver_base == "Secret" and expression.method == "reveal":
                if expression.arguments:
                    raise Diagnostic(expression.location, "type", "Secret.reveal() expects no arguments")
                if len(receiver_arguments) != 1:
                    raise Diagnostic(expression.location, "type", "Secret requires one type argument")
                return receiver_arguments[0]
            if receiver_base == "ReplySender" and expression.method == "send":
                if len(receiver_arguments) != 1:
                    raise Diagnostic(expression.location, "type", "ReplySender requires one type argument")
                self._check_arguments(expression.arguments, argument_types, (receiver_arguments[0],),
                                      expression.location, "ReplySender.send")
                return "Bool"
            if receiver_base == "ReplyReceiver":
                if len(receiver_arguments) != 1:
                    raise Diagnostic(expression.location, "type", "ReplyReceiver requires one type argument")
                methods = {
                    "isReady": ((), "Bool"),
                    "isCancelled": ((), "Bool"),
                    "isTimedOut": ((), "Bool"),
                    "cancel": ((), "Bool"),
                    "tryReceive": ((), make_type("Result", [make_type("Option", [receiver_arguments[0]]), "String"])),
                }
                method_signature = methods.get(expression.method)
                if method_signature is None:
                    raise Diagnostic(expression.location, "type", f"ReplyReceiver<{receiver_arguments[0]}> has no method '{expression.method}'")
                expected_types, result_type = method_signature
                self._check_arguments(expression.arguments, argument_types, expected_types, expression.location,
                                      f"ReplyReceiver.{expression.method}")
                return result_type
            if receiver_base == "Task":
                if len(receiver_arguments) != 1:
                    raise Diagnostic(expression.location, "type", "Task requires one type argument")
                task_value_type = self._canonical_type(receiver_arguments[0])
                methods = {
                    "isDone": ((), "Bool"),
                    "isCancelled": ((), "Bool"),
                    "cancel": ((), "Bool"),
                    "detach": ((), "Bool"),
                    "withTimeout": (("Duration",), make_type("Task", [task_value_type])),
                }
                methods["tryResult"] = (
                    (), "Result<Bool,String>" if task_value_type == "Void"
                    else make_type("Result", [make_type("Option", [task_value_type]), "String"]),
                )
                method_signature = methods.get(expression.method)
                if method_signature is None:
                    raise Diagnostic(expression.location, "type", f"Task has no method '{expression.method}'")
                expected_types, result_type = method_signature
                self._check_arguments(expression.arguments, argument_types, expected_types, expression.location,
                                      f"Task.{expression.method}")
                return result_type
            if receiver_base == "TaskGroup":
                if receiver_arguments:
                    raise Diagnostic(expression.location, "type", "TaskGroup does not accept type arguments")
                if expression.method == "add":
                    if len(argument_types) != 1:
                        raise Diagnostic(expression.location, "type", "TaskGroup.add expects one Task argument")
                    task_base, task_arguments = split_type(argument_types[0])
                    if task_base != "Task" or len(task_arguments) != 1:
                        raise Diagnostic(expression.arguments[0].location, "type", f"TaskGroup.add expects Task<T>, found {argument_types[0]}")
                    return "Bool"
                methods = {"isDone": ((), "Bool"), "cancel": ((), "Int")}
                method_signature = methods.get(expression.method)
                if method_signature is None:
                    raise Diagnostic(expression.location, "type", f"TaskGroup has no method '{expression.method}'")
                expected_types, result_type = method_signature
                self._check_arguments(expression.arguments, argument_types, expected_types, expression.location,
                                      f"TaskGroup.{expression.method}")
                return result_type
            if receiver_base == "Duration" and expression.method == "inMilliseconds":
                self._check_arguments(expression.arguments, argument_types, (), expression.location,
                                      "Duration.inMilliseconds")
                return "Int"
            if receiver_base == "Timer":
                methods = {
                    "isReady": ((), "Bool"),
                    "isCancelled": ((), "Bool"),
                    "cancel": ((), "Bool"),
                    "remaining": ((), "Duration"),
                }
                method_signature = methods.get(expression.method)
                if method_signature is None:
                    raise Diagnostic(expression.location, "type", f"Timer has no method '{expression.method}'")
                expected_types, result_type = method_signature
                self._check_arguments(expression.arguments, argument_types, expected_types, expression.location,
                                      f"Timer.{expression.method}")
                return result_type
            if receiver_base == "ActorRef":
                if len(receiver_arguments) != 1 or receiver_arguments[0] not in self.actors:
                    raise Diagnostic(expression.location, "type", "invalid actor reference type")
                actor = self.actors[receiver_arguments[0]]
                if expression.method == "isAlive":
                    self._check_arguments(expression.arguments, argument_types, (), expression.location,
                                          "ActorRef.isAlive")
                    return "Bool"
                if expression.method == "stop":
                    self._check_arguments(expression.arguments, argument_types, (), expression.location,
                                          "ActorRef.stop")
                    return "Void"
                signature = actor.methods.get(expression.method)
                if signature is None or expression.method == "init":
                    raise Diagnostic(expression.location, "type", f"actor '{actor.declaration.name}' has no message handler '{expression.method}'")
                if any(split_type(argument_type)[0] == "ReplySender" for argument_type in argument_types):
                    raise Diagnostic(expression.location, "type", "ReplySender values cannot be forwarded as ordinary messages; issue a typed request instead")
                self._check_arguments(expression.arguments, argument_types, signature.parameter_types,
                                      expression.location, f"{actor.declaration.name}.{expression.method} message")
                return "Void"
            if receiver_base == "Json":
                methods = {
                    "kind": ((), "String"),
                    "isNull": ((), "Bool"),
                    "asString": ((), "Option<String>"),
                    "asInt": ((), "Option<Int>"),
                    "asFloat": ((), "Option<Float>"),
                    "asBool": ((), "Option<Bool>"),
                    "arrayLength": ((), "Option<Int>"),
                    "objectLength": ((), "Option<Int>"),
                    "arrayAt": (("Int",), "Option<Json>"),
                    "objectGet": (("String",), "Option<Json>"),
                    "objectKeys": ((), "Option<List<String>>"),
                }
                method_signature = methods.get(expression.method)
                if method_signature is None:
                    raise Diagnostic(expression.location, "type", f"Json has no method '{expression.method}'")
                expected_types, result_type = method_signature
                self._check_arguments(expression.arguments, argument_types, expected_types, expression.location,
                                      f"Json.{expression.method}")
                return result_type
            if receiver_base in {"List", "Map"}:
                signature = self._collection_method(receiver_base, receiver_arguments, expression.method, expression.location)
                substitutions = {}
            elif receiver_base in self.classes:
                class_info = self.classes[receiver_base]
                signature = class_info.methods.get(expression.method)
                if receiver_arguments:
                    substitutions = dict(zip(class_info.type_parameters, receiver_arguments))
                else:
                    substitutions = {name: name for name in class_info.type_parameters}
            elif receiver_base in self.actors:
                actor = self.actors[receiver_base]
                signature = actor.methods.get(expression.method)
                substitutions = {}
            elif receiver_base in self.interfaces:
                signature = self.interfaces[receiver_base].methods.get(expression.method)
                substitutions = {}
            elif receiver_base in self.active_type_constraints:
                candidates = [self.interfaces[interface_name].methods[expression.method]
                              for interface_name in self.active_type_constraints[receiver_base]
                              if expression.method in self.interfaces[interface_name].methods]
                if candidates and any(
                    candidate.parameter_types != candidates[0].parameter_types
                    or candidate.return_type != candidates[0].return_type
                    for candidate in candidates[1:]
                ):
                    raise Diagnostic(expression.location, "type", f"constraints on '{receiver_base}' require incompatible '{expression.method}' signatures")
                signature = candidates[0] if candidates else None
                substitutions = {}
            else:
                signature = None
                substitutions = {}
            if signature is None:
                if receiver_base in self.active_type_constraints:
                    raise Diagnostic(expression.location, "type", f"type parameter '{receiver_base}' has no constrained method '{expression.method}'")
                if receiver_base in self.active_type_variables:
                    raise Diagnostic(expression.location, "type", f"type parameter '{receiver_base}' has no interface constraint providing method '{expression.method}'")
                raise Diagnostic(expression.location, "type", f"type '{receiver_type}' has no method '{expression.method}'")
            parameter_types = tuple(substitute_type(item, substitutions) for item in signature.parameter_types)
            self._check_arguments(expression.arguments, argument_types, parameter_types, expression.location,
                                  f"{receiver_type}.{expression.method}")
            return substitute_type(signature.return_type, substitutions)
        if isinstance(expression, Unary):
            if (
                expression.operator == "-"
                and isinstance(expression.operand, Literal)
                and expression.operand.type_name == "Int"
                and expression.operand.value == INT_MAX + 1
            ):
                return "Int"
            operand_type = self._expression_type(expression.operand, variables, owner)
            if expression.operator == "!":
                if operand_type != "Bool":
                    raise Diagnostic(expression.location, "type", f"operator '!' expects Bool, found {operand_type}")
                return "Bool"
            if operand_type not in _NUMERIC_TYPES:
                raise Diagnostic(expression.location, "type", f"unary '-' expects a number, found {operand_type}")
            return operand_type
        if isinstance(expression, Binary):
            left_type = self._expression_type(expression.left, variables, owner)
            right_type = self._expression_type(expression.right, variables, owner)
            return self._binary_type(expression, left_type, right_type)
        raise AssertionError(f"unhandled expression node: {type(expression).__name__}")

    def _member_field(
        self, expression: MemberAccess, variables: ChainMap[str, Binding], owner: ClassInfo | None,
    ) -> tuple[str, Field]:
        if isinstance(expression.receiver, Name) and expression.receiver.identifier == "secrets":
            if not expression.member[0].isupper() or expression.member != expression.member.upper():
                raise Diagnostic(expression.location, "type", "secret names must use uppercase letters, digits, and underscores")
            return "Option<Secret<String>>", Field(expression.member, "Option<Secret<String>>", False, expression.location)
        receiver_type = self._expression_type(expression.receiver, variables, owner)
        receiver_base, receiver_arguments = split_type(receiver_type)
        if receiver_base in {"List", "Map"} and expression.member == "length":
            return "Int", Field("length", "Int", False, expression.location)
        class_info = self.classes.get(receiver_base) or self.actors.get(receiver_base)
        record_info = self.records.get(receiver_base)
        if record_info is not None:
            field = record_info.fields.get(expression.member)
            if field is None:
                raise Diagnostic(expression.location, "type", f"type '{receiver_type}' has no field '{expression.member}'")
            substitutions = dict(zip(record_info.type_parameters, receiver_arguments))
            return self._canonical_type(substitute_type(field.type_name, substitutions)), field
        field = None if class_info is None else class_info.fields.get(expression.member)
        if field is None:
            raise Diagnostic(expression.location, "type", f"type '{receiver_type}' has no field '{expression.member}'")
        if receiver_arguments:
            substitutions = dict(zip(class_info.type_parameters, receiver_arguments))
        elif class_info is not None:
            substitutions = {name: name for name in class_info.type_parameters}
        else:
            substitutions = {}
        return self._canonical_type(substitute_type(field.type_name, substitutions)), field

    def _check_output_call(self, expression: Call, argument_types: list[str]) -> None:
        if expression.type_arguments:
            raise Diagnostic(expression.location, "type", f"'{expression.callee}' does not accept type arguments")
        if len(argument_types) != 1:
            raise Diagnostic(expression.location, "type", f"'{expression.callee}' expects exactly one argument")
        if argument_types[0] not in _DISPLAYABLE_TYPES:
            raise Diagnostic(expression.arguments[0].location, "type", f"'{expression.callee}' accepts Int, Float, Bool, or String, found {argument_types[0]}")

    def _resolve_type_arguments(
        self, type_parameters: tuple[str, ...], explicit: list[str], formal_types: tuple[str, ...],
        actual_types: list[str], arguments: list[Expression], location: Location, callee: str,
        type_constraints: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> dict[str, str]:
        if len(actual_types) != len(formal_types):
            raise Diagnostic(location, "type", f"'{callee}' expects {len(formal_types)} argument(s), got {len(actual_types)}")
        if not type_parameters:
            if explicit:
                raise Diagnostic(location, "type", f"'{callee}' does not accept type arguments")
            self._check_arguments(arguments, actual_types, formal_types, location, callee)
            return {}

        substitutions: dict[str, str] = {}
        if explicit:
            if len(explicit) != len(type_parameters):
                raise Diagnostic(location, "type", f"'{callee}' expects {len(type_parameters)} type argument(s), got {len(explicit)}")
            for parameter, type_name in zip(type_parameters, explicit):
                self._require_known_type(type_name, location)
                if self._canonical_type(type_name) == "Void":
                    raise Diagnostic(location, "type", "type arguments cannot be Void")
                substitutions[parameter] = self._canonical_type(type_name)
        else:
            allowed = set(type_parameters)
            for formal, actual in zip(formal_types, actual_types):
                self._infer_type_arguments(formal, actual, allowed, substitutions, location, callee)
            missing = [name for name in type_parameters if name not in substitutions]
            if missing:
                raise Diagnostic(location, "type", f"cannot infer type argument '{missing[0]}' for '{callee}'; provide it explicitly")

        resolved_parameters = tuple(substitute_type(item, substitutions) for item in formal_types)
        self._check_arguments(arguments, actual_types, resolved_parameters, location, callee)
        self._check_generic_constraints(type_constraints, substitutions, location, callee)
        self.generic_substitutions[location] = dict(substitutions)
        return substitutions

    def _check_generic_constraints(
        self,
        constraints: tuple[tuple[str, tuple[str, ...]], ...],
        substitutions: dict[str, str],
        location: Location,
        callee: str,
    ) -> None:
        for parameter, bounds in constraints:
            actual = substitutions.get(parameter)
            if actual is None:
                raise Diagnostic(location, "type", f"cannot validate constraints for uninferred type parameter '{parameter}'")
            for interface_name in bounds:
                if not self._satisfies_interface_constraint(actual, interface_name):
                    raise Diagnostic(
                        location, "type",
                        f"type '{actual}' does not satisfy constraint '{interface_name}' for '{parameter}' in call to '{callee}'",
                    )

    def _satisfies_interface_constraint(self, type_name: str, interface_name: str) -> bool:
        type_name = self._canonical_type(type_name)
        base, _ = split_type(type_name)
        if base == interface_name:
            return True
        class_info = self.classes.get(base)
        if class_info is not None and interface_name in class_info.declaration.interfaces:
            return True
        return interface_name in self.active_type_constraints.get(base, ())

    def _collection_method(
        self, collection: str, type_arguments: tuple[str, ...], method: str, location: Location,
    ) -> Signature:
        expected_arity = self.generic_arities[collection]
        if len(type_arguments) != expected_arity:
            raise Diagnostic(location, "type", f"type '{collection}' expects {expected_arity} type argument(s)")
        if collection == "List":
            element = type_arguments[0]
            signatures = {
                "append": (("T",), "Void"),
                "insert": (("Int", "T"), "Void"),
                "get": (("Int",), "Option<T>"),
                "set": (("Int", "T"), "Void"),
                "removeAt": (("Int",), "Option<T>"),
                "contains": (("T",), "Bool"),
                "clear": ((), "Void"),
            }
            signature = signatures.get(method)
            if signature is None:
                raise Diagnostic(location, "type", f"type '{make_type(collection, list(type_arguments))}' has no method '{method}'")
            params, result = signature
            substitutions = {"T": element}
        else:
            key_type, value_type = type_arguments
            signatures = {
                "put": (("K", "V"), "Void"),
                "get": (("K",), "Option<V>"),
                "remove": (("K",), "Option<V>"),
                "containsKey": (("K",), "Bool"),
                "keys": ((), "List<K>"),
                "values": ((), "List<V>"),
                "clear": ((), "Void"),
            }
            signature = signatures.get(method)
            if signature is None:
                raise Diagnostic(location, "type", f"type '{make_type(collection, list(type_arguments))}' has no method '{method}'")
            params, result = signature
            substitutions = {"K": key_type, "V": value_type}
        return Signature(tuple(substitute_type(item, substitutions) for item in params),
                         substitute_type(result, substitutions), location)

    def _check_pattern(self, pattern, subject_type: str, variables: ChainMap[str, Binding]) -> tuple[object, ...]:
        if isinstance(pattern, WildcardPattern):
            return ("catchall",)
        if isinstance(pattern, BindingPattern):
            if pattern.name in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                raise Diagnostic(pattern.location, "type", f"'{pattern.name}' is reserved and cannot be a pattern binding")
            return ("catchall",)
        if isinstance(pattern, LiteralPattern):
            if pattern.type_name == "Int" and not INT_MIN <= pattern.value <= INT_MAX:
                raise Diagnostic(pattern.location, "type", "Int pattern is outside the signed 64-bit range")
            if pattern.type_name != subject_type:
                raise Diagnostic(pattern.location, "type", f"pattern has type {pattern.type_name}, but match value has type {subject_type}")
            return ("literal", pattern.type_name, pattern.value)
        if isinstance(pattern, VariantPattern):
            base, arguments = split_type(subject_type)
            if base == "Option" and len(arguments) == 1:
                if pattern.variant not in {"Some", "None"}:
                    raise Diagnostic(pattern.location, "type", f"Option<{arguments[0]}> has no pattern '{pattern.variant}'")
                if pattern.variant == "None" and pattern.binding is not None:
                    raise Diagnostic(pattern.location, "type", "None pattern does not bind a value")
                payload_type = arguments[0]
            elif base == "Result" and len(arguments) == 2:
                if pattern.variant not in {"Ok", "Err"}:
                    raise Diagnostic(pattern.location, "type", f"Result<{arguments[0]},{arguments[1]}> has no pattern '{pattern.variant}'")
                payload_type = arguments[0] if pattern.variant == "Ok" else arguments[1]
            else:
                raise Diagnostic(pattern.location, "type", f"variant patterns require Option or Result, found {subject_type}")
            if pattern.binding in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                raise Diagnostic(pattern.location, "type", f"'{pattern.binding}' is reserved and cannot be a pattern binding")
            payload_pattern = pattern.payload_pattern
            if pattern.variant == "None":
                return ("variant", pattern.variant, True)
            if payload_pattern is None:
                return ("variant", pattern.variant, pattern.binding is not None)
            payload_key = self._check_pattern(payload_pattern, payload_type, variables)
            return ("variant", pattern.variant, self._pattern_is_irrefutable(payload_pattern), payload_key)
        if isinstance(pattern, TuplePattern):
            base, arguments = split_type(subject_type)
            if base != "Tuple" or len(arguments) != len(pattern.elements):
                raise Diagnostic(pattern.location, "type", f"tuple pattern with {len(pattern.elements)} elements does not match {subject_type}")
            keys = tuple(self._check_pattern(child, child_type, variables)
                         for child, child_type in zip(pattern.elements, arguments))
            if self._pattern_is_irrefutable(pattern):
                return ("catchall", "tuple", keys)
            return ("tuple", keys)
        if isinstance(pattern, RecordPattern):
            base, arguments = split_type(subject_type)
            record = self.records.get(pattern.name)
            newtype = self.newtypes.get(pattern.name)
            if newtype is not None:
                if base != pattern.name:
                    raise Diagnostic(pattern.location, "type", f"newtype pattern '{pattern.name}' does not match {subject_type}")
                if len(pattern.fields) != 1:
                    raise Diagnostic(pattern.location, "type", f"newtype pattern '{pattern.name}' expects one payload pattern")
                substitutions = dict(zip(newtype.type_parameters, arguments))
                payload_type = self._canonical_type(substitute_type(newtype.underlying_type, substitutions))
                payload_key = self._check_pattern(pattern.fields[0], payload_type, variables)
                if self._pattern_is_irrefutable(pattern):
                    return ("catchall", "newtype", pattern.name, payload_key)
                return ("newtype", pattern.name, payload_key)
            if record is None:
                raise Diagnostic(pattern.location, "type", f"'{pattern.name}' is not a record or newtype")
            if base != pattern.name:
                raise Diagnostic(pattern.location, "type", f"record pattern '{pattern.name}' does not match {subject_type}")
            if len(pattern.fields) != len(record.fields):
                raise Diagnostic(pattern.location, "type", f"record pattern '{pattern.name}' expects {len(record.fields)} field pattern(s), got {len(pattern.fields)}")
            substitutions = dict(zip(record.type_parameters, arguments))
            field_types = [self._canonical_type(substitute_type(field.type_name, substitutions))
                           for field in record.fields.values()]
            keys = tuple(self._check_pattern(child, child_type, variables)
                         for child, child_type in zip(pattern.fields, field_types))
            if self._pattern_is_irrefutable(pattern):
                return ("catchall", "record", pattern.name, keys)
            return ("record", pattern.name, keys)
        if isinstance(pattern, EnumPattern):
            base, arguments = split_type(subject_type)
            enum = self.enums.get(pattern.enum_name)
            if enum is None:
                raise Diagnostic(pattern.location, "type", f"'{pattern.enum_name}' is not an enum type")
            if base != pattern.enum_name:
                raise Diagnostic(pattern.location, "type", f"enum pattern '{pattern.enum_name}.{pattern.variant}' does not match {subject_type}")
            payload_types = enum.variants.get(pattern.variant)
            if payload_types is None:
                raise Diagnostic(pattern.location, "type", f"enum '{pattern.enum_name}' has no variant '{pattern.variant}'")
            if len(pattern.payload_patterns) != len(payload_types):
                raise Diagnostic(pattern.location, "type", f"variant '{pattern.variant}' expects {len(payload_types)} payload pattern(s), got {len(pattern.payload_patterns)}")
            substitutions = dict(zip(enum.type_parameters, arguments))
            child_types = [self._canonical_type(substitute_type(item, substitutions)) for item in payload_types]
            child_keys = tuple(self._check_pattern(child, child_type, variables)
                               for child, child_type in zip(pattern.payload_patterns, child_types))
            complete_variant = all(self._pattern_is_irrefutable(child) for child in pattern.payload_patterns)
            return ("enum", pattern.variant, complete_variant, child_keys)
        raise AssertionError(f"unhandled pattern node: {type(pattern).__name__}")

    @staticmethod
    def _pattern_is_irrefutable(pattern) -> bool:
        if isinstance(pattern, (BindingPattern, WildcardPattern)):
            return True
        if isinstance(pattern, TuplePattern):
            return all(TypeChecker._pattern_is_irrefutable(element) for element in pattern.elements)
        if isinstance(pattern, RecordPattern):
            return all(TypeChecker._pattern_is_irrefutable(element) for element in pattern.fields)
        return False

    def _bind_pattern(self, pattern, subject_type: str, variables: ChainMap[str, Binding]) -> None:
        if isinstance(pattern, BindingPattern):
            if pattern.name in variables.maps[0]:
                raise Diagnostic(pattern.location, "type", f"pattern binding '{pattern.name}' is declared more than once")
            variables[pattern.name] = Binding(subject_type)
            return
        if isinstance(pattern, TuplePattern):
            _, arguments = split_type(subject_type)
            for child, child_type in zip(pattern.elements, arguments):
                self._bind_pattern(child, child_type, variables)
            return
        if isinstance(pattern, RecordPattern):
            _, arguments = split_type(subject_type)
            newtype = self.newtypes.get(pattern.name)
            if newtype is not None:
                substitutions = dict(zip(newtype.type_parameters, arguments))
                child_type = self._canonical_type(substitute_type(newtype.underlying_type, substitutions))
                self._bind_pattern(pattern.fields[0], child_type, variables)
                return
            record = self.records[pattern.name]
            substitutions = dict(zip(record.type_parameters, arguments))
            for child, field in zip(pattern.fields, record.fields.values()):
                child_type = self._canonical_type(substitute_type(field.type_name, substitutions))
                self._bind_pattern(child, child_type, variables)
            return
        if isinstance(pattern, VariantPattern):
            if pattern.variant == "None":
                return
            payload_type = self._variant_payload_type(pattern, subject_type)
            if pattern.payload_pattern is not None:
                self._bind_pattern(pattern.payload_pattern, payload_type, variables)
            elif pattern.binding is not None:
                if pattern.binding in variables.maps[0]:
                    raise Diagnostic(pattern.location, "type", f"pattern binding '{pattern.binding}' is declared more than once")
                variables[pattern.binding] = Binding(payload_type)
            return
        if isinstance(pattern, EnumPattern):
            _, arguments = split_type(subject_type)
            enum = self.enums[pattern.enum_name]
            substitutions = dict(zip(enum.type_parameters, arguments))
            payload_types = enum.variants[pattern.variant]
            for child, payload_type in zip(pattern.payload_patterns, payload_types):
                self._bind_pattern(child, self._canonical_type(substitute_type(payload_type, substitutions)), variables)

    @staticmethod
    def _variant_payload_type(pattern: VariantPattern, subject_type: str) -> str:
        base, arguments = split_type(subject_type)
        if base == "Option":
            return arguments[0]
        return arguments[0] if pattern.variant == "Ok" else arguments[1]

    def _finite_constructors(self, type_name: str):
        type_name = self._canonical_type(type_name)
        base, arguments = split_type(type_name)
        if base == "Bool":
            return [(("literal", "Bool", value), ()) for value in (True, False)]
        if base == "Option" and len(arguments) == 1:
            return [(('variant', 'Some'), (arguments[0],)), (('variant', 'None'), ())]
        if base == "Result" and len(arguments) == 2:
            return [(('variant', 'Ok'), (arguments[0],)), (('variant', 'Err'), (arguments[1],))]
        enum = self.enums.get(base)
        if enum is not None:
            substitutions = dict(zip(enum.type_parameters, arguments))
            return [(("enum", base, variant), tuple(
                self._canonical_type(substitute_type(payload, substitutions)) for payload in payloads
            )) for variant, payloads in enum.variants.items()]
        if base == "Tuple" and len(arguments) >= 2:
            return [(('tuple', base), tuple(arguments))]
        record = self.records.get(base)
        if record is not None:
            substitutions = dict(zip(record.type_parameters, arguments))
            fields = tuple(self._canonical_type(substitute_type(field.type_name, substitutions))
                           for field in record.fields.values())
            return [(('record', base), fields)]
        newtype = self.newtypes.get(base)
        if newtype is not None:
            substitutions = dict(zip(newtype.type_parameters, arguments))
            payload = self._canonical_type(substitute_type(newtype.underlying_type, substitutions))
            return [(('newtype', base), (payload,))]
        if base == "Void":
            return []
        return None

    def _pattern_head(self, pattern, type_name: str):
        if isinstance(pattern, (BindingPattern, WildcardPattern)):
            return None
        if isinstance(pattern, LiteralPattern):
            return ("literal", pattern.type_name, pattern.value), ()
        base, _ = split_type(self._canonical_type(type_name))
        if isinstance(pattern, VariantPattern):
            if base == "Option":
                tag = ("variant", pattern.variant)
                if pattern.variant == "None":
                    return tag, ()
            elif base == "Result":
                tag = ("variant", pattern.variant)
            else:
                return None
            child = pattern.payload_pattern
            if child is None:
                child = (BindingPattern(pattern.binding, pattern.location) if pattern.binding is not None
                         else WildcardPattern(pattern.location))
            return tag, (child,)
        if isinstance(pattern, EnumPattern):
            return ("enum", pattern.enum_name, pattern.variant), tuple(pattern.payload_patterns)
        if isinstance(pattern, TuplePattern):
            return ("tuple", base), tuple(pattern.elements)
        if isinstance(pattern, RecordPattern):
            if pattern.name in self.newtypes:
                return ("newtype", base), tuple(pattern.fields)
            return ("record", base), tuple(pattern.fields)
        return None

    def _specialize_matrix(self, matrix, type_name: str, tag, argument_types: tuple[str, ...]):
        specialized = []
        for row in matrix:
            head, *tail = row
            if isinstance(head, (BindingPattern, WildcardPattern)):
                payloads = [WildcardPattern(head.location) for _ in argument_types]
            else:
                info = self._pattern_head(head, type_name)
                if info is None or info[0] != tag:
                    continue
                payloads = list(info[1])
            if len(payloads) == len(argument_types):
                specialized.append(payloads + tail)
        return specialized

    def _patterns_are_exhaustive(self, subject_type: str, patterns) -> bool:
        return self._matrix_is_exhaustive([[pattern] for pattern in patterns], [subject_type])

    def _fully_covered_cases(self, subject_type: str, patterns) -> set[str]:
        constructors = self._finite_constructors(subject_type)
        if constructors is None:
            return set()
        matrix = [[pattern] for pattern in patterns]
        covered: set[str] = set()
        for tag, argument_types in constructors:
            specialized = self._specialize_matrix(matrix, subject_type, tag, argument_types)
            if not self._matrix_is_exhaustive(specialized, list(argument_types)):
                continue
            if tag[0] == "literal" and tag[1] == "Bool":
                covered.add("true" if tag[2] else "false")
            elif tag[0] == "variant":
                covered.add(tag[1])
            elif tag[0] == "enum":
                covered.add(tag[2])
        return covered

    def _matrix_is_exhaustive(self, matrix, type_names: list[str]) -> bool:
        if not matrix:
            return False
        if any(all(isinstance(pattern, (BindingPattern, WildcardPattern)) for pattern in row)
               for row in matrix):
            return True
        if not type_names:
            return bool(matrix)
        type_name, *remaining_types = type_names
        constructors = self._finite_constructors(type_name)
        if constructors is None:
            defaults = [row[1:] for row in matrix
                        if isinstance(row[0], (BindingPattern, WildcardPattern))]
            return self._matrix_is_exhaustive(defaults, remaining_types)
        return all(
            self._matrix_is_exhaustive(
                self._specialize_matrix(matrix, type_name, tag, argument_types),
                list(argument_types) + remaining_types,
            )
            for tag, argument_types in constructors
        )

    def _pattern_is_useful(self, patterns, candidate, subject_type: str) -> bool:
        return self._matrix_pattern_is_useful(
            [[pattern] for pattern in patterns], [candidate], [subject_type],
        )

    def _matrix_pattern_is_useful(self, matrix, candidate, type_names: list[str]) -> bool:
        if not matrix:
            return True
        if not type_names:
            return False
        if any(all(isinstance(pattern, (BindingPattern, WildcardPattern)) for pattern in row)
               for row in matrix):
            return False
        type_name, *remaining_types = type_names
        head, *remaining_candidate = candidate
        constructors = self._finite_constructors(type_name)
        if constructors is not None:
            if isinstance(head, (BindingPattern, WildcardPattern)):
                return any(
                    self._matrix_pattern_is_useful(
                        self._specialize_matrix(matrix, type_name, tag, argument_types),
                        [WildcardPattern(head.location) for _ in argument_types] + remaining_candidate,
                        list(argument_types) + remaining_types,
                    )
                    for tag, argument_types in constructors
                )
            info = self._pattern_head(head, type_name)
            if info is None:
                return False
            tag, payloads = info
            matching = next(((candidate_tag, argument_types) for candidate_tag, argument_types in constructors
                             if candidate_tag == tag), None)
            if matching is None:
                return False
            _, argument_types = matching
            return self._matrix_pattern_is_useful(
                self._specialize_matrix(matrix, type_name, tag, argument_types),
                list(payloads) + remaining_candidate, list(argument_types) + remaining_types,
            )

        if isinstance(head, LiteralPattern):
            literal_tag, _ = self._pattern_head(head, type_name)
            specialized = []
            for row in matrix:
                previous = row[0]
                if isinstance(previous, (BindingPattern, WildcardPattern)):
                    specialized.append(row[1:])
                else:
                    previous_info = self._pattern_head(previous, type_name)
                    if previous_info is not None and previous_info[0] == literal_tag:
                        specialized.append(row[1:])
            return self._matrix_pattern_is_useful(specialized, remaining_candidate, remaining_types)

        defaults = [row[1:] for row in matrix
                    if isinstance(row[0], (BindingPattern, WildcardPattern))]
        return self._matrix_pattern_is_useful(defaults, remaining_candidate, remaining_types)

    def _infer_type_arguments(
        self, formal: str, actual: str, parameters: set[str], substitutions: dict[str, str],
        location: Location, callee: str,
    ) -> None:
        if formal in parameters:
            previous = substitutions.get(formal)
            if previous is None:
                if self._canonical_type(actual) == "Void":
                    raise Diagnostic(location, "type", "type arguments cannot be inferred as Void")
                substitutions[formal] = self._canonical_type(actual)
            elif previous != self._canonical_type(actual):
                raise Diagnostic(location, "type", f"cannot infer one type for '{formal}' in call to '{callee}': found {previous} and {actual}")
            return
        formal_base, formal_arguments = split_type(formal)
        actual_base, actual_arguments = split_type(actual)
        if formal_arguments and formal_base == actual_base and len(formal_arguments) == len(actual_arguments):
            for formal_arg, actual_arg in zip(formal_arguments, actual_arguments):
                self._infer_type_arguments(formal_arg, actual_arg, parameters, substitutions, location, callee)

    def _check_arguments(
        self, arguments: list[Expression], actual_types: list[str], expected_types: tuple[str, ...],
        location: Location, callee: str,
    ) -> None:
        if len(actual_types) != len(expected_types):
            raise Diagnostic(location, "type", f"'{callee}' expects {len(expected_types)} argument(s), got {len(actual_types)}")
        for index, (actual, expected) in enumerate(zip(actual_types, expected_types), start=1):
            canonical_expected = self._canonical_type(expected)
            canonical_actual = self._canonical_type(actual)
            if not self._is_assignable(canonical_expected, canonical_actual):
                raise Diagnostic(arguments[index - 1].location, "type", f"argument {index} of '{callee}' expects {canonical_expected}, found {canonical_actual}")

    def _is_assignable(self, expected: str, actual: str) -> bool:
        expected = self._canonical_type(expected)
        actual = self._canonical_type(actual)
        if expected == actual:
            return True
        actual_base, _ = split_type(actual)
        expected_base, expected_arguments = split_type(expected)
        if expected_arguments:
            return False
        class_info = self.classes.get(actual_base)
        return class_info is not None and expected_base in class_info.declaration.interfaces

    def _binary_type(self, expression: Binary, left: str, right: str) -> str:
        operator = expression.operator
        if operator in {"and", "or"}:
            if left != "Bool" or right != "Bool":
                raise Diagnostic(expression.location, "type", f"operator '{operator}' expects Bool operands, found {left} and {right}")
            return "Bool"
        if operator == "+" and left == right == "String":
            return "String"
        if left == right == "Duration":
            if operator in {"+", "-"}:
                return "Duration"
            if operator in {"==", "!=", "<", "<=", ">", ">="}:
                return "Bool"
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
                if not (self._is_assignable(left, right) or self._is_assignable(right, left)):
                    raise Diagnostic(expression.location, "type", f"cannot compare {left} with {right}")
            return "Bool"
        if operator in {"<", "<=", ">", ">="}:
            if left not in _NUMERIC_TYPES or right not in _NUMERIC_TYPES:
                raise Diagnostic(expression.location, "type", f"operator '{operator}' expects numeric operands, found {left} and {right}")
            return "Bool"
        raise AssertionError(f"unhandled operator: {operator}")

    def _require_same_type(self, expected: str, actual: str, location: Location) -> None:
        if not self._is_assignable(expected, actual):
            raise Diagnostic(location, "type", f"expected {expected}, found {actual}")

    def _check_constructor_initialization(self, constructor: Method, owner: ClassInfo) -> None:
        required = frozenset(owner.fields)

        def reads(expression: Expression, initialized: frozenset[str]) -> None:
            if isinstance(expression, MemberAccess):
                if (isinstance(expression.receiver, Name) and expression.receiver.identifier == "self"
                        and expression.member not in initialized):
                    raise Diagnostic(expression.location, "type", f"field 'self.{expression.member}' may be read before initialization")
                if not (isinstance(expression.receiver, Name) and expression.receiver.identifier == "self"):
                    reads(expression.receiver, initialized)
            elif isinstance(expression, MemberCall):
                if isinstance(expression.receiver, Name) and expression.receiver.identifier == "self":
                    missing = required - initialized
                    if missing:
                        name = sorted(missing)[0]
                        raise Diagnostic(expression.location, "type", f"'self' cannot call methods before field '{name}' is initialized")
                reads(expression.receiver, initialized)
                for argument in expression.arguments:
                    reads(argument, initialized)
            elif isinstance(expression, Call):
                for argument in expression.arguments:
                    reads(argument, initialized)
            elif isinstance(expression, Unary):
                reads(expression.operand, initialized)
            elif isinstance(expression, Binary):
                reads(expression.left, initialized)
                reads(expression.right, initialized)
            elif isinstance(expression, Name) and expression.identifier == "self":
                missing = required - initialized
                if missing:
                    name = sorted(missing)[0]
                    raise Diagnostic(expression.location, "type", f"'self' cannot escape before field '{name}' is initialized")

        def flow(body: list[Statement], states: list[tuple[frozenset[str], str]], loop_depth: int = 0):
            current = states
            for statement in body:
                following: list[tuple[frozenset[str], str]] = []
                for initialized, outcome in current:
                    if outcome != "next":
                        following.append((initialized, outcome))
                        continue
                    if isinstance(statement, Assignment):
                        reads(statement.value, initialized)
                        target = statement.target
                        if (isinstance(target, MemberAccess) and isinstance(target.receiver, Name)
                                and target.receiver.identifier == "self" and target.member in owner.fields):
                            field = owner.fields[target.member]
                            if not field.mutable and target.member in initialized:
                                raise Diagnostic(statement.location, "type", f"immutable field 'self.{target.member}' is initialized more than once")
                            if not field.mutable and loop_depth:
                                raise Diagnostic(statement.location, "type", "immutable fields cannot be initialized inside loops")
                            initialized = initialized | {target.member}
                        following.append((initialized, "next"))
                    elif isinstance(statement, LetStatement):
                        reads(statement.value, initialized)
                        following.append((initialized, "next"))
                    elif isinstance(statement, ReturnStatement):
                        if statement.value is not None:
                            reads(statement.value, initialized)
                        missing = required - initialized
                        if missing:
                            name = sorted(missing)[0]
                            raise Diagnostic(statement.location, "type", f"constructor may return before field '{name}' is initialized")
                        following.append((initialized, "return"))
                    elif isinstance(statement, Block):
                        following.extend(flow(statement.body, [(initialized, "next")], loop_depth))
                    elif isinstance(statement, UsingStatement):
                        reads(statement.resource, initialized)
                        following.extend(flow(statement.body, [(initialized, "next")], loop_depth))
                        if statement.error_name is not None:
                            following.extend(flow(statement.else_body, [(initialized, "next")], loop_depth))
                    elif isinstance(statement, IfStatement):
                        reads(statement.condition, initialized)
                        branches = [statement.then_body, statement.else_body]
                        if isinstance(statement.condition, Literal):
                            branches = [branches[0] if statement.condition.value else branches[1]]
                        for branch in branches:
                            following.extend(flow(branch, [(initialized, "next")], loop_depth))
                    elif isinstance(statement, WhileStatement):
                        reads(statement.condition, initialized)
                        if isinstance(statement.condition, Literal) and not statement.condition.value:
                            following.append((initialized, "next"))
                            continue
                        body_states = flow(statement.body, [(initialized, "next")], loop_depth + 1)
                        following.extend((state, "return") for state, kind in body_states if kind == "return")
                        if not isinstance(statement.condition, Literal) or not statement.condition.value:
                            following.append((initialized, "next"))
                        following.extend((state, "next") for state, kind in body_states if kind == "break")
                    elif isinstance(statement, ForStatement):
                        reads(statement.iterable, initialized)
                        body_states = flow(statement.body, [(initialized, "next")], loop_depth + 1)
                        following.append((initialized, "next"))
                        following.extend((state, "return") for state, kind in body_states if kind == "return")
                        following.extend((state, "next") for state, kind in body_states if kind == "break")
                    elif isinstance(statement, MatchStatement):
                        reads(statement.value, initialized)
                        for arm in statement.arms:
                            following.extend(flow(arm.body, [(initialized, "next")], loop_depth))
                        patterns = [arm.pattern for arm in statement.arms]
                        exhaustive = any(isinstance(item, (WildcardPattern, BindingPattern)) for item in patterns)
                        variants = {item.variant for item in patterns if isinstance(item, VariantPattern)}
                        literals = {(item.type_name, item.value) for item in patterns if isinstance(item, LiteralPattern)}
                        exhaustive |= {"Some", "None"} <= variants or {"Ok", "Err"} <= variants
                        exhaustive |= {("Bool", True), ("Bool", False)} <= literals
                        if not exhaustive:
                            following.append((initialized, "next"))
                    elif isinstance(statement, ExpressionStatement):
                        reads(statement.expression, initialized)
                        following.append((initialized, "next"))
                    elif isinstance(statement, LoopControl):
                        following.append((initialized, statement.kind))
                    else:
                        following.append((initialized, "next"))
                current = list(dict.fromkeys(following))
            return current

        outcomes = flow(constructor.body, [(frozenset(), "next")])
        for initialized, outcome in outcomes:
            if outcome == "next":
                missing = required - initialized
                if missing:
                    name = sorted(missing)[0]
                    raise Diagnostic(constructor.location, "type", f"constructor does not initialize field '{name}' on all paths")

