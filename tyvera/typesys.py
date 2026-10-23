from __future__ import annotations

import math
from collections import ChainMap
from dataclasses import dataclass

from .ast import (
    ActorDeclaration,
    Assignment,
    Binary,
    Block,
    Call,
    ClassDeclaration,
    Expression,
    ExpressionStatement,
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
    Statement,
    Unary,
    VariantPattern,
    WildcardPattern,
    WhileStatement,
)
from .source import Diagnostic, Location
from .type_names import make_type, split_type, substitute_type


_BUILTIN_TYPES = {"Int", "Float", "Bool", "String", "Void", "Unit", "List", "Map", "Option", "Result", "Secret", "Json", "ActorRef", "Supervisor"}
_BUILTIN_FUNCTIONS = {
    "println", "eprintln", "readText", "writeText", "pathJoin", "pathExists",
    "parseJson", "jsonStringify", "jsonString", "jsonInt", "jsonFloat", "jsonBool",
    "jsonNull", "jsonArray", "jsonObject", "spawn", "runActors", "oneForOne",
}
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
    type_parameters: tuple[str, ...] = ()


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


class TypeChecker:
    def __init__(self, program: Program, filename: str = "<memory>") -> None:
        self.program = program
        self.filename = filename
        self.signatures: dict[str, Signature] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.interfaces: dict[str, InterfaceInfo] = {}
        self.actors: dict[str, ClassInfo] = {}
        self.actor_names: set[str] = set()
        self.type_names: set[str] = set(_BUILTIN_TYPES)
        self.generic_arities: dict[str, int] = {"List": 1, "Map": 2, "Option": 1, "Result": 2, "Secret": 1, "ActorRef": 1}
        self.active_type_variables: set[str] = set()

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
        declarations = [*self.program.classes, *self.program.interfaces, *self.program.actors]
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
        for declaration in self.program.interfaces:
            methods: dict[str, Signature] = {}
            for method in declaration.methods:
                if method.name == "init":
                    raise Diagnostic(method.location, "type", "interfaces cannot declare constructors")
                if method.name in methods:
                    raise Diagnostic(method.location, "type", f"interface method '{method.name}' is already declared")
                methods[method.name] = self._method_signature(method)
            self.interfaces[declaration.name] = InterfaceInfo(declaration, methods)

        for declaration in self.program.classes:
            class_type_variables = set(declaration.type_parameters)
            fields: dict[str, Field] = {}
            for member in declaration.fields:
                if member.name in fields:
                    raise Diagnostic(member.location, "type", f"field '{member.name}' is already declared")
                self._require_known_type(member.type_name, member.location, class_type_variables)
                if canonical_type(member.type_name) == "Void":
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
                if canonical_type(member.type_name) == "Void":
                    raise Diagnostic(member.location, "type", "actor state fields cannot have type Void")
                fields[member.name] = member
            handlers: dict[str, Signature] = {}
            for method in declaration.handlers:
                if method.name in {"stop", "isAlive"}:
                    raise Diagnostic(method.location, "type", f"actor handler name '{method.name}' is reserved by ActorRef lifecycle methods")
                if method.name in handlers:
                    raise Diagnostic(method.location, "type", f"actor handler '{method.name}' is already declared")
                if method.name in fields:
                    raise Diagnostic(method.location, "type", f"actor handler '{method.name}' conflicts with a state field")
                signature = self._method_signature(method)
                if method.name != "init" and signature.return_type != "Void":
                    raise Diagnostic(method.location, "type", "actor message handlers must return Void")
                for parameter, parameter_type in zip(method.parameters, signature.parameter_types):
                    if not self._is_actor_message_type(parameter_type):
                        purpose = "actor constructor" if method.name == "init" else "actor message"
                        raise Diagnostic(parameter.location, "type", f"{purpose} cannot carry mutable or process-local value type '{parameter_type}'")
                handlers[method.name] = signature
            if fields and "init" not in handlers:
                raise Diagnostic(declaration.location, "type", f"actor '{declaration.name}' declares state and requires an 'init' constructor")
            self.actors[declaration.name] = ClassInfo(declaration, fields, handlers)

    def _is_actor_message_type(self, type_name: str) -> bool:
        base, arguments = split_type(type_name)
        if base in {"Int", "Float", "Bool", "String"} and not arguments:
            return True
        if base == "ActorRef" and len(arguments) == 1:
            actor_base, actor_arguments = split_type(arguments[0])
            return actor_base in self.actor_names and not actor_arguments
        if base == "Option" and len(arguments) == 1:
            return self._is_actor_message_type(arguments[0])
        if base == "Result" and len(arguments) == 2:
            return all(self._is_actor_message_type(argument) for argument in arguments)
        return False

    def _collect_signatures(self) -> None:
        all_names = set(self.classes) | set(self.interfaces) | set(self.actors)
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
            if canonical_type(parameter.type_name) == "Void":
                raise Diagnostic(parameter.location, "type", "parameters cannot have type Void")
            parameter_types.append(canonical_type(parameter.type_name))
        self._require_known_type(function.return_type, function.location, known_variables)
        return Signature(tuple(parameter_types), canonical_type(function.return_type), function.location,
                         tuple(declared_parameters))

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
        if base in self.actor_names and not allow_actor:
            raise Diagnostic(location, "type", f"actor type '{base}' can only be used inside ActorRef<{base}>")
        expected_arity = self.generic_arities.get(base, 0)
        if len(arguments) != expected_arity:
            if expected_arity:
                raise Diagnostic(location, "type", f"type '{base}' expects {expected_arity} type argument(s), got {len(arguments)}")
            if arguments:
                raise Diagnostic(location, "type", f"type '{base}' does not accept type arguments")
        for argument in arguments:
            self._require_known_type(argument, location, known_variables, allow_actor=(base == "ActorRef"))
            if canonical_type(argument) == "Void":
                raise Diagnostic(location, "type", "type arguments cannot be Void")
        if base == "ActorRef" and arguments:
            actor_base, actor_arguments = split_type(arguments[0])
            if actor_arguments or actor_base not in self.actor_names:
                raise Diagnostic(location, "type", "ActorRef requires one actor type, such as ActorRef<Counter>")
        if base == "Map" and arguments:
            key_base, _ = split_type(arguments[0])
            allowed_keys = _DISPLAYABLE_TYPES | set(self.classes) | set(self.interfaces)
            if key_base not in allowed_keys:
                raise Diagnostic(location, "type", f"Map key type '{arguments[0]}' is not a supported primitive or object-reference type")

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
        self.active_type_variables = set(signature.type_parameters)
        variables = ChainMap({p.name: Binding(canonical_type(p.type_name)) for p in function.parameters})
        try:
            outcomes = self._check_block(function.body, variables, signature.return_type, 0)
            self._require_return_paths(function.name, signature.return_type, outcomes, function.location)
        finally:
            self.active_type_variables = previous

    def _check_method(self, method: Method, signature: Signature, owner: ClassInfo) -> None:
        if method.name == "init" and signature.return_type != "Void":
            raise Diagnostic(method.location, "type", "constructors must return Void")
        previous = self.active_type_variables
        self.active_type_variables = set(owner.type_parameters)
        variables = ChainMap({p.name: Binding(canonical_type(p.type_name)) for p in method.parameters})
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
        method_name: str | None = None,
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
            elif isinstance(statement, Block):
                exits = self._check_block(statement.body, variables.new_child(), return_type, loop_depth, owner, method_name)
            elif isinstance(statement, IfStatement):
                self._require_same_type("Bool", self._expression_type(statement.condition, variables, owner), statement.condition.location)
                then_exits = self._check_block(statement.then_body, variables.new_child(), return_type, loop_depth, owner, method_name)
                else_exits = self._check_block(statement.else_body, variables.new_child(), return_type, loop_depth, owner, method_name)
                if isinstance(statement.condition, Literal):
                    exits = then_exits if statement.condition.value else else_exits
                else:
                    exits = then_exits | else_exits
            elif isinstance(statement, WhileStatement):
                self._require_same_type("Bool", self._expression_type(statement.condition, variables, owner), statement.condition.location)
                body_exits = self._check_block(statement.body, variables.new_child(), return_type, loop_depth + 1, owner, method_name)
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
                body_exits = self._check_block(statement.body, loop_variables, return_type, loop_depth + 1, owner, method_name)
                exits = {"next"} | (body_exits & {"return"})
            elif isinstance(statement, MatchStatement):
                subject_type = self._expression_type(statement.value, variables, owner)
                exits = set()
                seen: set[tuple[object, ...]] = set()
                coverage: set[str] = set()
                has_catchall = False
                for arm in statement.arms:
                    pattern = arm.pattern
                    if has_catchall:
                        raise Diagnostic(pattern.location, "type", "match arm is unreachable after a wildcard or binding pattern")
                    if self._match_is_exhaustive(subject_type, coverage, has_catchall):
                        raise Diagnostic(pattern.location, "type", "match arm is unreachable after exhaustive patterns")
                    key = self._check_pattern(pattern, subject_type, variables)
                    if key in seen:
                        raise Diagnostic(pattern.location, "type", "duplicate match pattern")
                    seen.add(key)
                    if key and key[0] == "variant":
                        coverage.add(str(key[1]))
                    if key and key[0] == "literal" and key[1] == "Bool":
                        coverage.add("true" if key[2] else "false")
                    if key and key[0] == "catchall":
                        has_catchall = True
                    arm_variables = variables.new_child()
                    if isinstance(pattern, BindingPattern):
                        arm_variables[pattern.name] = Binding(subject_type)
                    elif isinstance(pattern, VariantPattern) and pattern.binding is not None:
                        arm_variables[pattern.binding] = Binding(self._variant_payload_type(pattern, subject_type))
                    exits |= self._check_block(arm.body, arm_variables, return_type, loop_depth, owner, method_name)
                if not self._match_is_exhaustive(subject_type, coverage, has_catchall):
                    exits.add("next")
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
        if isinstance(expression, Literal):
            if expression.type_name == "Int" and expression.value > INT_MAX:
                raise Diagnostic(expression.location, "type", "Int literal is outside the signed 64-bit range")
            if expression.type_name == "Float" and not math.isfinite(expression.value):
                raise Diagnostic(expression.location, "type", "Float literal is outside the supported range")
            return expression.type_name
        if isinstance(expression, Name):
            binding = variables.get(expression.identifier)
            if binding is None:
                raise Diagnostic(expression.location, "type", f"unknown variable '{expression.identifier}'")
            return binding.type_name
        if isinstance(expression, Call):
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
            if expression.callee == "runActors":
                if expression.type_arguments or expression.arguments:
                    raise Diagnostic(expression.location, "type", "'runActors' expects no arguments")
                return "Result<Int,String>"
            argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
            if expression.callee == "oneForOne":
                if expression.type_arguments:
                    raise Diagnostic(expression.location, "type", "'oneForOne' does not accept type arguments")
                self._check_arguments(expression.arguments, argument_types, ("Int", "Int"),
                                      expression.location, "oneForOne")
                return "Supervisor"
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
                collection_type = make_type(expression.callee, [canonical_type(item) for item in expression.type_arguments])
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
                expression.arguments, expression.location, expression.callee,
            )
            return substitute_type(signature.return_type, substitutions)
        if isinstance(expression, MemberAccess):
            field_type, _ = self._member_field(expression, variables, owner)
            return field_type
        if isinstance(expression, MemberCall):
            if expression.type_arguments:
                raise Diagnostic(expression.location, "type", "generic methods are not supported yet")
            receiver_type = self._expression_type(expression.receiver, variables, owner)
            if expression.method == "init":
                raise Diagnostic(expression.location, "type", "constructors can only run when creating an instance")
            receiver_base, receiver_arguments = split_type(receiver_type)
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
            argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
            if receiver_base == "Secret" and expression.method == "reveal":
                if expression.arguments:
                    raise Diagnostic(expression.location, "type", "Secret.reveal() expects no arguments")
                if len(receiver_arguments) != 1:
                    raise Diagnostic(expression.location, "type", "Secret requires one type argument")
                return receiver_arguments[0]
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
            else:
                signature = None
                substitutions = {}
            if signature is None:
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
        field = None if class_info is None else class_info.fields.get(expression.member)
        if field is None:
            raise Diagnostic(expression.location, "type", f"type '{receiver_type}' has no field '{expression.member}'")
        if receiver_arguments:
            substitutions = dict(zip(class_info.type_parameters, receiver_arguments))
        elif class_info is not None:
            substitutions = {name: name for name in class_info.type_parameters}
        else:
            substitutions = {}
        return canonical_type(substitute_type(field.type_name, substitutions)), field

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
                if canonical_type(type_name) == "Void":
                    raise Diagnostic(location, "type", "type arguments cannot be Void")
                substitutions[parameter] = canonical_type(type_name)
        else:
            allowed = set(type_parameters)
            for formal, actual in zip(formal_types, actual_types):
                self._infer_type_arguments(formal, actual, allowed, substitutions, location, callee)
            missing = [name for name in type_parameters if name not in substitutions]
            if missing:
                raise Diagnostic(location, "type", f"cannot infer type argument '{missing[0]}' for '{callee}'; provide it explicitly")

        resolved_parameters = tuple(substitute_type(item, substitutions) for item in formal_types)
        self._check_arguments(arguments, actual_types, resolved_parameters, location, callee)
        return substitutions

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
            elif base == "Result" and len(arguments) == 2:
                if pattern.variant not in {"Ok", "Err"}:
                    raise Diagnostic(pattern.location, "type", f"Result<{arguments[0]},{arguments[1]}> has no pattern '{pattern.variant}'")
            else:
                raise Diagnostic(pattern.location, "type", f"variant patterns require Option or Result, found {subject_type}")
            if pattern.binding in {"self", "secrets", *_BUILTIN_FUNCTIONS}:
                raise Diagnostic(pattern.location, "type", f"'{pattern.binding}' is reserved and cannot be a pattern binding")
            return ("variant", pattern.variant)
        raise AssertionError(f"unhandled pattern node: {type(pattern).__name__}")

    @staticmethod
    def _variant_payload_type(pattern: VariantPattern, subject_type: str) -> str:
        base, arguments = split_type(subject_type)
        if base == "Option":
            return arguments[0]
        return arguments[0] if pattern.variant == "Ok" else arguments[1]

    @staticmethod
    def _match_is_exhaustive(subject_type: str, coverage: set[str], catchall: bool) -> bool:
        if catchall:
            return True
        base, _ = split_type(subject_type)
        if base == "Option":
            return {"Some", "None"} <= coverage
        if base == "Result":
            return {"Ok", "Err"} <= coverage
        if subject_type == "Bool":
            return {"true", "false"} <= coverage
        return False

    def _infer_type_arguments(
        self, formal: str, actual: str, parameters: set[str], substitutions: dict[str, str],
        location: Location, callee: str,
    ) -> None:
        if formal in parameters:
            previous = substitutions.get(formal)
            if previous is None:
                if canonical_type(actual) == "Void":
                    raise Diagnostic(location, "type", "type arguments cannot be inferred as Void")
                substitutions[formal] = canonical_type(actual)
            elif previous != canonical_type(actual):
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
            if not self._is_assignable(expected, actual):
                raise Diagnostic(arguments[index - 1].location, "type", f"argument {index} of '{callee}' expects {expected}, found {actual}")

    def _is_assignable(self, expected: str, actual: str) -> bool:
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
