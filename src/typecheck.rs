use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use crate::ast::{
    BinaryOp, Block, ElseBranch, Expr, FunctionDecl, Item, Module, Statement, Type, UnaryOp,
};
use crate::diagnostic::{Diagnostic, Span};
use crate::resolver::ModuleGraph;

type ImportNamespaces = HashMap<String, HashMap<String, ImportedFunction>>;

#[derive(Clone)]
struct ImportedFunction {
    module_path: PathBuf,
    name: String,
    signature: Option<ValueType>,
}

/// Target of a call resolved through an import alias.
#[derive(Clone, Debug)]
pub struct ResolvedFunction {
    pub module_path: PathBuf,
    pub name: String,
}

/// A checked module and the inferred type of each source expression.
#[derive(Clone, Debug)]
pub struct CheckedModule {
    pub module: Module,
    pub expression_types: HashMap<Span, ValueType>,
    pub function_types: HashMap<String, ValueType>,
    pub resolved_import_calls: HashMap<Span, ResolvedFunction>,
}

/// A checked module in a dependency-first source program.
#[derive(Clone, Debug)]
pub struct CheckedSourceModule {
    pub path: PathBuf,
    pub checked: CheckedModule,
}

/// The result of checking all modules reachable from one entry file.
#[derive(Clone, Debug)]
pub struct CheckedProgram {
    pub entry: PathBuf,
    pub modules: Vec<CheckedSourceModule>,
}

/// Types after aliases have been expanded and user declarations resolved.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum ValueType {
    Unit,
    Int,
    Float,
    Bool,
    String,
    Named(String),
    Array(Box<ValueType>),
    Channel(Box<ValueType>),
    Task(Box<ValueType>),
    Optional(Box<ValueType>),
    Function(Vec<ValueType>, Box<ValueType>),
}

impl std::fmt::Display for ValueType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unit => write!(f, "Unit"),
            Self::Int => write!(f, "Int"),
            Self::Float => write!(f, "Float"),
            Self::Bool => write!(f, "Bool"),
            Self::String => write!(f, "String"),
            Self::Named(name) => write!(f, "{name}"),
            Self::Array(element) => write!(f, "[{element}]"),
            Self::Channel(element) => write!(f, "chan<{element}>"),
            Self::Task(result) => write!(f, "Task<{result}>"),
            Self::Optional(inner) => write!(f, "?{inner}"),
            Self::Function(params, result) => {
                let params = params.iter().map(ToString::to_string).collect::<Vec<_>>();
                write!(f, "fn({}) -> {result}", params.join(", "))
            }
        }
    }
}

/// Check names and types in one parsed module.
///
/// This pass validates local module declarations; import graph resolution and
/// generic declaration parameters are deliberately handled by later passes.
pub fn check(module: &Module) -> Result<CheckedModule, Vec<Diagnostic>> {
    Checker::new(module.clone(), HashMap::new()).run()
}

