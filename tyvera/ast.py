from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .source import Location


@dataclass(slots=True)
class Program:
    functions: list[Function]
    classes: list[ClassDeclaration] = field(default_factory=list)
    interfaces: list[InterfaceDeclaration] = field(default_factory=list)
    imports: list[ImportDeclaration] = field(default_factory=list)
    actors: list[ActorDeclaration] = field(default_factory=list)
    records: list[RecordDeclaration] = field(default_factory=list)
    enums: list[EnumDeclaration] = field(default_factory=list)
    newtypes: list[NewtypeDeclaration] = field(default_factory=list)
    type_aliases: list[TypeAliasDeclaration] = field(default_factory=list)


@dataclass(slots=True)
class ImportDeclaration:
    path: str
    alias: str
    location: Location


@dataclass(slots=True)
class Field:
    name: str
    type_name: str
    mutable: bool
    location: Location


@dataclass(slots=True)
class Method:
    name: str
    parameters: list[Parameter]
    return_type: str
    body: list[Statement]
    location: Location


@dataclass(slots=True)
class MethodSignature:
    name: str
    parameters: list[Parameter]
    return_type: str
    location: Location


@dataclass(slots=True)
class ClassDeclaration:
    name: str
    interfaces: list[str]
    fields: list[Field]
    methods: list[Method]
    location: Location
    public: bool = False
    type_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecordDeclaration:
    name: str
    fields: list[Field]
    location: Location
    public: bool = False
    type_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EnumVariant:
    name: str
    payload_types: list[str]
    location: Location


@dataclass(slots=True)
class EnumDeclaration:
    name: str
    variants: list[EnumVariant]
    location: Location
    public: bool = False
    type_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NewtypeDeclaration:
    name: str
    underlying_type: str
    location: Location
    public: bool = False
    type_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TypeAliasDeclaration:
    name: str
    target_type: str
    location: Location
    public: bool = False
    type_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class InterfaceDeclaration:
    name: str
    methods: list[MethodSignature]
    location: Location
    public: bool = False


@dataclass(slots=True)
class ActorDeclaration:
    name: str
    fields: list[Field]
    handlers: list[Method]
    location: Location
    public: bool = False

    @property
    def methods(self) -> list[Method]:
        return self.handlers


@dataclass(slots=True)
class Parameter:
    name: str
    type_name: str
    location: Location


@dataclass(slots=True)
class Function:
    name: str
    parameters: list[Parameter]
    return_type: str
    body: list[Statement]
    location: Location
    public: bool = False
    type_parameters: list[str] = field(default_factory=list)
    is_async: bool = False
    type_constraints: dict[str, list[str]] = field(default_factory=dict)


class Statement:
    location: Location


@dataclass(slots=True)
class LetStatement(Statement):
    name: str
    annotation: str | None
    value: Expression
    location: Location
    mutable: bool = False


@dataclass(slots=True)
class Assignment(Statement):
    target: str | MemberAccess
    value: Expression
    location: Location


@dataclass(slots=True)
class Block(Statement):
    body: list[Statement]
    location: Location


@dataclass(slots=True)
class UsingStatement(Statement):
    name: str
    resource: Expression
    body: list[Statement]
    location: Location
    error_name: str | None = None
    else_body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class IfStatement(Statement):
    condition: Expression
    then_body: list[Statement]
    else_body: list[Statement]
    location: Location


@dataclass(slots=True)
class WhileStatement(Statement):
    condition: Expression
    body: list[Statement]
    location: Location


@dataclass(slots=True)
class ForStatement(Statement):
    name: str
    iterable: Expression
    body: list[Statement]
    location: Location


@dataclass(slots=True)
class MatchStatement(Statement):
    value: Expression
    arms: list[MatchArm]
    location: Location


class Pattern:
    location: Location


@dataclass(slots=True)
class LiteralPattern(Pattern):
    value: Any
    type_name: str
    location: Location


@dataclass(slots=True)
class WildcardPattern(Pattern):
    location: Location


@dataclass(slots=True)
class BindingPattern(Pattern):
    name: str
    location: Location


@dataclass(slots=True)
class VariantPattern(Pattern):
    variant: str
    binding: str | None
    location: Location
    payload_pattern: Pattern | None = None


@dataclass(slots=True)
class TuplePattern(Pattern):
    elements: list[Pattern]
    location: Location


@dataclass(slots=True)
class RecordPattern(Pattern):
    name: str
    fields: list[Pattern]
    location: Location


@dataclass(slots=True)
class EnumPattern(Pattern):
    enum_name: str
    variant: str
    payload_patterns: list[Pattern]
    location: Location


@dataclass(slots=True)
class MatchArm:
    pattern: Pattern
    body: list[Statement]
    location: Location


@dataclass(slots=True)
class LoopControl(Statement):
    kind: str
    location: Location


@dataclass(slots=True)
class ReturnStatement(Statement):
    value: Expression | None
    location: Location


@dataclass(slots=True)
class AwaitStatement(Statement):
    value: Expression
    location: Location
    binding_name: str | None = None
    annotation: str | None = None
    mutable: bool = False


@dataclass(slots=True)
class ExpressionStatement(Statement):
    expression: Expression
    location: Location


class Expression:
    location: Location


@dataclass(slots=True)
class Literal(Expression):
    value: Any
    type_name: str
    location: Location


@dataclass(slots=True)
class Name(Expression):
    identifier: str
    location: Location


@dataclass(slots=True)
class Call(Expression):
    callee: str
    arguments: list[Expression]
    location: Location
    type_arguments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemberAccess(Expression):
    receiver: Expression
    member: str
    location: Location


@dataclass(slots=True)
class MemberCall(Expression):
    receiver: Expression
    method: str
    arguments: list[Expression]
    location: Location
    type_arguments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TupleExpression(Expression):
    elements: list[Expression]
    location: Location


@dataclass(slots=True)
class Unary(Expression):
    operator: str
    operand: Expression
    location: Location


@dataclass(slots=True)
class Binary(Expression):
    left: Expression
    operator: str
    right: Expression
    location: Location
