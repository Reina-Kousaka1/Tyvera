"""Lower the supported, already checked Tyvera subset into native IR."""

from __future__ import annotations

from .ast import (
    Assignment,
    Binary,
    Block,
    Call,
    Expression,
    ExpressionStatement,
    Function,
    IfStatement,
    LetStatement,
    Literal,
    LoopControl,
    Name,
    Program,
    ReturnStatement,
    Statement,
    Unary,
    WhileStatement,
)
from .compiler import CheckedProgram
from .ir import (
    IRAssign,
    IRBinary,
    IRBlock,
    IRCall,
    IRExpression,
    IRExpressionStatement,
    IRFunction,
    IRIf,
    IRLet,
    IRLiteral,
    IRLocalRef,
    IRLoopControl,
    IRModule,
    IROutput,
    IRReturn,
    IRType,
    IRUnary,
    IRVariable,
    IRWhile,
    validate_ir,
)
from .source import Diagnostic, Location


_TYPE_MAP = {
    "Int": IRType.INT,
    "Float": IRType.FLOAT,
    "Bool": IRType.BOOL,
    "String": IRType.STRING,
    "Void": IRType.VOID,
    "Unit": IRType.VOID,
}


def lower_checked_program(checked: CheckedProgram) -> IRModule:
    """Lower the stable scalar/control-flow slice, rejecting other features."""
    program = checked.program
    if program.classes:
        raise Diagnostic(program.classes[0].location, "backend", "native backend does not yet support classes")
    if program.interfaces:
        raise Diagnostic(program.interfaces[0].location, "backend", "native backend does not yet support interfaces")
    if program.actors:
        raise Diagnostic(program.actors[0].location, "backend", "native backend does not yet support actors")
    if program.records:
        raise Diagnostic(program.records[0].location, "backend", "native backend does not yet support records")
    if program.enums:
        raise Diagnostic(program.enums[0].location, "backend", "native backend does not yet support enums or pattern matching")
    if program.newtypes:
        raise Diagnostic(program.newtypes[0].location, "backend", "native backend does not yet support newtypes")
    if program.imports:
        raise Diagnostic(program.imports[0].location, "backend", "native backend requires linked project modules")
    if not program.functions:
        raise Diagnostic(Location("<program>", 1, 1), "backend", "native backend requires a main function")

    symbols = {function.name: f"tyv_fn_{index}" for index, function in enumerate(program.functions)}
    lowerer = _FunctionLowerer(checked, symbols)
    functions = tuple(lowerer.lower(function) for function in program.functions)
    entry = next((function for function in functions if function.name == "main"), None)
    if entry is None:
        raise Diagnostic(program.functions[0].location, "backend", "native backend requires a main function")
    module = IRModule(functions, entry.symbol)
    validate_ir(module)
    return module