/// Type-check a resolved source graph, exposing only imported public functions.
pub fn check_graph(graph: &ModuleGraph) -> Result<CheckedProgram, Vec<Diagnostic>> {
    let mut modules_by_path = HashMap::<PathBuf, CheckedModule>::new();
    let mut checked_modules = Vec::with_capacity(graph.modules().len());
    let mut errors = Vec::new();

    for source in graph.modules() {
        let mut imports = ImportNamespaces::new();
        for import in &source.imports {
            let Some(dependency) = modules_by_path.get(&import.path) else {
                errors.push(Diagnostic::new(
                    format!(
                        "{}: imported module was not successfully checked",
                        import.path.display()
                    ),
                    Span::default(),
                ));
                continue;
            };
            let alias = import
                .alias
                .clone()
                .or_else(|| {
                    dependency
                        .module
                        .name
                        .as_ref()
                        .map(|name| name.text.clone())
                })
                .or_else(|| {
                    import
                        .path
                        .file_stem()
                        .map(|stem| stem.to_string_lossy().into_owned())
                })
                .unwrap_or_default();
            let mut exports = HashMap::new();
            for item in &dependency.module.items {
                let Item::Function(function) = item else {
                    continue;
                };
                if function.is_public
                    && let Some(signature) = dependency.function_types.get(&function.name.text)
                {
                    let importable = match signature {
                        ValueType::Function(params, result) => {
                            params.iter().all(is_importable_type) && is_importable_type(result)
                        }
                        _ => false,
                    };
                    exports.insert(
                        function.name.text.clone(),
                        ImportedFunction {
                            module_path: import.path.clone(),
                            name: function.name.text.clone(),
                            signature: importable.then(|| signature.clone()),
                        },
                    );
                }
            }
            if imports.insert(alias.clone(), exports).is_some() {
                errors.push(Diagnostic::new(
                    format!(
                        "{}: import alias '{alias}' is declared more than once",
                        source.path.display()
                    ),
                    Span::default(),
                ));
            }
        }

        match Checker::new(source.module.clone(), imports).run() {
            Ok(checked) => {
                modules_by_path.insert(source.path.clone(), checked.clone());
                checked_modules.push(CheckedSourceModule {
                    path: source.path.clone(),
                    checked,
                });
            }
            Err(diagnostics) => errors.extend(diagnostics.into_iter().map(|mut diagnostic| {
                let position = diagnostic.position(&source.source);
                diagnostic.message = format!(
                    "{}:{}:{}: {}",
                    source.path.display(),
                    position.line,
                    position.column,
                    diagnostic.message
                );
                diagnostic
            })),
        }
    }

    if errors.is_empty() {
        Ok(CheckedProgram {
            entry: graph.entry().to_path_buf(),
            modules: checked_modules,
        })
    } else {
        Err(errors)
    }
}

fn is_importable_type(ty: &ValueType) -> bool {
    match ty {
        ValueType::Unit
        | ValueType::Int
        | ValueType::Float
        | ValueType::Bool
        | ValueType::String => true,
        ValueType::Array(inner)
        | ValueType::Channel(inner)
        | ValueType::Task(inner)
        | ValueType::Optional(inner) => is_importable_type(inner),
        ValueType::Function(params, result) => {
            params.iter().all(is_importable_type) && is_importable_type(result)
        }
        ValueType::Named(_) => false,
    }
}

#[derive(Clone)]
enum UserType {
    Struct(Vec<crate::ast::FieldDecl>),
    Enum(Vec<crate::ast::EnumVariant>),
    Alias(Type),
}

#[derive(Clone)]
struct Local {
    ty: ValueType,
    mutable: bool,
}

struct Checker {
    module: Module,
    types: HashMap<String, UserType>,
    functions: HashMap<String, ValueType>,
    constants: HashMap<String, ValueType>,
    imports: ImportNamespaces,
    resolved_import_calls: HashMap<Span, ResolvedFunction>,
    scopes: Vec<HashMap<String, Local>>,
    expression_types: HashMap<Span, ValueType>,
    errors: Vec<Diagnostic>,
}

impl Checker {
    fn new(module: Module, imports: ImportNamespaces) -> Self {
        let functions = HashMap::from([(
            "print".to_owned(),
            ValueType::Function(vec![ValueType::String], Box::new(ValueType::Unit)),
        )]);
        Self {
            module,
            types: HashMap::new(),
            functions,
            constants: HashMap::new(),
            imports,
            resolved_import_calls: HashMap::new(),
            scopes: Vec::new(),
            expression_types: HashMap::new(),
            errors: Vec::new(),
        }
    }

    fn run(mut self) -> Result<CheckedModule, Vec<Diagnostic>> {
        self.collect_types();
        self.validate_aliases();
        self.collect_functions();
        self.check_enum_constructors();
        self.check_constants();

        let functions = self
            .module
            .items
            .iter()
            .filter_map(|item| match item {
                Item::Function(function) => Some(function.clone()),
                _ => None,
            })
            .collect::<Vec<_>>();
        for function in functions {
            self.check_function(&function);
        }

        if self.errors.is_empty() {
            Ok(CheckedModule {
                module: self.module,
                expression_types: self.expression_types,
                function_types: self.functions,
                resolved_import_calls: self.resolved_import_calls,
            })
        } else {
            Err(self.errors)
        }
    }

