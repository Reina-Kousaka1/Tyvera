from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ast import (
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
    InterfaceDeclaration,
    LetStatement,
    MatchStatement,
    MemberAccess,
    MemberCall,
    Name,
    Program,
    ReturnStatement,
    Statement,
    Unary,
    WhileStatement,
)
from .lexer import Lexer
from .parser import Parser
from .source import Diagnostic, Location
from .type_names import make_type, split_type


@dataclass(slots=True)
class _Module:
    path: Path
    name: str
    program: Program

    @property
    def location(self) -> Location:
        declarations = [*self.program.functions, *self.program.classes, *self.program.interfaces]
        return declarations[0].location if declarations else Location(str(self.path), 1, 1)


class ModuleLoader:
    """Resolve explicit file imports and link modules into one checked program."""

    def __init__(self, project_root: Path, entry_file: Path) -> None:
        self.root = project_root.resolve()
        self.entry = entry_file.resolve()
        self.modules: dict[Path, _Module] = {}
        self.stack: list[Path] = []
        self.declaration_index: dict[Path, dict[str, tuple[str, object]]] = {}

    def load(self) -> Program:
        self._load_module(self.entry, None)
        return self._link()

    def _load_module(self, path: Path, import_location: Location | None) -> _Module:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            location = import_location or Location(str(path), 1, 1)
            raise Diagnostic(location, "module", "module path must stay inside the project root") from error
        if resolved in self.stack:
            cycle_start = self.stack.index(resolved)
            cycle = self.stack[cycle_start:] + [resolved]
            chain = " -> ".join(self._display_path(item) for item in cycle)
            location = import_location or Location(self._display_path(resolved), 1, 1)
            raise Diagnostic(location, "module", f"circular import detected: {chain}")
        existing = self.modules.get(resolved)
        if existing is not None:
            return existing
        try:
            source = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            location = import_location or Location(self._display_path(resolved), 1, 1)
            raise Diagnostic(location, "io", "source file is not valid UTF-8") from error
        except OSError as error:
            location = import_location or Location(self._display_path(resolved), 1, 1)
            detail = error.strerror or str(error)
            raise Diagnostic(location, "module", f"cannot read module '{self._display_path(resolved)}': {detail}") from error

        name = self._module_name(resolved)
        program = Parser(Lexer(source, self._display_path(resolved)).tokenize()).parse()
        module = _Module(resolved, name, program)
        self.modules[resolved] = module
        self.stack.append(resolved)
        try:
            for declaration in program.imports:
                imported = (resolved.parent / declaration.path).resolve()
                self._load_module(imported, declaration.location)
        finally:
            self.stack.pop()
        return module

    def _module_name(self, path: Path) -> str:
        relative = path.relative_to(self.root).with_suffix("")
        parts = [part for part in relative.parts if part not in {".", ""}]
        if not parts:
            raise Diagnostic(Location(self._display_path(path), 1, 1), "module", "module path must have a name")
        return ".".join(parts)

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _declarations(module: _Module) -> dict[str, tuple[str, object]]:
        builtin_functions = {"println", "eprintln", "readText", "writeText", "pathJoin", "pathExists"}
        declarations: dict[str, tuple[str, object]] = {}
        all_declarations = [
            *(('function', declaration) for declaration in module.program.functions),
            *(('class', declaration) for declaration in module.program.classes),
            *(('interface', declaration) for declaration in module.program.interfaces),
        ]
        for kind, declaration in all_declarations:
            if declaration.name in {"Int", "Float", "Bool", "String", "Void", "Unit"}:
                raise Diagnostic(declaration.location, "type", f"type name '{declaration.name}' is reserved")
            if declaration.name == "secrets":
                raise Diagnostic(declaration.location, "type", "'secrets' is a reserved namespace")
            if kind == "function" and declaration.name in builtin_functions:
                raise Diagnostic(declaration.location, "type", f"'{declaration.name}' is a built-in function name")
            if declaration.name in declarations:
                raise Diagnostic(declaration.location, "module", f"symbol '{declaration.name}' is declared more than once in module '{module.name}'")
            declarations[declaration.name] = (kind, declaration)
        return declarations

    def _link(self) -> Program:
        declarations = {module.path: self._declarations(module) for module in self.modules.values()}
        self.declaration_index = declarations
        local_names: dict[Path, dict[str, str]] = {}
        imported_modules: dict[Path, dict[str, _Module]] = {}
        root = self.modules[self.entry]

        for module in self.modules.values():
            local: dict[str, str] = {}
            for name, (kind, declaration) in declarations[module.path].items():
                if module is root and kind == "function" and name == "main":
                    local[name] = "main"
                else:
                    local[name] = f"{module.name}.{name}"
            local_names[module.path] = local
            imports: dict[str, _Module] = {}
            for declaration in module.program.imports:
                path = (module.path.parent / declaration.path).resolve()
                imported = self.modules[path]
                if declaration.alias in {"secrets", "println", "eprintln", "readText", "writeText", "pathJoin", "pathExists"}:
                    raise Diagnostic(declaration.location, "module", f"'{declaration.alias}' is a reserved name")
                if declaration.alias in imports:
                    raise Diagnostic(declaration.location, "module", f"import alias '{declaration.alias}' is already declared")
                if declaration.alias in local:
                    raise Diagnostic(declaration.location, "module", f"import alias '{declaration.alias}' conflicts with a local top-level symbol")
                imports[declaration.alias] = imported
            imported_modules[module.path] = imports
            self._reject_alias_bindings(module, set(imports))

        functions: list[Function] = []
        classes: list[ClassDeclaration] = []
        interfaces: list[InterfaceDeclaration] = []
        for module in self.modules.values():
            local = local_names[module.path]
            imports = imported_modules[module.path]
            known = declarations[module.path]
            for function in module.program.functions:
                self._rewrite_function(function, module, local, imports, known)
                function.name = local[function.name]
                functions.append(function)
            for class_decl in module.program.classes:
                self._rewrite_class(class_decl, module, local, imports, known)
                class_decl.name = local[class_decl.name]
                classes.append(class_decl)
            for interface_decl in module.program.interfaces:
                self._rewrite_interface(interface_decl, module, local, imports, known)
                interface_decl.name = local[interface_decl.name]
                interfaces.append(interface_decl)

        return Program(functions, classes, interfaces)

    def _resolve_imported(
        self, qualifier: str, symbol: str, module: _Module, imports: dict[str, _Module],
        location: Location, expected: set[str] | None = None,
    ) -> str:
        imported = imports[qualifier]
        exported = self.declaration_index[imported.path]
        target = exported.get(symbol)
        if target is None:
            raise Diagnostic(location, "module", f"module alias '{qualifier}' has no exported symbol '{symbol}'")
        kind, declaration = target
        if not declaration.public:
            raise Diagnostic(location, "module", f"symbol '{qualifier}.{symbol}' is private; declare it with 'pub' to import it")
        if expected is not None and kind not in expected:
            kinds = " or ".join(sorted(expected))
            raise Diagnostic(location, "module", f"'{qualifier}.{symbol}' is not a {kinds}")
        return f"{imported.name}.{symbol}"

    def _resolve_type(
        self, type_name: str, module: _Module, local: dict[str, str],
        imports: dict[str, _Module], known: dict[str, tuple[str, object]], location: Location,
    ) -> str:
        base, arguments = split_type(type_name)
        if "." in base:
            qualifier, symbol = base.split(".", 1)
            if qualifier in imports:
                base = self._resolve_imported(qualifier, symbol, module, imports, location, {"class", "interface"})
            return make_type(base, [self._resolve_type(argument, module, local, imports, known, location)
                                   for argument in arguments])
        target = known.get(base)
        if target is not None and target[0] in {"class", "interface"}:
            base = local[base]
        return make_type(base, [self._resolve_type(argument, module, local, imports, known, location)
                                for argument in arguments])

    def _rewrite_function(self, function, module, local, imports, known) -> None:
        for parameter in function.parameters:
            parameter.type_name = self._resolve_type(parameter.type_name, module, local, imports, known, parameter.location)
        function.return_type = self._resolve_type(function.return_type, module, local, imports, known, function.location)
        self._rewrite_block(function.body, module, local, imports, known)

    def _rewrite_class(self, declaration, module, local, imports, known) -> None:
        declaration.interfaces = [
            self._resolve_type(name, module, local, imports, known, declaration.location)
            for name in declaration.interfaces
        ]
        for field in declaration.fields:
            field.type_name = self._resolve_type(field.type_name, module, local, imports, known, field.location)
        for method in declaration.methods:
            for parameter in method.parameters:
                parameter.type_name = self._resolve_type(parameter.type_name, module, local, imports, known, parameter.location)
            method.return_type = self._resolve_type(method.return_type, module, local, imports, known, method.location)
            self._rewrite_block(method.body, module, local, imports, known)

    def _rewrite_interface(self, declaration, module, local, imports, known) -> None:
        for method in declaration.methods:
            for parameter in method.parameters:
                parameter.type_name = self._resolve_type(parameter.type_name, module, local, imports, known, parameter.location)
            method.return_type = self._resolve_type(method.return_type, module, local, imports, known, method.location)

    def _rewrite_block(self, body: list[Statement], module, local, imports, known) -> None:
        for statement in body:
            if isinstance(statement, LetStatement):
                if statement.annotation is not None:
                    statement.annotation = self._resolve_type(statement.annotation, module, local, imports, known, statement.location)
                statement.value = self._rewrite_expression(statement.value, module, local, imports, known)
            elif isinstance(statement, Assignment):
                if isinstance(statement.target, MemberAccess):
                    statement.target = self._rewrite_expression(statement.target, module, local, imports, known)
                statement.value = self._rewrite_expression(statement.value, module, local, imports, known)
            elif isinstance(statement, Block):
                self._rewrite_block(statement.body, module, local, imports, known)
            elif isinstance(statement, IfStatement):
                statement.condition = self._rewrite_expression(statement.condition, module, local, imports, known)
                self._rewrite_block(statement.then_body, module, local, imports, known)
                self._rewrite_block(statement.else_body, module, local, imports, known)
            elif isinstance(statement, WhileStatement):
                statement.condition = self._rewrite_expression(statement.condition, module, local, imports, known)
                self._rewrite_block(statement.body, module, local, imports, known)
            elif isinstance(statement, ForStatement):
                statement.iterable = self._rewrite_expression(statement.iterable, module, local, imports, known)
                self._rewrite_block(statement.body, module, local, imports, known)
            elif isinstance(statement, MatchStatement):
                statement.value = self._rewrite_expression(statement.value, module, local, imports, known)
                for arm in statement.arms:
                    self._rewrite_block(arm.body, module, local, imports, known)
            elif isinstance(statement, ReturnStatement) and statement.value is not None:
                statement.value = self._rewrite_expression(statement.value, module, local, imports, known)
            elif isinstance(statement, ExpressionStatement):
                statement.expression = self._rewrite_expression(statement.expression, module, local, imports, known)

    def _rewrite_expression(self, expression: Expression, module, local, imports, known) -> Expression:
        if isinstance(expression, Call):
            if expression.callee != "println":
                target = known.get(expression.callee)
                if target is not None:
                    expression.callee = local[expression.callee]
            expression.type_arguments = [self._resolve_type(argument, module, local, imports, known, expression.location)
                                         for argument in expression.type_arguments]
            expression.arguments = [self._rewrite_expression(arg, module, local, imports, known) for arg in expression.arguments]
            return expression
        if isinstance(expression, MemberCall):
            if isinstance(expression.receiver, Name) and expression.receiver.identifier in imports:
                callee = self._resolve_imported(expression.receiver.identifier, expression.method, module, imports,
                                                expression.location, {"function", "class", "interface"})
                return Call(callee, [self._rewrite_expression(arg, module, local, imports, known)
                                     for arg in expression.arguments], expression.location,
                            [self._resolve_type(argument, module, local, imports, known, expression.location)
                             for argument in expression.type_arguments])
            expression.receiver = self._rewrite_expression(expression.receiver, module, local, imports, known)
            expression.type_arguments = [self._resolve_type(argument, module, local, imports, known, expression.location)
                                         for argument in expression.type_arguments]
            expression.arguments = [self._rewrite_expression(arg, module, local, imports, known) for arg in expression.arguments]
            return expression
        if isinstance(expression, MemberAccess):
            if isinstance(expression.receiver, Name) and expression.receiver.identifier in imports:
                self._resolve_imported(expression.receiver.identifier, expression.member, module, imports, expression.location)
                raise Diagnostic(expression.location, "module", "module symbols must be called or used as a type")
            expression.receiver = self._rewrite_expression(expression.receiver, module, local, imports, known)
            return expression
        if isinstance(expression, Unary):
            expression.operand = self._rewrite_expression(expression.operand, module, local, imports, known)
        elif isinstance(expression, Binary):
            expression.left = self._rewrite_expression(expression.left, module, local, imports, known)
            expression.right = self._rewrite_expression(expression.right, module, local, imports, known)
        return expression

    def _reject_alias_bindings(self, module: _Module, aliases: set[str]) -> None:
        def block(body: list[Statement]) -> None:
            for statement in body:
                if isinstance(statement, LetStatement) and statement.name in aliases:
                    raise Diagnostic(statement.location, "module", f"'{statement.name}' is reserved as an import alias in this module")
                if isinstance(statement, ForStatement) and statement.name in aliases:
                    raise Diagnostic(statement.location, "module", f"'{statement.name}' is reserved as an import alias in this module")
                if isinstance(statement, MatchStatement):
                    for arm in statement.arms:
                        pattern = arm.pattern
                        binding = getattr(pattern, "name", None) or getattr(pattern, "binding", None)
                        if binding in aliases:
                            raise Diagnostic(pattern.location, "module", f"'{binding}' is reserved as an import alias in this module")
                if isinstance(statement, Block):
                    block(statement.body)
                elif isinstance(statement, IfStatement):
                    block(statement.then_body)
                    block(statement.else_body)
                elif isinstance(statement, WhileStatement):
                    block(statement.body)
                elif isinstance(statement, ForStatement):
                    block(statement.body)
                elif isinstance(statement, MatchStatement):
                    for arm in statement.arms:
                        block(arm.body)

        for function in module.program.functions:
            for parameter in function.parameters:
                if parameter.name in aliases:
                    raise Diagnostic(parameter.location, "module", f"'{parameter.name}' is reserved as an import alias in this module")
            block(function.body)
        for declaration in module.program.classes:
            for method in declaration.methods:
                for parameter in method.parameters:
                    if parameter.name in aliases:
                        raise Diagnostic(parameter.location, "module", f"'{parameter.name}' is reserved as an import alias in this module")
                block(method.body)
