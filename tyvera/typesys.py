from __future__ import annotations

import math
from collections import ChainMap
from dataclasses import dataclass

from .ast import (
    Assignment,
    Binary,
    Block,
    Call,
    ClassDeclaration,
    Expression,
    ExpressionStatement,
    Field,
    Function,
    IfStatement,
    InterfaceDeclaration,
    LetStatement,
    Literal,
    LoopControl,
    MemberAccess,
    MemberCall,
    Method,
    MethodSignature,
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


@dataclass(slots=True)
class ClassInfo:
    declaration: ClassDeclaration
    fields: dict[str, Field]
    methods: dict[str, Signature]


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
        self.type_names: set[str] = set(_BUILTIN_TYPES)

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
        for class_info in self.classes.values():
            for method in class_info.declaration.methods:
                signature = class_info.methods[method.name]
                self._check_method(method, signature, class_info)
            constructor = next((method for method in class_info.declaration.methods if method.name == "init"), None)
            if constructor is not None:
                self._check_constructor_initialization(constructor, class_info)
        return self.signatures

    def _collect_types(self) -> None:
        names: dict[str, Location] = {}
        for declaration in [*self.program.classes, *self.program.interfaces]:
            if declaration.name in _BUILTIN_TYPES:
                raise Diagnostic(declaration.location, "type", f"type name '{declaration.name}' is reserved")
            if declaration.name in names:
                raise Diagnostic(declaration.location, "type", f"type '{declaration.name}' is already declared")
            names[declaration.name] = declaration.location
        self.type_names.update(names)

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
            fields: dict[str, Field] = {}
            for member in declaration.fields:
                if member.name in fields:
                    raise Diagnostic(member.location, "type", f"field '{member.name}' is already declared")
                self._require_known_type(member.type_name, member.location)
                if canonical_type(member.type_name) == "Void":
                    raise Diagnostic(member.location, "type", "fields cannot have type Void")
                fields[member.name] = member
            methods: dict[str, Signature] = {}
            for method in declaration.methods:
                if method.name in methods:
                    raise Diagnostic(method.location, "type", f"method '{method.name}' is already declared")
                if method.name in fields:
                    raise Diagnostic(method.location, "type", f"method '{method.name}' conflicts with a field")
                methods[method.name] = self._method_signature(method)
            constructor = methods.get("init")
            if declaration.fields and constructor is None:
                raise Diagnostic(declaration.location, "type", f"class '{declaration.name}' declares fields and requires an 'init' constructor")
            self.classes[declaration.name] = ClassInfo(declaration, fields, methods)

    def _collect_signatures(self) -> None:
        all_names = set(self.classes) | set(self.interfaces)
        for function in self.program.functions:
            if function.name == "println":
                raise Diagnostic(function.location, "type", "'println' is a built-in function name")
            if function.name in all_names or function.name in self.signatures:
                raise Diagnostic(function.location, "type", f"function or type '{function.name}' is already declared")
            self.signatures[function.name] = self._method_signature(function)

    def _method_signature(self, function: Function | Method | MethodSignature) -> Signature:
        parameter_names: set[str] = set()
        parameter_types: list[str] = []
        for parameter in function.parameters:
            if parameter.name == "self":
                raise Diagnostic(parameter.location, "type", "'self' is reserved for the current instance")
            if parameter.name == "println":
                raise Diagnostic(parameter.location, "type", "'println' is reserved for the built-in function")
            if parameter.name in parameter_names:
                raise Diagnostic(parameter.location, "type", f"parameter '{parameter.name}' is declared more than once")
            parameter_names.add(parameter.name)
            self._require_known_type(parameter.type_name, parameter.location)
            if canonical_type(parameter.type_name) == "Void":
                raise Diagnostic(parameter.location, "type", "parameters cannot have type Void")
            parameter_types.append(canonical_type(parameter.type_name))
        self._require_known_type(function.return_type, function.location)
        return Signature(tuple(parameter_types), canonical_type(function.return_type), function.location)

    def _require_known_type(self, type_name: str, location: Location) -> None:
        if type_name not in self.type_names:
            raise Diagnostic(location, "type", f"unknown type '{type_name}'")

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
        variables = ChainMap({p.name: Binding(canonical_type(p.type_name)) for p in function.parameters})
        outcomes = self._check_block(function.body, variables, signature.return_type, 0)
        self._require_return_paths(function.name, signature.return_type, outcomes, function.location)

    def _check_method(self, method: Method, signature: Signature, owner: ClassInfo) -> None:
        if method.name == "init" and signature.return_type != "Void":
            raise Diagnostic(method.location, "type", "constructors must return Void")
        variables = ChainMap({p.name: Binding(canonical_type(p.type_name)) for p in method.parameters})
        variables["self"] = Binding(owner.declaration.name)
        outcomes = self._check_block(method.body, variables, signature.return_type, 0, owner, method.name)
        self._require_return_paths(f"{owner.declaration.name}.{method.name}", signature.return_type, outcomes, method.location)

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
            argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
            if expression.callee == "println":
                if len(argument_types) != 1:
                    raise Diagnostic(expression.location, "type", "'println' expects exactly one argument")
                if argument_types[0] not in _DISPLAYABLE_TYPES:
                    raise Diagnostic(expression.arguments[0].location, "type", "'println' cannot display a Void value")
                return "Void"
            class_info = self.classes.get(expression.callee)
            if class_info is not None:
                constructor = class_info.methods.get("init")
                expected = () if constructor is None else constructor.parameter_types
                self._check_arguments(expression.arguments, argument_types, expected, expression.location, expression.callee)
                return expression.callee
            signature = self.signatures.get(expression.callee)
            if signature is None:
                if expression.callee in self.interfaces:
                    raise Diagnostic(expression.location, "type", "interfaces cannot be constructed")
                raise Diagnostic(expression.location, "type", f"unknown function '{expression.callee}'")
            self._check_arguments(expression.arguments, argument_types, signature.parameter_types, expression.location, expression.callee)
            return signature.return_type
        if isinstance(expression, MemberAccess):
            field_type, _ = self._member_field(expression, variables, owner)
            return field_type
        if isinstance(expression, MemberCall):
            receiver_type = self._expression_type(expression.receiver, variables, owner)
            argument_types = [self._expression_type(argument, variables, owner) for argument in expression.arguments]
            if expression.method == "init":
                raise Diagnostic(expression.location, "type", "constructors can only run when creating an instance")
            if receiver_type in self.classes:
                signature = self.classes[receiver_type].methods.get(expression.method)
            elif receiver_type in self.interfaces:
                signature = self.interfaces[receiver_type].methods.get(expression.method)
            else:
                signature = None
            if signature is None:
                raise Diagnostic(expression.location, "type", f"type '{receiver_type}' has no method '{expression.method}'")
            self._check_arguments(expression.arguments, argument_types, signature.parameter_types, expression.location,
                                  f"{receiver_type}.{expression.method}")
            return signature.return_type
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
        receiver_type = self._expression_type(expression.receiver, variables, owner)
        class_info = self.classes.get(receiver_type)
        field = None if class_info is None else class_info.fields.get(expression.member)
        if field is None:
            raise Diagnostic(expression.location, "type", f"type '{receiver_type}' has no field '{expression.member}'")
        return canonical_type(field.type_name), field

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
        class_info = self.classes.get(actual)
        return class_info is not None and expected in class_info.declaration.interfaces

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