    fn collect_types(&mut self) {
        for item in &self.module.items {
            let (name, definition, span) = match item {
                Item::Struct(value) => (
                    value.name.text.clone(),
                    UserType::Struct(value.fields.clone()),
                    value.name.span,
                ),
                Item::Enum(value) => (
                    value.name.text.clone(),
                    UserType::Enum(value.variants.clone()),
                    value.name.span,
                ),
                Item::TypeAlias(value) => (
                    value.name.text.clone(),
                    UserType::Alias(value.target.clone()),
                    value.name.span,
                ),
                _ => continue,
            };
            if is_builtin(&name) || self.types.contains_key(&name) {
                self.errors.push(Diagnostic::new(
                    format!("type '{name}' is already declared"),
                    span,
                ));
            } else {
                self.types.insert(name, definition);
            }
        }

        let type_names = self.types.keys().cloned().collect::<Vec<_>>();
        for name in type_names {
            let definition = self.types.get(&name).cloned();
            match definition {
                Some(UserType::Struct(fields)) => {
                    let mut seen = HashSet::new();
                    for field in fields {
                        if !seen.insert(field.name.text.clone()) {
                            self.errors.push(Diagnostic::new(
                                format!("field '{}' is declared more than once", field.name.text),
                                field.name.span,
                            ));
                        }
                        self.resolve_type(&field.ty, &mut Vec::new());
                    }
                }
                Some(UserType::Enum(variants)) => {
                    let mut seen = HashSet::new();
                    for variant in variants {
                        if !seen.insert(variant.name.text.clone()) {
                            self.errors.push(Diagnostic::new(
                                format!(
                                    "variant '{}' is declared more than once",
                                    variant.name.text
                                ),
                                variant.name.span,
                            ));
                        }
                        for field in variant.fields {
                            self.resolve_type(&field, &mut Vec::new());
                        }
                    }
                }
                Some(UserType::Alias(_)) | None => {}
            }
        }
    }

    fn validate_aliases(&mut self) {
        let aliases = self
            .types
            .iter()
            .filter_map(|(name, definition)| match definition {
                UserType::Alias(_) => Some(name.clone()),
                _ => None,
            })
            .collect::<Vec<_>>();
        for name in aliases {
            if let Some(UserType::Alias(target)) = self.types.get(&name).cloned() {
                let mut stack = vec![name];
                self.resolve_type(&target, &mut stack);
            }
        }
    }

    fn collect_functions(&mut self) {
        let declarations = self
            .module
            .items
            .iter()
            .filter_map(|item| match item {
                Item::Function(function) => Some(function.clone()),
                _ => None,
            })
            .collect::<Vec<_>>();
        for function in declarations {
            let params = function
                .params
                .iter()
                .filter_map(|param| self.resolve_type(&param.ty, &mut Vec::new()))
                .collect::<Vec<_>>();
            let result = self.resolve_type(&function.result, &mut Vec::new());
            if params.len() != function.params.len() || result.is_none() {
                continue;
            }
            let ty = ValueType::Function(params, Box::new(result.unwrap()));
            if self.functions.contains_key(&function.name.text)
                || self.constants.contains_key(&function.name.text)
            {
                self.errors.push(Diagnostic::new(
                    format!("value '{}' is already declared", function.name.text),
                    function.name.span,
                ));
            } else {
                self.functions.insert(function.name.text.clone(), ty);
            }
        }
    }

    fn check_constants(&mut self) {
        let declarations = self
            .module
            .items
            .iter()
            .filter_map(|item| match item {
                Item::Constant(constant) => Some(constant.clone()),
                _ => None,
            })
            .collect::<Vec<_>>();
        for constant in declarations {
            if self.functions.contains_key(&constant.name.text)
                || self.constants.contains_key(&constant.name.text)
            {
                self.errors.push(Diagnostic::new(
                    format!("value '{}' is already declared", constant.name.text),
                    constant.name.span,
                ));
                continue;
            }
            let expected = constant
                .ty
                .as_ref()
                .and_then(|ty| self.resolve_type(ty, &mut Vec::new()));
            let actual = self.infer_expr(&constant.value, expected.as_ref());
            let ty = match (expected, actual) {
                (Some(expected), Some(actual)) if self.compatible(&actual, &expected) => expected,
                (Some(expected), Some(actual)) => {
                    self.type_mismatch(constant.value.span(), &expected, &actual);
                    continue;
                }
                (None, Some(actual)) => actual,
                _ => continue,
            };
            self.constants.insert(constant.name.text, ty);
        }
    }

