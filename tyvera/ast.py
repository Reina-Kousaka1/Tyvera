from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .source import Location


@dataclass(slots=True)
class Program:
    functions: list[Function]


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


class Statement:
    location: Location


@dataclass(slots=True)
class LetStatement(Statement):
    name: str
    annotation: str | None
    value: Expression
    location: Location


@dataclass(slots=True)
class ReturnStatement(Statement):
    value: Expression | None
    location: Location


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
