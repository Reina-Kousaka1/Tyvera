"""Lower the supported, already checked Tyvera subset into native IR."""

from __future__ import annotations

from .ast import (
    Assignment,
    AwaitStatement,
    BindingPattern,
    Binary,
    Block,
    Call,
    EnumPattern,
    Expression,
    ExpressionStatement,
    Function,
    ForStatement,
    IfStatement,
    LetStatement,
    Literal,
    LiteralPattern,
    LoopControl,
    MatchStatement,
    MemberAccess,
    MemberCall,
    Name,
    Program,
    ReturnStatement,
    Statement,
    Unary,
    UsingStatement,
    VariantPattern,
    WhileStatement,
    WildcardPattern,
)
from .compiler import CheckedProgram
from .ir import (
    IRAppliedType,
    IRAssign,
    IRAwait,
    IRBinary,
    IRBlock,
    IRBindingPattern,
    IRBuiltinCall,
    IRCall,
    IRCollectionCreate,
    IRCollectionLength,
    IRExpression,
    IRExpressionStatement,
    IRForEach,
    IRFunction,
    IRIf,
    IRLet,
    IRLiteral,
    IRLiteralPattern,
    IRLocalRef,
    IRLoopControl,
    IRMatch,
    IRMatchArm,
    IRMethodCall,
    IRMove,
    IRModule,
    IROutput,
    IRVariant,
    IRVariantPattern,
    IRReturn,
    IRUsing,
    IRType,
    IRTypeRef,
    IRUnary,
    IRVariable,
    IRWhile,
    IRWildcardPattern,
    ir_type_name,
    validate_ir,
)
from .source import Diagnostic, Location
from .type_names import split_type