    fn check_enum_constructors(&mut self) {
        let declarations = self
            .types
            .iter()
            .filter_map(|(name, definition)| match definition {
                UserType::Enum(variants) => Some((name.clone(), variants.clone())),
                _ => None,
            })
            .collect::<Vec<_>>();
        for (enum_name, variants) in declarations {
            for variant in variants {
                let fields = variant
                    .fields
                    .iter()
                    .filter_map(|ty| self.resolve_type(ty, &mut Vec::new()))
                    .collect::<Vec<_>>();
                if fields.len() != variant.fields.len() {
                    continue;
                }
                let value_type = if fields.is_empty() {
                    ValueType::Named(enum_name.clone())
                } else {
                    ValueType::Function(fields, Box::new(ValueType::Named(enum_name.clone())))
                };
                let name = variant.name.text.clone();
                if self.functions.contains_key(&name) || self.constants.contains_key(&name) {
                    self.errors.push(Diagnostic::new(
                        format!("enum constructor '{name}' conflicts with another value"),
                        variant.name.span,
                    ));
                } else {
                    self.constants.insert(name, value_type);
                }
            }
        }
    }

    fn resolve_type(&mut self, ty: &Type, aliases: &mut Vec<String>) -> Option<ValueType> {
        match ty {
            Type::Unit => Some(ValueType::Unit),
            Type::Named(name) => match name.text.as_str() {
                "Int" => Some(ValueType::Int),
                "Float" => Some(ValueType::Float),
                "Bool" => Some(ValueType::Bool),
                "String" => Some(ValueType::String),
                "Unit" => Some(ValueType::Unit),
                _ => match self.types.get(&name.text).cloned() {
                    Some(UserType::Alias(target)) => {
                        if aliases.contains(&name.text) {
                            let mut cycle = aliases.clone();
                            cycle.push(name.text.clone());
                            self.errors.push(Diagnostic::new(
                                format!("cyclic type alias: {}", cycle.join(" -> ")),
                                name.span,
                            ));
                            None
                        } else {
                            aliases.push(name.text.clone());
                            let resolved = self.resolve_type(&target, aliases);
                            aliases.pop();
                            resolved
                        }
                    }
                    Some(UserType::Struct(_)) | Some(UserType::Enum(_)) => {
                        Some(ValueType::Named(name.text.clone()))
                    }
                    None => {
                        self.errors.push(Diagnostic::new(
                            format!("unknown type '{}'", name.text),
                            name.span,
                        ));
                        None
                    }
                },
            },
            Type::Generic { base, args } => {
                self.errors.push(Diagnostic::new(
                    format!(
                        "generic type '{}' is not supported until generic declarations are implemented",
                        base.text
                    ),
                    base.span,
                ));
                for arg in args {
                    self.resolve_type(arg, aliases);
                }
                None
            }
            Type::Array(element) => self
                .resolve_type(element, aliases)
                .map(|element| ValueType::Array(Box::new(element))),
            Type::Channel(element) => self
                .resolve_type(element, aliases)
                .map(|element| ValueType::Channel(Box::new(element))),
            Type::Task(result) => self
                .resolve_type(result, aliases)
                .map(|result| ValueType::Task(Box::new(result))),
            Type::Optional(inner) => self
                .resolve_type(inner, aliases)
                .map(|inner| ValueType::Optional(Box::new(inner))),
            Type::Function { params, result } => {
                let param_types = params
                    .iter()
                    .filter_map(|param| self.resolve_type(param, aliases))
                    .collect::<Vec<_>>();
                let result = self.resolve_type(result, aliases);
                if param_types.len() != params.len() {
                    return None;
                }
                result.map(|result| ValueType::Function(param_types, Box::new(result)))
            }
        }
    }

