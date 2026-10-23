from __future__ import annotations

from .ast import (
    Assignment,
    ActorDeclaration,
    AwaitStatement,
    Binary,
    BindingPattern,
    Block,
    ClassDeclaration,
    Call,
    Expression,
    ExpressionStatement,
    EnumDeclaration,
    EnumPattern,
    EnumVariant,
    Field,
    ForStatement,
    Function,
    IfStatement,
    ImportDeclaration,
    InterfaceDeclaration,
    LetStatement,
    Literal,
    LoopControl,
    LiteralPattern,
    MatchArm,
    MatchStatement,
    MemberAccess,
    MemberCall,
    Method,
    MethodSignature,
    Name,
    Parameter,
    Program,
    ReturnStatement,
    RecordDeclaration,
    RecordPattern,
    NewtypeDeclaration,
    TypeAliasDeclaration,
    Statement,
    TupleExpression,
    TuplePattern,
    Unary,
    UsingStatement,
    VariantPattern,
    WildcardPattern,
    WhileStatement,
)
from .source import Diagnostic
from .tokens import Token


_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "EQUAL_EQUAL": 3,
    "BANG_EQUAL": 3,
    "LESS": 4,
    "LESS_EQUAL": 4,
    "GREATER": 4,
    "GREATER_EQUAL": 4,
    "PLUS": 5,
    "MINUS": 5,
    "STAR": 6,
    "SLASH": 6,
}
_OPERATOR = {
    "OR": "or",
    "AND": "and",
    "EQUAL_EQUAL": "==",
    "BANG_EQUAL": "!=",
    "LESS": "<",
    "LESS_EQUAL": "<=",
    "GREATER": ">",
    "GREATER_EQUAL": ">=",
    "PLUS": "+",
    "MINUS": "-",
    "STAR": "*",
    "SLASH": "/",
}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse(self) -> Program:
        functions: list[Function] = []
        classes: list[ClassDeclaration] = []
        interfaces: list[InterfaceDeclaration] = []
        imports: list[ImportDeclaration] = []
        actors: list[ActorDeclaration] = []
        records: list[RecordDeclaration] = []
        enums: list[EnumDeclaration] = []
        newtypes: list[NewtypeDeclaration] = []
        type_aliases: list[TypeAliasDeclaration] = []
        self._skip_separators()
        while not self._check("EOF"):
            public = self._match("PUB")
            is_async = self._match("ASYNC")
            if self._check("IMPORT"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                if public:
                    self._error(self._previous(), "imports cannot be declared public")
                imports.append(self._import())
            elif self._check("FN"):
                functions.append(self._function(public, is_async))
            elif self._check("CLASS"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                classes.append(self._class_declaration(public))
            elif self._check("INTERFACE"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                interfaces.append(self._interface_declaration(public))
            elif self._check("ACTOR"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                actors.append(self._actor_declaration(public))
            elif self._check("RECORD"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                records.append(self._record_declaration(public))
            elif self._check("ENUM"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                enums.append(self._enum_declaration(public))
            elif self._check("NEWTYPE"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                newtypes.append(self._newtype_declaration(public))
            elif self._check("TYPE"):
                if is_async:
                    self._error(self._previous(), "'async' can only modify a function declaration")
                type_aliases.append(self._type_alias_declaration(public))
            else:
                self._error(self._current(), "expected an import or function, class, interface, actor, record, enum, type alias, or newtype declaration")
            if not self._check_any("NEWLINE", "SEMICOLON", "EOF"):
                self._error(self._current(), "expected a line break after the declaration")
            self._skip_separators()
        aliases: set[str] = set()
        for declaration in imports:
            if declaration.alias in aliases:
                raise Diagnostic(declaration.location, "module", f"import alias '{declaration.alias}' is already declared")
            aliases.add(declaration.alias)
        return Program(functions, classes, interfaces, imports, actors, records, enums, newtypes, type_aliases)

    def _import(self) -> ImportDeclaration:
        token = self._expect("IMPORT", "expected 'import'")
        path = self._expect("STRING", "expected a quoted .tyv module path after 'import'")
        self._expect("AS", "expected 'as' and a module alias after the import path")
        alias = self._expect("IDENTIFIER", "expected an import alias after 'as'")
        if not path.value.endswith(".tyv"):
            self._error(path, "module import paths must end in '.tyv'")
        return ImportDeclaration(path.value, alias.lexeme, token.location)

    def _function(self, public: bool = False, is_async: bool = False) -> Function:
        fn_token = self._expect("FN", "expected 'fn'")
        name_token = self._expect("IDENTIFIER", "expected a function name after 'fn'")
        type_parameters = self._type_parameters(allow_constraints=True)
        type_constraints = {name: list(bounds) for name, bounds in self._last_type_constraints.items()}
        name, parameters, return_type = self._function_header(name_token)
        body = self._block()
        return Function(name, parameters, return_type, body, fn_token.location, public, type_parameters, is_async,
                        type_constraints)

    def _function_header(self, name_token: Token | None = None) -> tuple[str, list[Parameter], str]:
        if name_token is None:
            name_token = self._expect("IDENTIFIER", "expected a function name after 'fn'")
        self._expect("LPAREN", "expected '(' after function name")
        parameters: list[Parameter] = []
        if not self._check("RPAREN"):
            while True:
                parameter_name = self._expect("IDENTIFIER", "expected a parameter name")
                self._expect("COLON", "expected ':' after parameter name")
                type_name = self._type_name("expected a type name after ':'")
                parameters.append(Parameter(parameter_name.lexeme, type_name, parameter_name.location))
                if not self._match("COMMA"):
                    break
                if self._check("RPAREN"):
                    break
        self._expect("RPAREN", "expected ')' after parameters")
        return_type = "Void"
        if self._match("ARROW"):
            return_type = self._type_name("expected a return type after '->'")
        return name_token.lexeme, parameters, return_type

    def _type_name(self, message: str) -> str:
        if self._match("LPAREN"):
            elements = [self._type_name("expected a tuple element type")]
            if not self._match("COMMA"):
                self._error(self._current(), "tuple types require at least two element types")
            elements.append(self._type_name("expected a tuple element type after ','"))
            while self._match("COMMA"):
                if self._check("RPAREN"):
                    break
                elements.append(self._type_name("expected a tuple element type after ','"))
            self._expect("RPAREN", "expected ')' after tuple type")
            return "Tuple<" + ",".join(elements) + ">"
        parts = [self._expect("IDENTIFIER", message).lexeme]
        while self._match("DOT"):
            parts.append(self._expect("IDENTIFIER", "expected a type name after '.'").lexeme)
        name = ".".join(parts)
        if self._match("LESS"):
            arguments = [self._type_name("expected a type argument after '<'")]
            while self._match("COMMA"):
                arguments.append(self._type_name("expected a type argument after ','"))
            self._expect("GREATER", "expected '>' after type arguments")
            name += "<" + ",".join(arguments) + ">"
        return name

    def _type_arguments(self) -> list[str]:
        self._expect("LESS", "expected '<' before type arguments")
        arguments = [self._type_name("expected a type argument after '<'")]
        while self._match("COMMA"):
            arguments.append(self._type_name("expected a type argument after ','"))
        self._expect("GREATER", "expected '>' after type arguments")
        return arguments

    def _type_parameters(self, *, allow_constraints: bool = False) -> list[str]:
        self._last_type_constraints: dict[str, list[str]] = {}
        if not self._match("LESS"):
            return []
        parameters: list[str] = []
        while True:
            name = self._expect("IDENTIFIER", "expected a type parameter after '<' or ','").lexeme
            parameters.append(name)
            bounds: list[str] = []
            if self._match("COLON"):
                if not allow_constraints:
                    self._error(self._previous(), "constraints are currently supported only on generic functions")
                bounds.append(self._type_name("expected an interface constraint after ':'"))
                while self._match("PLUS"):
                    bounds.append(self._type_name("expected an interface constraint after '+'"))
            self._last_type_constraints[name] = bounds
            if not self._match("COMMA"):
                break
        self._expect("GREATER", "expected '>' after type parameters")
        if len(set(parameters)) != len(parameters):
            self._error(self._previous(), "generic type parameters must be unique")
        return parameters

    def _class_declaration(self, public: bool = False) -> ClassDeclaration:
        class_token = self._expect("CLASS", "expected 'class'")
        name = self._expect("IDENTIFIER", "expected a class name after 'class'")
        type_parameters = self._type_parameters()
        interfaces: list[str] = []
        if self._match("IMPLEMENTS"):
            while True:
                interfaces.append(self._type_name("expected an interface name after 'implements'"))
                if not self._match("COMMA"):
                    break
        self._expect("LBRACE", "expected '{' after class header")
        fields: list[Field] = []
        methods: list[Method] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            if self._match("LET", "VAR"):
                declaration = self._previous()
                field_name = self._expect("IDENTIFIER", "expected a field name")
                self._expect("COLON", "expected ':' after field name")
                field_type = self._type_name("expected a field type after ':'")
                fields.append(Field(field_name.lexeme, field_type, declaration.kind == "VAR", declaration.location))
            elif self._check("FN"):
                method_token = self._advance()
                method_name, parameters, return_type = self._function_header()
                body = self._block()
                methods.append(Method(method_name, parameters, return_type, body, method_token.location))
            else:
                self._error(self._current(), "expected a field declaration or method in class body")
            if not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
                self._error(self._current(), "expected a line break or ';' after class member")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close class body")
        return ClassDeclaration(name.lexeme, interfaces, fields, methods, class_token.location, public, type_parameters)

    def _record_declaration(self, public: bool = False) -> RecordDeclaration:
        record_token = self._expect("RECORD", "expected 'record'")
        name = self._expect("IDENTIFIER", "expected a record name after 'record'")
        type_parameters = self._type_parameters()
        self._expect("LBRACE", "expected '{' after record name")
        fields: list[Field] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            field_name = self._expect("IDENTIFIER", "expected a record field name")
            self._expect("COLON", "expected ':' after record field name")
            field_type = self._type_name("expected a record field type after ':'")
            fields.append(Field(field_name.lexeme, field_type, False, field_name.location))
            if self._match("COMMA"):
                self._skip_separators()
            elif not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF", "IDENTIFIER"):
                self._error(self._current(), "expected a line break, ',', or ';' after record field")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close record declaration")
        return RecordDeclaration(name.lexeme, fields, record_token.location, public, type_parameters)

    def _enum_declaration(self, public: bool = False) -> EnumDeclaration:
        enum_token = self._expect("ENUM", "expected 'enum'")
        name = self._expect("IDENTIFIER", "expected an enum name after 'enum'")
        type_parameters = self._type_parameters()
        self._expect("LBRACE", "expected '{' after enum name")
        variants: list[EnumVariant] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            variant = self._expect("IDENTIFIER", "expected an enum variant name")
            payload_types: list[str] = []
            if self._match("LPAREN"):
                if not self._check("RPAREN"):
                    payload_types.append(self._type_name("expected a variant payload type"))
                    while self._match("COMMA"):
                        payload_types.append(self._type_name("expected a variant payload type after ','"))
                self._expect("RPAREN", "expected ')' after enum payload types")
            variants.append(EnumVariant(variant.lexeme, payload_types, variant.location))
            if self._match("COMMA"):
                self._skip_separators()
            elif not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF", "IDENTIFIER"):
                self._error(self._current(), "expected a line break, ',', or ';' after enum variant")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close enum declaration")
        if not variants:
            self._error(enum_token, "enum must declare at least one variant")
        return EnumDeclaration(name.lexeme, variants, enum_token.location, public, type_parameters)

    def _newtype_declaration(self, public: bool = False) -> NewtypeDeclaration:
        newtype_token = self._expect("NEWTYPE", "expected 'newtype'")
        name = self._expect("IDENTIFIER", "expected a newtype name after 'newtype'")
        type_parameters = self._type_parameters()
        self._expect("EQUAL", "expected '=' after newtype name")
        underlying_type = self._type_name("expected a representation type after '='")
        return NewtypeDeclaration(name.lexeme, underlying_type, newtype_token.location, public, type_parameters)

    def _type_alias_declaration(self, public: bool = False) -> TypeAliasDeclaration:
        alias_token = self._expect("TYPE", "expected 'type'")
        name = self._expect("IDENTIFIER", "expected a type alias name after 'type'")
        type_parameters = self._type_parameters()
        self._expect("EQUAL", "expected '=' after type alias name")
        target_type = self._type_name("expected a target type after '='")
        return TypeAliasDeclaration(name.lexeme, target_type, alias_token.location, public, type_parameters)

    def _interface_declaration(self, public: bool = False) -> InterfaceDeclaration:
        interface_token = self._expect("INTERFACE", "expected 'interface'")
        name = self._expect("IDENTIFIER", "expected an interface name after 'interface'")
        self._expect("LBRACE", "expected '{' after interface name")
        methods: list[MethodSignature] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            method_token = self._expect("FN", "expected a method signature beginning with 'fn'")
            method_name, parameters, return_type = self._function_header()
            if self._check("LBRACE"):
                self._error(self._current(), "interface methods declare signatures and cannot have bodies")
            methods.append(MethodSignature(method_name, parameters, return_type, method_token.location))
            if not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
                self._error(self._current(), "expected a line break or ';' after interface method")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close interface body")
        return InterfaceDeclaration(name.lexeme, methods, interface_token.location, public)

    def _actor_declaration(self, public: bool = False) -> ActorDeclaration:
        actor_token = self._expect("ACTOR", "expected 'actor'")
        name = self._expect("IDENTIFIER", "expected an actor name after 'actor'")
        self._expect("LBRACE", "expected '{' after actor name")
        fields: list[Field] = []
        handlers: list[Method] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            if self._match("LET", "VAR"):
                declaration = self._previous()
                field_name = self._expect("IDENTIFIER", "expected an actor state field name")
                self._expect("COLON", "expected ':' after actor state field name")
                field_type = self._type_name("expected an actor state type after ':'")
                fields.append(Field(field_name.lexeme, field_type, declaration.kind == "VAR", declaration.location))
            elif self._check("IDENTIFIER") and self._current().lexeme == "init":
                method_token = self._advance()
                method_name, parameters, return_type = self._function_header(method_token)
                handlers.append(Method(method_name, parameters, return_type, self._block(), method_token.location))
            elif self._match("ON"):
                method_token = self._previous()
                handler_name = self._expect("IDENTIFIER", "expected a message name after 'on'")
                if handler_name.lexeme == "init":
                    self._error(handler_name, "'init' is the actor constructor and cannot be declared as a message handler")
                _, parameters, return_type = self._function_header(handler_name)
                handlers.append(Method(handler_name.lexeme, parameters, return_type, self._block(), method_token.location))
            else:
                self._error(self._current(), "expected an actor state field, 'init' constructor, or 'on' message handler")
            if not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
                self._error(self._current(), "expected a line break or ';' after actor member")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close actor body")
        return ActorDeclaration(name.lexeme, fields, handlers, actor_token.location, public)

    def _block(self) -> list[Statement]:
        self._expect("LBRACE", "expected '{' to start the block")
        statements: list[Statement] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            statements.append(self._statement())
            if not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
                self._error(self._current(), "expected a line break or ';' after the statement")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close the block")
        return statements

    def _statement(self) -> Statement:
        if self._match("USING"):
            token = self._previous()
            name = self._expect("IDENTIFIER", "expected a resource name after 'using'")
            self._expect("EQUAL", "expected '=' after the resource name")
            resource = self._expression()
            body = self._block()
            saved = self.index
            while self._match("NEWLINE"):
                pass
            error_name = None
            else_body: list[Statement] = []
            if self._match("ELSE"):
                error_name = self._expect("IDENTIFIER", "expected an error variable after 'else'").lexeme
                else_body = self._block()
            else:
                self.index = saved
            return UsingStatement(name.lexeme, resource, body, token.location, error_name, else_body)
        if self._match("AWAIT"):
            token = self._previous()
            return AwaitStatement(self._expression(), token.location)
        if self._match("LET", "VAR"):
            return self._let_statement(self._previous())
        if self._match("IF"):
            return self._if_statement(self._previous())
        if self._match("WHILE"):
            token = self._previous()
            condition = self._expression()
            return WhileStatement(condition, self._block(), token.location)
        if self._match("FOR"):
            token = self._previous()
            name = self._expect("IDENTIFIER", "expected a loop variable after 'for'")
            self._expect("IN", "expected 'in' after the loop variable")
            iterable = self._expression()
            return ForStatement(name.lexeme, iterable, self._block(), token.location)
        if self._match("MATCH"):
            return self._match_statement(self._previous())
        if self._match("BREAK", "CONTINUE"):
            return LoopControl(self._previous().lexeme, self._previous().location)
        if self._check("LBRACE"):
            location = self._current().location
            return Block(self._block(), location)
        if self._match("RETURN"):
            return self._return_statement(self._previous())
        token = self._current()
        expression = self._expression()
        if self._match("EQUAL"):
            if not isinstance(expression, (Name, MemberAccess)):
                self._error(token, "assignment target must be a variable or field")
            return Assignment(expression.identifier if isinstance(expression, Name) else expression,
                              self._expression(), token.location)
        if not isinstance(expression, (Call, MemberCall)):
            self._error(token, "only a function call can be used as an expression statement")
        return ExpressionStatement(expression, token.location)

    def _let_statement(self, let_token: Token) -> LetStatement:
        name = self._expect("IDENTIFIER", f"expected a variable name after '{let_token.lexeme}'")
        annotation: str | None = None
        if self._match("COLON"):
            annotation = self._type_name("expected a type name after ':'")
        self._expect("EQUAL", "expected '=' in variable declaration")
        if self._match("AWAIT"):
            await_token = self._previous()
            return AwaitStatement(self._expression(), await_token.location,
                                  name.lexeme, annotation, let_token.kind == "VAR")
        value = self._expression()
        return LetStatement(name.lexeme, annotation, value, let_token.location, let_token.kind == "VAR")

    def _if_statement(self, token: Token) -> IfStatement:
        condition = self._expression()
        then_body = self._block()
        # Only consume intervening newlines if an else follows. Otherwise they
        # still separate this compound statement from the next statement.
        saved = self.index
        while self._match("NEWLINE"):
            pass
        else_body: list[Statement] = []
        if self._match("ELSE"):
            if self._match("IF"):
                else_body = [self._if_statement(self._previous())]
            else:
                else_body = self._block()
        else:
            self.index = saved
        return IfStatement(condition, then_body, else_body, token.location)

    def _return_statement(self, return_token: Token) -> ReturnStatement:
        if self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
            return ReturnStatement(None, return_token.location)
        return ReturnStatement(self._expression(), return_token.location)

    def _expression(self, minimum_precedence: int = 0) -> Expression:
        expression = self._prefix()
        while True:
            if isinstance(expression, Name) and self._check("LESS"):
                saved = self.index
                try:
                    type_arguments = self._type_arguments()
                except Diagnostic:
                    self.index = saved
                else:
                    if self._check("LPAREN"):
                        expression = self._call(expression, type_arguments)
                        continue
                    self.index = saved
            if self._check("LPAREN") and isinstance(expression, Name):
                expression = self._call(expression)
                continue
            if self._check("DOT"):
                dot = self._advance()
                if self._check_any("IDENTIFIER", "INTEGER"):
                    member = self._advance()
                else:
                    self._error(self._current(), "expected a field, tuple index, or method name after '.'")
                type_arguments: list[str] = []
                if self._check("LESS"):
                    saved = self.index
                    try:
                        parsed_arguments = self._type_arguments()
                    except Diagnostic:
                        self.index = saved
                    else:
                        if self._check("LPAREN"):
                            type_arguments = parsed_arguments
                        else:
                            self.index = saved
                if self._check("LPAREN"):
                    expression = self._member_call(expression, member.lexeme, dot.location, type_arguments)
                else:
                    expression = MemberAccess(expression, member.lexeme, dot.location)
                continue
            precedence = _PRECEDENCE.get(self._current().kind, -1)
            if precedence < minimum_precedence:
                break
            operator_token = self._advance()
            right = self._expression(precedence + 1)
            expression = Binary(
                expression,
                _OPERATOR[operator_token.kind],
                right,
                operator_token.location,
            )
        return expression

    def _prefix(self) -> Expression:
        token = self._advance()
        if token.kind == "INTEGER":
            return Literal(token.value, "Int", token.location)
        if token.kind == "FLOAT":
            return Literal(token.value, "Float", token.location)
        if token.kind == "STRING":
            return Literal(token.value, "String", token.location)
        if token.kind in {"TRUE", "FALSE"}:
            return Literal(token.kind == "TRUE", "Bool", token.location)
        if token.kind == "IDENTIFIER":
            return Name(token.lexeme, token.location)
        if token.kind in {"MINUS", "BANG"}:
            operator = "-" if token.kind == "MINUS" else "!"
            return Unary(operator, self._expression(7), token.location)
        if token.kind == "LPAREN":
            expression = self._expression()
            if self._match("COMMA"):
                elements = [expression]
                while True:
                    if self._check("RPAREN"):
                        break
                    elements.append(self._expression())
                    if not self._match("COMMA"):
                        break
                self._expect("RPAREN", "expected ')' after tuple expression")
                if len(elements) < 2:
                    self._error(token, "tuple values require at least two elements")
                return TupleExpression(elements, token.location)
            self._expect("RPAREN", "expected ')' after expression")
            return expression
        self._error(token, "expected an expression")

    def _match_statement(self, token: Token) -> MatchStatement:
        value = self._expression()
        self._expect("LBRACE", "expected '{' after match value")
        arms: list[MatchArm] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            pattern = self._pattern()
            self._expect("FAT_ARROW", "expected '=>' after match pattern")
            if self._check("LBRACE"):
                body = self._block()
            else:
                body = [self._statement()]
            arms.append(MatchArm(pattern, body, pattern.location))
            if not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
                self._error(self._current(), "expected a line break after match arm")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' after match arms")
        if not arms:
            self._error(token, "match must contain at least one arm")
        return MatchStatement(value, arms, token.location)

    def _pattern(self):
        token = self._current()
        if self._match("LPAREN"):
            first = self._pattern()
            if not self._match("COMMA"):
                self._expect("RPAREN", "expected ')' after grouped pattern")
                return first
            elements = [first, self._pattern()]
            while self._match("COMMA"):
                if self._check("RPAREN"):
                    break
                elements.append(self._pattern())
            self._expect("RPAREN", "expected ')' after tuple pattern")
            return TuplePattern(elements, token.location)
        if token.kind == "MINUS":
            self._advance()
            number = self._advance()
            if number.kind not in {"INTEGER", "FLOAT"}:
                self._error(number, "expected a numeric literal after '-' in pattern")
            type_name = "Int" if number.kind == "INTEGER" else "Float"
            return LiteralPattern(-number.value, type_name, token.location)
        if token.kind in {"INTEGER", "FLOAT", "STRING", "TRUE", "FALSE"}:
            self._advance()
            value = token.value if token.kind in {"INTEGER", "FLOAT", "STRING"} else token.kind == "TRUE"
            type_name = {"INTEGER": "Int", "FLOAT": "Float", "STRING": "String",
                         "TRUE": "Bool", "FALSE": "Bool"}[token.kind]
            return LiteralPattern(value, type_name, token.location)
        if token.kind == "IDENTIFIER":
            self._advance()
            if token.lexeme == "_":
                return WildcardPattern(token.location)
            if self._match("DOT"):
                type_or_variant = self._expect("IDENTIFIER", "expected a type or enum variant after '.'")
                enum_name = token.lexeme
                variant = type_or_variant
                if self._match("DOT"):
                    enum_name = f"{token.lexeme}.{type_or_variant.lexeme}"
                    variant = self._expect("IDENTIFIER", "expected an enum variant after enum type")
                payload_patterns: list = []
                if self._match("LPAREN"):
                    if not self._check("RPAREN"):
                        payload_patterns.append(self._pattern())
                        while self._match("COMMA"):
                            if self._check("RPAREN"):
                                break
                            payload_patterns.append(self._pattern())
                    self._expect("RPAREN", "expected ')' after enum variant pattern")
                return EnumPattern(enum_name, variant.lexeme, payload_patterns, token.location)
            if self._match("LPAREN"):
                if token.lexeme == "None":
                    self._error(token, "None pattern does not take a payload")
                patterns: list = []
                if not self._check("RPAREN"):
                    patterns.append(self._pattern())
                    while self._match("COMMA"):
                        if self._check("RPAREN"):
                            break
                        patterns.append(self._pattern())
                self._expect("RPAREN", "expected ')' after constructor pattern")
                if token.lexeme in {"Some", "Ok", "Err"}:
                    if len(patterns) != 1:
                        self._error(token, f"'{token.lexeme}' pattern requires exactly one payload pattern")
                    payload_pattern = patterns[0]
                    binding = payload_pattern.name if isinstance(payload_pattern, BindingPattern) else None
                    return VariantPattern(token.lexeme, binding, token.location, payload_pattern)
                return RecordPattern(token.lexeme, patterns, token.location)
            if token.lexeme == "None":
                return VariantPattern("None", None, token.location)
            if token.lexeme in {"Some", "Ok", "Err"}:
                self._error(token, f"'{token.lexeme}' pattern requires a payload")
            return BindingPattern(token.lexeme, token.location)
        self._error(token, "expected a literal, wildcard, binding, or variant pattern")

    def _call(self, callee: Name, type_arguments: list[str] | None = None) -> Call:
        self._expect("LPAREN", "expected '(' after function name")
        arguments = self._arguments()
        self._expect("RPAREN", "expected ')' after function arguments")
        return Call(callee.identifier, arguments, callee.location, type_arguments or [])

    def _member_call(
        self, receiver: Expression, method: str, location, type_arguments: list[str] | None = None,
    ) -> MemberCall:
        self._expect("LPAREN", "expected '(' after method name")
        arguments = self._arguments()
        self._expect("RPAREN", "expected ')' after method arguments")
        return MemberCall(receiver, method, arguments, location, type_arguments or [])

    def _arguments(self) -> list[Expression]:
        arguments: list[Expression] = []
        if not self._check("RPAREN"):
            while True:
                arguments.append(self._expression())
                if not self._match("COMMA"):
                    break
                if self._check("RPAREN"):
                    break
        return arguments

    def _skip_separators(self) -> None:
        while self._match("NEWLINE", "SEMICOLON"):
            pass

    def _check(self, kind: str) -> bool:
        return self._current().kind == kind

    def _check_any(self, *kinds: str) -> bool:
        return self._current().kind in kinds

    def _match(self, *kinds: str) -> bool:
        if self._check_any(*kinds):
            self._advance()
            return True
        return False

    def _expect(self, kind: str, message: str) -> Token:
        if self._check(kind):
            return self._advance()
        self._error(self._current(), message)

    def _advance(self) -> Token:
        token = self._current()
        if token.kind != "EOF":
            self.index += 1
        return token

    def _current(self) -> Token:
        return self.tokens[min(self.index, len(self.tokens) - 1)]

    def _previous(self) -> Token:
        return self.tokens[max(0, self.index - 1)]

    @staticmethod
    def _error(token: Token, message: str) -> None:
        raise Diagnostic(token.location, "syntax", message)