_TYPE_MAP = {
    "Int": IRType.INT,
    "Float": IRType.FLOAT,
    "Bool": IRType.BOOL,
    "String": IRType.STRING,
    "Byte": IRType.BYTE,
    "Duration": IRType.DURATION,
    "Bytes": IRType.BYTES,
    "ByteBuffer": IRType.BYTE_BUFFER,
    "TextFile": IRType.TEXT_FILE,
    "TextReader": IRType.TEXT_READER,
    "TextWriter": IRType.TEXT_WRITER,
    "SocketAddress": IRType.SOCKET_ADDRESS,
    "TcpConnection": IRType.TCP_CONNECTION,
    "TlsConnection": IRType.TLS_CONNECTION,
    "TcpListener": IRType.TCP_LISTENER,
    "WebSocketUrl": IRType.WEBSOCKET_URL,
    "WebSocket": IRType.WEBSOCKET,
    "WebSocketMessage": IRType.WEBSOCKET_MESSAGE,
    "HttpMethod": IRType.HTTP_METHOD,
    "HttpUrl": IRType.HTTP_URL,
    "HttpHeaders": IRType.HTTP_HEADERS,
    "HttpRequest": IRType.HTTP_REQUEST,
    "HttpResponse": IRType.HTTP_RESPONSE,
    "Void": IRType.VOID,
    "Unit": IRType.VOID,
}
_APPLIED_TYPES = {"Option": 1, "Result": 2, "List": 1, "Map": 2, "Task": 1}


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
            function.is_async,
        )

    def _type(self, type_name: str, location: Location) -> IRTypeRef:
        seen: set[str] = set()
        while True:
            base, arguments = split_type(type_name)
            target = self.checked.type_alias_targets.get(base)
            if target is None or arguments:
                break
            if base in seen:
                raise Diagnostic(location, "backend", f"native backend found a recursive type alias '{base}'")
            seen.add(base)
            type_name = target
        base, arguments = split_type(type_name)
        result = _TYPE_MAP.get(base) if not arguments else None
        if result is not None:
            return result
        expected_arity = _APPLIED_TYPES.get(base)
        if expected_arity is not None and len(arguments) == expected_arity:
            return IRAppliedType(base, tuple(self._type(argument, location) for argument in arguments))
        if expected_arity is not None:
            raise Diagnostic(
                location,
                "backend",
                f"native backend cannot lower malformed Tyvera type '{type_name}'",
            )
        raise Diagnostic(
            location,
            "backend",
            f"native backend does not yet support Tyvera type '{type_name}'",
        )

    def _expression_type(self, expression: Expression) -> IRTypeRef:
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
        if isinstance(statement, AwaitStatement):
            task = self._expression(statement.value)
            if not isinstance(task.type, IRAppliedType) or task.type.name != "Task":
                raise Diagnostic(statement.location, "backend", "native backend currently supports await only for Task<T>")
            result = None
            if statement.binding_name is not None:
                result = self._new_variable(
                    statement.binding_name,
                    ir_type_name(task.type.arguments[0]),
                    mutable=statement.mutable,
                    location=statement.location,
                )
                if statement.annotation is not None and result.type != self._type(statement.annotation, statement.location):
                    raise Diagnostic(statement.location, "backend", "native await annotation does not match its checked result")
            return IRAwait(task, result, statement.location)
        if isinstance(statement, Block):
            return IRBlock(self._statements(statement.body, nested_scope=True), statement.location)
        if isinstance(statement, UsingStatement):
            acquisition = self._expression(statement.resource)
            if not isinstance(acquisition.type, IRAppliedType) or acquisition.type.name != "Result":
                raise Diagnostic(statement.location, "backend", "native using requires a checked Result acquisition")
            resource_type = acquisition.type.arguments[0]
            if resource_type not in {
                IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER,
                IRType.TCP_CONNECTION, IRType.TLS_CONNECTION, IRType.TCP_LISTENER,
                IRType.WEBSOCKET,
            }:
                raise Diagnostic(statement.location, "backend", "native using supports file, TCP, and TLS resources")
            if self.checked.ownership_transfers.get(statement.location) != (
                f"{ir_type_name(resource_type)} to scoped owner {statement.name}"
            ):
                raise Diagnostic(statement.location, "backend", "native using is missing checked ownership-transfer metadata")
            self.scopes.append({})
            try:
                resource = self._new_variable(
                    statement.name, ir_type_name(resource_type), mutable=False, location=statement.location,
                )
                body = self._statements(statement.body)
            finally:
                self.scopes.pop()
            error_variable = None
            else_body = ()
            if statement.error_name is not None:
                self.scopes.append({})
                try:
                    error_variable = self._new_variable(
                        statement.error_name, "String", mutable=False, location=statement.location,
                    )
                    else_body = self._statements(statement.else_body)
                finally:
                    self.scopes.pop()
            elif statement.else_body:
                raise Diagnostic(statement.location, "backend", "native using else body requires an error binding")
            return IRUsing(resource, acquisition, body, error_variable, else_body, statement.location)
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
        if isinstance(statement, ForStatement):
            iterable = self._expression(statement.iterable)
            if not isinstance(iterable.type, IRAppliedType) or iterable.type.name not in {"List", "Map"}:
                raise Diagnostic(statement.location, "backend", "native for loops currently require List or Map")
            element_type = iterable.type.arguments[0]
            self.scopes.append({})
            self.loop_depth += 1
            try:
                variable = self._new_variable(
                    statement.name, ir_type_name(element_type), mutable=False, location=statement.location,
                )
                body = self._statements(statement.body)
            finally:
                self.loop_depth -= 1
                self.scopes.pop()
            return IRForEach(variable, iterable, body, statement.location)
        if isinstance(statement, MatchStatement):
            value = self._expression(statement.value)
            arms: list[IRMatchArm] = []
            for arm in statement.arms:
                self.scopes.append({})
                try:
                    pattern = self._pattern(arm.pattern, value.type)
                    body = self._statements(arm.body)
                    arms.append(IRMatchArm(pattern, body, arm.location))
                finally:
                    self.scopes.pop()
            exhaustive = self.checked.match_exhaustiveness.get(statement.location)
            coverage = self.checked.match_coverage.get(statement.location)
            if exhaustive is None or coverage is None:
                raise Diagnostic(statement.location, "backend", "native backend is missing checked match coverage metadata")
            return IRMatch(value, tuple(arms), exhaustive, coverage, statement.location)
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
            "AwaitStatement": "Tasks and await",
            "UsingStatement": "owned resources and deterministic cleanup",
        }.get(type(statement).__name__, type(statement).__name__)
        raise Diagnostic(statement.location, "backend", f"native backend does not yet support {unsupported}")

    def _pattern(self, pattern, expected_type: IRTypeRef):
        if isinstance(pattern, WildcardPattern):
            return IRWildcardPattern(pattern.location)
        if isinstance(pattern, BindingPattern):
            variable = self._new_variable(
                pattern.name, ir_type_name(expected_type), mutable=False, location=pattern.location,
            )
            return IRBindingPattern(variable, pattern.location)
        if isinstance(pattern, LiteralPattern):
            literal_type = self._type(pattern.type_name, pattern.location)
            if literal_type != expected_type:
                raise Diagnostic(pattern.location, "backend", "native pattern type does not match the checked match value")
            return IRLiteralPattern(IRLiteral(pattern.value, literal_type, pattern.location), pattern.location)
        if isinstance(pattern, VariantPattern):
            if not isinstance(expected_type, IRAppliedType):
                raise Diagnostic(pattern.location, "backend", "native variant matching requires Option or Result")
            if expected_type.name == "Option":
                if pattern.variant == "None":
                    if pattern.binding is not None or pattern.payload_pattern is not None:
                        raise Diagnostic(pattern.location, "backend", "invalid checked None pattern")
                    return IRVariantPattern("None", None, pattern.location)
                if pattern.variant != "Some":
                    raise Diagnostic(pattern.location, "backend", f"Option has no native pattern '{pattern.variant}'")
                payload_type = expected_type.arguments[0]
            elif expected_type.name == "Result":
                if pattern.variant == "Ok":
                    payload_type = expected_type.arguments[0]
                elif pattern.variant == "Err":
                    payload_type = expected_type.arguments[1]
                else:
                    raise Diagnostic(pattern.location, "backend", f"Result has no native pattern '{pattern.variant}'")
            else:
                raise Diagnostic(pattern.location, "backend", "native variant matching requires Option or Result")
            payload = pattern.payload_pattern
            if payload is None:
                if pattern.binding is None:
                    raise Diagnostic(pattern.location, "backend", f"native {pattern.variant} pattern requires a payload pattern")
                lowered_payload = IRBindingPattern(
                    self._new_variable(pattern.binding, ir_type_name(payload_type), mutable=False,
                                       location=pattern.location),
                    pattern.location,
                )
            else:
                lowered_payload = self._pattern(payload, payload_type)
            return IRVariantPattern(pattern.variant, lowered_payload, pattern.location)
        if isinstance(pattern, EnumPattern):
            raise Diagnostic(pattern.location, "backend", "native backend does not yet support user-defined enum patterns")
        raise Diagnostic(pattern.location, "backend", f"native backend does not yet support {type(pattern).__name__} patterns")

    def _expression(self, expression: Expression) -> IRExpression:
        if isinstance(expression, Unary) and expression.operator == "move":
            if not isinstance(expression.operand, Name):
                raise Diagnostic(expression.location, "backend", "native move requires a local resource binding")
            source = self._lookup(expression.operand.identifier, expression.operand.location)
            if self.checked.move_sites.get(expression.location) != expression.operand.identifier:
                raise Diagnostic(expression.location, "backend", "native move is missing checked ownership metadata")
            return IRMove(
                IRLocalRef(source.identifier, source.type, expression.operand.location),
                source.type,
                expression.location,
            )
        if isinstance(expression, Name):
            variable = self._lookup(expression.identifier, expression.location)
            return IRLocalRef(variable.identifier, variable.type, expression.location)
        expression_type = self._expression_type(expression)
        if isinstance(expression, Literal):
            return IRLiteral(expression.value, expression_type, expression.location)
        if isinstance(expression, Unary):
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
        if isinstance(expression, MemberAccess):
            receiver = self._expression(expression.receiver)
            if (expression.member == "length" and isinstance(receiver.type, IRAppliedType)
                    and receiver.type.name in {"List", "Map"}):
                return IRCollectionLength(receiver, expression_type, expression.location)
            raise Diagnostic(expression.location, "backend", "native backend does not yet support this field access")
        if isinstance(expression, MemberCall):
            receiver = self._expression(expression.receiver)
            if expression.type_arguments:
                raise Diagnostic(expression.location, "backend", "native backend does not yet support generic method calls")
            if isinstance(receiver.type, IRAppliedType) and receiver.type.name == "Task" and expression.method not in {
                "cancel", "isCancelled", "isDone", "withTimeout",
            }:
                raise Diagnostic(
                    expression.location, "backend",
                    f"native backend does not yet support Task.{expression.method}",
                )
            if not (
                isinstance(receiver.type, IRAppliedType) and receiver.type.name in {"List", "Map"}
                or isinstance(receiver.type, IRAppliedType) and receiver.type.name == "Task"
                or receiver.type in {
                    IRType.BYTE, IRType.DURATION, IRType.BYTES, IRType.BYTE_BUFFER,
                    IRType.TEXT_FILE, IRType.TEXT_READER, IRType.TEXT_WRITER,
                    IRType.SOCKET_ADDRESS, IRType.TCP_CONNECTION, IRType.TLS_CONNECTION, IRType.TCP_LISTENER,
                    IRType.WEBSOCKET, IRType.WEBSOCKET_URL, IRType.WEBSOCKET_MESSAGE,
                }
            ):
                raise Diagnostic(expression.location, "backend", f"native backend does not yet support methods on {ir_type_name(receiver.type)}")
            if (
                isinstance(receiver.type, IRAppliedType) and receiver.type.name == "Task"
                and expression.method == "withTimeout"
                and self.checked.move_sites.get(expression.location) is not None
            ):
                if not isinstance(expression.receiver, Name):
                    raise Diagnostic(expression.location, "backend", "native Task.withTimeout requires a local Task binding")
                source = self._lookup(expression.receiver.identifier, expression.receiver.location)
                receiver = IRMove(
                    IRLocalRef(source.identifier, source.type, expression.receiver.location),
                    source.type,
                    expression.location,
                )
            arguments = tuple(self._expression(argument) for argument in expression.arguments)
            return IRMethodCall(receiver, expression.method, arguments, expression_type, expression.location)
        if isinstance(expression, Call):
            if expression.callee in {"println", "eprintln"}:
                raise Diagnostic(expression.location, "backend", "output functions are supported only as statements")
            if expression.callee in {"Some", "None", "Ok", "Err"}:
                payload = None if expression.callee == "None" else self._expression(expression.arguments[0])
                return IRVariant(expression.callee, payload, expression_type, expression.location)
            if expression.callee in {"List", "Map"}:
                return IRCollectionCreate(expression_type, expression.location)
            if expression.callee in {
                "milliseconds", "seconds", "durationFromSeconds", "byteFromInt", "bytesFromString", "byteBuffer", "readText", "writeText",
                "readBytes", "writeBytes", "pathJoin", "pathExists", "openText",
                "openTextReader", "openTextWriter", "hostOS", "hostArchitecture",
                "monotonicMilliseconds", "runTasks", "resolveHost", "tcpConnect", "tlsConnect", "tcpListen",
                "webSocketUrl", "webSocketConnect", "httpMethod", "httpUrl", "httpHeaders",
                "httpRequest", "httpSend", "httpGet", "httpPost", "httpPut", "httpDelete",
            }:
                return IRBuiltinCall(
                    expression.callee,
                    tuple(self._expression(argument) for argument in expression.arguments),
                    expression_type,
                    expression.location,
                )
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
        if type(expression).__name__ == "TupleExpression":
            raise Diagnostic(expression.location, "backend", "native backend does not yet support tuples")
        raise Diagnostic(expression.location, "backend", f"native backend does not yet support {type(expression).__name__}")