    fn check_function(&mut self, function: &FunctionDecl) {
        let Some(signature) = self.functions.get(&function.name.text).cloned() else {
            return;
        };
        let ValueType::Function(params, result) = signature else {
            unreachable!()
        };
        let mut initial_scope = HashMap::new();
        for (parameter, ty) in function.params.iter().zip(params) {
            if initial_scope.contains_key(&parameter.name.text) {
                self.errors.push(Diagnostic::new(
                    format!("parameter '{}' is repeated", parameter.name.text),
                    parameter.name.span,
                ));
            } else {
                initial_scope.insert(parameter.name.text.clone(), Local { ty, mutable: false });
            }
        }
        self.scopes.push(initial_scope);
        self.check_block(&function.body, &result);
        self.scopes.pop();
        if *result != ValueType::Unit && !block_returns(&function.body) {
            self.errors.push(Diagnostic::new(
                format!(
                    "function '{}' can finish without returning {result}",
                    function.name.text
                ),
                function.name.span,
            ));
        }
    }

    fn check_block(&mut self, block: &Block, return_type: &ValueType) {
        self.scopes.push(HashMap::new());
        for statement in &block.statements {
            self.check_statement(statement, return_type);
        }
        self.scopes.pop();
    }

    fn check_statement(&mut self, statement: &Statement, return_type: &ValueType) {
        match statement {
            Statement::Let {
                mutable,
                name,
                annotation,
                value,
                ..
            } => {
                let expected = annotation
                    .as_ref()
                    .and_then(|ty| self.resolve_type(ty, &mut Vec::new()));
                let actual = self.infer_expr(value, expected.as_ref());
                let ty = match (expected, actual) {
                    (Some(expected), Some(actual)) if self.compatible(&actual, &expected) => {
                        expected
                    }
                    (Some(expected), Some(actual)) => {
                        self.type_mismatch(value.span(), &expected, &actual);
                        return;
                    }
                    (None, Some(actual)) => actual,
                    _ => return,
                };
                if let Some(scope) = self.scopes.last_mut() {
                    if scope.contains_key(&name.text) {
                        self.errors.push(Diagnostic::new(
                            format!("'{}' is already declared in this scope", name.text),
                            name.span,
                        ));
                    } else {
                        scope.insert(
                            name.text.clone(),
                            Local {
                                ty,
                                mutable: *mutable,
                            },
                        );
                    }
                }
            }
            Statement::Return { value, span } => match (value, return_type) {
                (None, ValueType::Unit) => {}
                (None, expected) => self.errors.push(Diagnostic::new(
                    format!("return value of type {expected} is required"),
                    *span,
                )),
                (Some(value), expected) => {
                    if let Some(actual) = self.infer_expr(value, Some(expected))
                        && !self.compatible(&actual, expected)
                    {
                        self.type_mismatch(value.span(), expected, &actual);
                    }
                }
            },
            Statement::If {
                condition,
                then_block,
                else_branch,
                ..
            } => {
                if let Some(actual) = self.infer_expr(condition, Some(&ValueType::Bool))
                    && actual != ValueType::Bool
                {
                    self.type_mismatch(condition.span(), &ValueType::Bool, &actual);
                }
                self.check_block(then_block, return_type);
                match else_branch {
                    Some(ElseBranch::Block(block)) => self.check_block(block, return_type),
                    Some(ElseBranch::If(statement)) => self.check_statement(statement, return_type),
                    None => {}
                }
            }
            Statement::While {
                condition, body, ..
            } => {
                if let Some(actual) = self.infer_expr(condition, Some(&ValueType::Bool))
                    && actual != ValueType::Bool
                {
                    self.type_mismatch(condition.span(), &ValueType::Bool, &actual);
                }
                self.check_block(body, return_type);
            }
            Statement::Expression { expression, .. } => {
                self.infer_expr(expression, None);
            }
        }
    }