class _FunctionLowerer:
    def __init__(self, checked: CheckedProgram, symbols: dict[str, str]) -> None:
        self.checked = checked
        self.symbols = symbols
        self.scopes: list[dict[str, IRVariable]] = []
        self.next_local_id = 0
        self.return_type = IRType.VOID
        self.loop_depth = 0

    def lower(self, function: Function) -> IRFunction:
        if function.is_async:
            raise Diagnostic(function.location, "backend", "native backend does not yet support async functions or Tasks")
        if function.type_parameters:
            raise Diagnostic(function.location, "backend", "native backend does not yet support generic functions")
        signature = self.checked.function_signatures.get(function.name)
        if signature is None:
            raise Diagnostic(function.location, "backend", f"native backend has no checked signature for '{function.name}'")
        if len(signature.parameter_types) != len(function.parameters):
            raise Diagnostic(function.location, "backend", f"native backend cannot resolve parameters for '{function.name}'")
        parameter_variables: list[IRVariable] = []
        self.scopes = [{}]
        self.next_local_id = 0
        for parameter, type_name in zip(function.parameters, signature.parameter_types):
            parameter_variables.append(self._new_variable(
                parameter.name, type_name, mutable=False, location=parameter.location,
            ))
        self.return_type = self._type(signature.return_type, function.location)
        body = self._statements(function.body)
        return IRFunction(
            function.name,
            self.symbols[function.name],
            tuple(parameter_variables),
            self.return_type,
            body,
            function.location,
        )

    def _type(self, type_name: str, location: Location) -> IRType:
        seen: set[str] = set()
        while type_name in self.checked.type_alias_targets:
            if type_name in seen:
                raise Diagnostic(location, "backend", f"native backend found a recursive type alias '{type_name}'")
            seen.add(type_name)
            type_name = self.checked.type_alias_targets[type_name]
        result = _TYPE_MAP.get(type_name)
        if result is None:
            raise Diagnostic(
                location,
                "backend",
                f"native backend does not yet support Tyvera type '{type_name}'",
            )
        return result

    def _expression_type(self, expression: Expression) -> IRType:
        type_name = self.checked.expression_types.get(expression.location)
        if type_name is None:
            raise Diagnostic(expression.location, "backend", "native backend is missing checked expression type information")
        return self._type(type_name, expression.location)

    def _new_variable(
        self, name: str, type_name: str, *, mutable: bool, location: Location,
    ) -> IRVariable:
        variable = IRVariable(self.next_local_id, name, self._type(type_name, location), mutable, location)
        self.next_local_id += 1
        self.scopes[-1][name] = variable
        return variable

    def _lookup(self, name: str, location: Location) -> IRVariable:
        for scope in reversed(self.scopes):
            variable = scope.get(name)
            if variable is not None:
                return variable
        raise Diagnostic(location, "backend", f"native backend cannot resolve local '{name}'")

    def _statements(self, statements: list[Statement], *, nested_scope: bool = False) -> tuple:
        if nested_scope:
            self.scopes.append({})
        result = []
        try:
            for statement in statements:
                result.append(self._statement(statement))
        finally:
            if nested_scope:
                self.scopes.pop()
        return tuple(result)

    def _statement(self, statement: Statement):
        if isinstance(statement, LetStatement):
            value = self._expression(statement.value)
            variable = self._new_variable(
                statement.name,
                self.checked.expression_types.get(statement.value.location, ""),
                mutable=statement.mutable,
                location=statement.location,
            )
            return IRLet(variable, value, statement.location)
        if isinstance(statement, Assignment):
            if not isinstance(statement.target, str):
                raise Diagnostic(statement.location, "backend", "native backend does not yet support field assignment")
            variable = self._lookup(statement.target, statement.location)
            return IRAssign(variable.identifier, self._expression(statement.value), statement.location)
        if isinstance(statement, ReturnStatement):
            return IRReturn(
                None if statement.value is None else self._expression(statement.value),
                statement.location,
            )
        if isinstance(statement, Block):
            return IRBlock(self._statements(statement.body, nested_scope=True), statement.location)
        if isinstance(statement, IfStatement):
            condition = self._expression(statement.condition)
            then_body = self._statements(statement.then_body, nested_scope=True)
            else_body = self._statements(statement.else_body, nested_scope=True)
            return IRIf(condition, then_body, else_body, statement.location)
        if isinstance(statement, WhileStatement):
            condition = self._expression(statement.condition)
            self.loop_depth += 1
            try:
                body = self._statements(statement.body, nested_scope=True)
            finally:
                self.loop_depth -= 1
            return IRWhile(condition, body, statement.location)
        if isinstance(statement, LoopControl):
            if self.loop_depth == 0:
                raise Diagnostic(statement.location, "backend", "native backend found loop control outside a loop")
            return IRLoopControl(statement.kind, statement.location)
        if isinstance(statement, ExpressionStatement):
            expression = statement.expression
            if isinstance(expression, Call) and expression.callee in {"println", "eprintln"}:
                if len(expression.arguments) != 1:
                    raise Diagnostic(expression.location, "backend", f"'{expression.callee}' expects one argument")
                return IROutput(
                    "stdout" if expression.callee == "println" else "stderr",
                    self._expression(expression.arguments[0]),
                    statement.location,
                )
            return IRExpressionStatement(self._expression(expression), statement.location)

        unsupported = {
            "ForStatement": "for loops and collection iteration",
            "MatchStatement": "pattern matching and Option/Result values",
            "AwaitStatement": "Tasks and await",
            "UsingStatement": "owned resources and deterministic cleanup",
        }.get(type(statement).__name__, type(statement).__name__)
        raise Diagnostic(statement.location, "backend", f"native backend does not yet support {unsupported}")

    def _expression(self, expression: Expression) -> IRExpression:
        expression_type = self._expression_type(expression)
        if isinstance(expression, Literal):
            return IRLiteral(expression.value, expression_type, expression.location)
        if isinstance(expression, Name):
            variable = self._lookup(expression.identifier, expression.location)
            return IRLocalRef(variable.identifier, expression_type, expression.location)
        if isinstance(expression, Unary):
            if expression.operator == "move":
                raise Diagnostic(expression.location, "backend", "native backend does not yet support Resource ownership")
            if (expression.operator == "-" and isinstance(expression.operand, Literal)
                    and expression.operand.type_name == "Int" and expression.operand.value == 1 << 63):
                return IRLiteral(-(1 << 63), expression_type, expression.location)
            return IRUnary(
                expression.operator,
                self._expression(expression.operand),
                expression_type,
                expression.location,
            )
        if isinstance(expression, Binary):
            return IRBinary(
                self._expression(expression.left),
                expression.operator,
                self._expression(expression.right),
                expression_type,
                expression.location,
            )
        if isinstance(expression, Call):
            if expression.callee in {"println", "eprintln"}:
                raise Diagnostic(expression.location, "backend", "output functions are supported only as statements")
            if expression.type_arguments:
                raise Diagnostic(expression.location, "backend", "native backend does not yet support generic calls")
            symbol = self.symbols.get(expression.callee)
            if symbol is None:
                raise Diagnostic(
                    expression.location,
                    "backend",
                    f"native backend does not yet support built-in or unresolved call '{expression.callee}'",
                )
            return IRCall(
                symbol,
                tuple(self._expression(argument) for argument in expression.arguments),
                expression_type,
                expression.location,
            )
        if type(expression).__name__ == "MemberCall":
            raise Diagnostic(expression.location, "backend", "native backend does not yet support methods")
        if type(expression).__name__ == "MemberAccess":
            raise Diagnostic(expression.location, "backend", "native backend does not yet support fields or tuple access")
        if type(expression).__name__ == "TupleExpression":
            raise Diagnostic(expression.location, "backend", "native backend does not yet support tuples")
        raise Diagnostic(expression.location, "backend", f"native backend does not yet support {type(expression).__name__}")