    fn infer_expr(&mut self, expression: &Expr, expected: Option<&ValueType>) -> Option<ValueType> {
        let ty = match expression {
            Expr::Integer(_, _) => Some(ValueType::Int),
            Expr::Float(_, _) => Some(ValueType::Float),
            Expr::String(_, _) => Some(ValueType::String),
            Expr::Boolean(_, _) => Some(ValueType::Bool),
            Expr::Nil(span) => match expected {
                Some(ValueType::Optional(_)) => expected.cloned(),
                _ => {
                    self.errors.push(Diagnostic::new(
                        "nil needs an optional type annotation or context",
                        *span,
                    ));
                    None
                }
            },
            Expr::Name(name) => self.lookup_value(&name.text).or_else(|| {
                self.errors.push(Diagnostic::new(
                    format!("unknown value '{}'", name.text),
                    name.span,
                ));
                None
            }),
            Expr::Array(elements, span) => {
                let expected_element = match expected {
                    Some(ValueType::Array(element)) => Some(element.as_ref()),
                    _ => None,
                };
                let mut element_type = expected_element.cloned();
                for element in elements {
                    let actual = self.infer_expr(element, element_type.as_ref());
                    if let Some(actual) = actual {
                        match &element_type {
                            Some(expected) if !self.compatible(&actual, expected) => {
                                self.type_mismatch(element.span(), expected, &actual);
                            }
                            None => element_type = Some(actual),
                            _ => {}
                        }
                    }
                }
                match element_type {
                    Some(element) => Some(ValueType::Array(Box::new(element))),
                    None => {
                        self.errors.push(Diagnostic::new(
                            "cannot infer the element type of an empty array",
                            *span,
                        ));
                        None
                    }
                }
            }
            Expr::Unary { op, operand, span } => {
                let operand_type = self.infer_expr(operand, None)?;
                match op {
                    UnaryOp::Negate if is_numeric(&operand_type) => Some(operand_type),
                    UnaryOp::Not if operand_type == ValueType::Bool => Some(ValueType::Bool),
                    UnaryOp::Spawn => Some(ValueType::Task(Box::new(operand_type))),
                    UnaryOp::Await => match operand_type {
                        ValueType::Task(result) => Some(*result),
                        actual => {
                            self.type_mismatch(
                                *span,
                                &ValueType::Task(Box::new(ValueType::Unit)),
                                &actual,
                            );
                            None
                        }
                    },
                    _ => {
                        let expected = if *op == UnaryOp::Not {
                            ValueType::Bool
                        } else {
                            ValueType::Int
                        };
                        self.type_mismatch(*span, &expected, &operand_type);
                        None
                    }
                }
            }
            Expr::Binary {
                left,
                op,
                right,
                span,
            } => self.infer_binary(left, *op, right, *span),
            Expr::Call { callee, args, span } => {
                let imported = if let Expr::Field { object, field, .. } = callee.as_ref()
                    && let Expr::Name(alias) = object.as_ref()
                    && let Some(exports) = self.imports.get(&alias.text)
                {
                    Some((
                        alias.text.clone(),
                        field.text.clone(),
                        field.span,
                        exports.get(&field.text).cloned(),
                    ))
                } else {
                    None
                };
                let callee_type = match imported {
                    Some((alias, field_name, field_span, Some(imported_function))) => {
                        let Some(signature) = imported_function.signature else {
                            self.errors.push(Diagnostic::new(
                                format!(
                                    "imported function '{}.{}' uses module-local types, which are not supported across imports yet",
                                    alias, field_name
                                ),
                                field_span,
                            ));
                            return None;
                        };
                        self.resolved_import_calls.insert(
                            *span,
                            ResolvedFunction {
                                module_path: imported_function.module_path,
                                name: imported_function.name,
                            },
                        );
                        signature
                    }
                    Some((alias, field_name, field_span, None)) => {
                        self.errors.push(Diagnostic::new(
                            format!("module '{alias}' has no exported function '{field_name}'"),
                            field_span,
                        ));
                        return None;
                    }
                    None => self.infer_expr(callee, None)?,
                };
                let ValueType::Function(params, result) = callee_type else {
                    self.errors.push(Diagnostic::new(
                        format!("value of type {callee_type} cannot be called"),
                        callee.span(),
                    ));
                    return None;
                };
                if params.len() != args.len() {
                    self.errors.push(Diagnostic::new(
                        format!(
                            "call expects {} argument(s), got {}",
                            params.len(),
                            args.len()
                        ),
                        *span,
                    ));
                }
                for (argument, expected) in args.iter().zip(&params) {
                    if let Some(actual) = self.infer_expr(argument, Some(expected))
                        && !self.compatible(&actual, expected)
                    {
                        self.type_mismatch(argument.span(), expected, &actual);
                    }
                }
                Some(*result)
            }
            Expr::Field { object, field, .. } => {
                let object_type = self.infer_expr(object, None)?;
                match object_type {
                    ValueType::Named(name) => match self.types.get(&name).cloned() {
                        Some(UserType::Struct(fields)) => fields
                            .iter()
                            .find(|candidate| candidate.name.text == field.text)
                            .and_then(|candidate| self.resolve_type(&candidate.ty, &mut Vec::new()))
                            .or_else(|| {
                                self.errors.push(Diagnostic::new(
                                    format!("type '{name}' has no field '{}'", field.text),
                                    field.span,
                                ));
                                None
                            }),
                        _ => {
                            self.errors.push(Diagnostic::new(
                                format!("type '{name}' has no fields"),
                                field.span,
                            ));
                            None
                        }
                    },
                    other => {
                        self.errors.push(Diagnostic::new(
                            format!("value of type {other} has no fields"),
                            field.span,
                        ));
                        None
                    }
                }
            }
            Expr::Index {
                object,
                index,
                span,
            } => {
                let object_type = self.infer_expr(object, None)?;
                let index_type = self.infer_expr(index, Some(&ValueType::Int));
                if let Some(index_type) = index_type
                    && index_type != ValueType::Int
                {
                    self.type_mismatch(index.span(), &ValueType::Int, &index_type);
                }
                match object_type {
                    ValueType::Array(element) => Some(*element),
                    other => {
                        self.errors.push(Diagnostic::new(
                            format!("value of type {other} cannot be indexed"),
                            *span,
                        ));
                        None
                    }
                }
            }
        };
        if let Some(ty) = &ty {
            self.expression_types.insert(expression.span(), ty.clone());
        }
        ty
    }

    fn infer_binary(
        &mut self,
        left: &Expr,
        op: BinaryOp,
        right: &Expr,
        span: Span,
    ) -> Option<ValueType> {
        if op == BinaryOp::Assign {
            self.check_assignable(left)?;
            let target = self.infer_expr(left, None)?;
            let value = self.infer_expr(right, Some(&target))?;
            if !self.compatible(&target, &value) {
                self.type_mismatch(right.span(), &target, &value);
                return None;
            }
            return Some(target);
        }
        let left_type = self.infer_expr(left, None)?;
        let right_type = self.infer_expr(right, Some(&left_type))?;
        match op {
            BinaryOp::Or | BinaryOp::And => {
                if left_type != ValueType::Bool {
                    self.type_mismatch(left.span(), &ValueType::Bool, &left_type);
                }
                if right_type != ValueType::Bool {
                    self.type_mismatch(right.span(), &ValueType::Bool, &right_type);
                }
                Some(ValueType::Bool)
            }
            BinaryOp::Equal | BinaryOp::NotEqual => {
                if !self.compatible(&left_type, &right_type) {
                    self.type_mismatch(right.span(), &left_type, &right_type);
                }
                Some(ValueType::Bool)
            }
            BinaryOp::Less | BinaryOp::LessEqual | BinaryOp::Greater | BinaryOp::GreaterEqual => {
                if !is_numeric(&left_type) || !self.compatible(&left_type, &right_type) {
                    self.type_mismatch(span, &left_type, &right_type);
                }
                Some(ValueType::Bool)
            }
            BinaryOp::Add if left_type == ValueType::String && right_type == ValueType::String => {
                Some(ValueType::String)
            }
            BinaryOp::Add
            | BinaryOp::Subtract
            | BinaryOp::Multiply
            | BinaryOp::Divide
            | BinaryOp::Remainder => {
                if !is_numeric(&left_type) || !self.compatible(&left_type, &right_type) {
                    self.type_mismatch(span, &left_type, &right_type);
                    None
                } else {
                    Some(left_type)
                }
            }
            BinaryOp::Assign => unreachable!(),
        }
    }

    fn check_assignable(&mut self, expression: &Expr) -> Option<()> {
        match expression {
            Expr::Name(name) => {
                for scope in self.scopes.iter().rev() {
                    if let Some(local) = scope.get(&name.text) {
                        if local.mutable {
                            return Some(());
                        }
                        self.errors.push(Diagnostic::new(
                            format!("cannot assign to immutable binding '{}'", name.text),
                            name.span,
                        ));
                        return None;
                    }
                }
                self.errors.push(Diagnostic::new(
                    format!("'{}' is not a mutable local variable", name.text),
                    name.span,
                ));
                None
            }
            Expr::Field { object, .. } => self.infer_expr(object, None).map(|_| ()),
            Expr::Index { object, index, .. } => {
                self.infer_expr(object, None)?;
                self.infer_expr(index, Some(&ValueType::Int)).map(|_| ())
            }
            _ => {
                self.errors.push(Diagnostic::new(
                    "left side of assignment is not an assignable place",
                    expression.span(),
                ));
                None
            }
        }
    }

    fn lookup_value(&self, name: &str) -> Option<ValueType> {
        self.scopes
            .iter()
            .rev()
            .find_map(|scope| scope.get(name).map(|local| local.ty.clone()))
            .or_else(|| self.constants.get(name).cloned())
            .or_else(|| self.functions.get(name).cloned())
    }

    fn compatible(&self, actual: &ValueType, expected: &ValueType) -> bool {
        actual == expected
    }

    fn type_mismatch(&mut self, span: Span, expected: &ValueType, actual: &ValueType) {
        self.errors.push(Diagnostic::new(
            format!("type mismatch: expected {expected}, got {actual}"),
            span,
        ));
    }
}

fn is_builtin(name: &str) -> bool {
    matches!(name, "Int" | "Float" | "Bool" | "String" | "Unit")
}

fn is_numeric(ty: &ValueType) -> bool {
    matches!(ty, ValueType::Int | ValueType::Float)
}

fn block_returns(block: &Block) -> bool {
    block.statements.iter().any(statement_returns)
}

fn statement_returns(statement: &Statement) -> bool {
    match statement {
        Statement::Return { .. } => true,
        Statement::If {
            then_block,
            else_branch,
            ..
        } => {
            let else_returns = match else_branch {
                Some(ElseBranch::Block(block)) => block_returns(block),
                Some(ElseBranch::If(statement)) => statement_returns(statement),
                None => false,
            };
            block_returns(then_block) && else_returns
        }
        Statement::Let { .. } | Statement::While { .. } | Statement::Expression { .. } => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse;

    #[test]
    fn checks_aliases_struct_fields_enums_calls_and_array_types() {
        let source = r#"
            module typed;
            type Count = Int;
            struct Pair { left: Int, right: Int, }
            enum Maybe { Some(Int), None, }
            const zero: Count = 0;

            fn double(value: Count) -> Count {
                return value * 2;
            }
            fn read_left(pair: Pair) -> Int {
                return pair.left;
            }
            fn maybe() -> Maybe {
                return Some(double(zero));
            }
            fn run() -> Int {
                var total: Count = double(21);
                let values: [Int] = [total, zero];
                total = values[0];
                if total > 0 { return total; } else { return 0; }
            }
        "#;
        let module = parse(source).unwrap();
        let checked = check(&module).unwrap();
        assert!(
            checked
                .expression_types
                .values()
                .any(|ty| *ty == ValueType::Int)
        );
    }

    #[test]
    fn reports_immutable_assignment_and_type_mismatches() {
        let module = parse(
            r#"
                module bad;
                fn run() -> Int {
                    let fixed: Int = 1;
                    fixed = 2;
                    let flag: Bool = 3;
                    return "wrong";
                }
            "#,
        )
        .unwrap();
        let errors = check(&module).unwrap_err();
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("immutable"))
        );
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("expected Bool, got Int"))
        );
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("expected Int, got String"))
        );
    }

    #[test]
    fn detects_cyclic_type_aliases() {
        let module = parse("module cycle; type First = Second; type Second = First;").unwrap();
        let errors = check(&module).unwrap_err();
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("cyclic type alias"))
        );
    }

    #[test]
    fn checks_call_arity_and_all_return_paths() {
        let module = parse(
            r#"
                module paths;
                fn identity(value: Int) -> Int { return value; }
                fn incomplete(flag: Bool) -> Int {
                    if flag { return identity(); }
                }
            "#,
        )
        .unwrap();
        let errors = check(&module).unwrap_err();
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("expects 1 argument"))
        );
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("can finish without returning Int"))
        );
    }
}
