use crate::ast::{BinaryOp, Block, ElseBranch, Item, UnaryOp};
use crate::diagnostic::{Diagnostic, Span};
use crate::typecheck::{CheckedModule, ValueType};

/// A deliberately small typed IR used by the first native execution slice.
#[derive(Clone, Debug, PartialEq)]
pub struct Program {
    pub functions: Vec<Function>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Function {
    pub name: String,
    pub params: Vec<(String, IrType)>,
    pub result: IrType,
    pub body: Vec<Statement>,
    pub span: Span,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IrType {
    Int,
    Float,
    Bool,
    Unit,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Statement {
    Let {
        name: String,
        mutable: bool,
        ty: IrType,
        value: Expr,
        span: Span,
    },
    Assign {
        name: String,
        value: Expr,
        span: Span,
    },
    Return(Option<Expr>, Span),
    Expression(Expr, Span),
    If {
        condition: Expr,
        then_body: Vec<Statement>,
        else_body: Vec<Statement>,
        span: Span,
    },
    While {
        condition: Expr,
        body: Vec<Statement>,
        span: Span,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum Expr {
    Integer(i64, Span),
    Float(f64, Span),
    Boolean(bool, Span),
    Name(String, IrType, Span),
    Unary {
        op: UnaryOp,
        operand: Box<Expr>,
        ty: IrType,
        span: Span,
    },
    Binary {
        left: Box<Expr>,
        op: BinaryOp,
        right: Box<Expr>,
        ty: IrType,
        operand_ty: IrType,
        span: Span,
    },
    Call {
        name: String,
        args: Vec<Expr>,
        result: IrType,
        span: Span,
    },
}

/// Lower a successfully checked module into the currently supported scalar IR.
pub fn lower(checked: &CheckedModule) -> Result<Program, Vec<Diagnostic>> {
    Lowerer {
        checked,
        errors: Vec::new(),
    }
    .run()
}

struct Lowerer<'a> {
    checked: &'a CheckedModule,
    errors: Vec<Diagnostic>,
}

impl Lowerer<'_> {
    fn run(mut self) -> Result<Program, Vec<Diagnostic>> {
        let mut functions = Vec::new();
        for item in &self.checked.module.items {
            let Item::Function(function) = item else {
                continue;
            };
            let Some(ValueType::Function(param_types, result_type)) =
                self.checked.function_types.get(&function.name.text)
            else {
                continue;
            };
            let mut params = Vec::new();
            for (param, ty) in function.params.iter().zip(param_types) {
                if let Some(ty) = self.lower_type(ty, param.span) {
                    params.push((param.name.text.clone(), ty));
                }
            }
            let Some(result) = self.lower_type(result_type, function.name.span) else {
                continue;
            };
            let body = self.lower_block(&function.body);
            functions.push(Function {
                name: function.name.text.clone(),
                params,
                result,
                body,
                span: function.name.span,
            });
        }
        if self.errors.is_empty() {
            Ok(Program { functions })
        } else {
            Err(self.errors)
        }
    }

    fn lower_type(&mut self, ty: &ValueType, span: Span) -> Option<IrType> {
        match ty {
            ValueType::Int => Some(IrType::Int),
            ValueType::Float => Some(IrType::Float),
            ValueType::Bool => Some(IrType::Bool),
            ValueType::Unit => Some(IrType::Unit),
            other => {
                self.errors.push(Diagnostic::new(
                    format!("native scalar backend does not support type {other} yet"),
                    span,
                ));
                None
            }
        }
    }

    fn lower_block(&mut self, block: &Block) -> Vec<Statement> {
        block
            .statements
            .iter()
            .filter_map(|statement| self.lower_statement(statement))
            .collect()
    }

    fn lower_statement(&mut self, statement: &crate::ast::Statement) -> Option<Statement> {
        match statement {
            crate::ast::Statement::Let {
                mutable,
                name,
                value,
                ..
            } => {
                let ty = self
                    .checked
                    .expression_types
                    .get(&value.span())
                    .cloned()
                    .and_then(|ty| self.lower_type(&ty, value.span()))?;
                Some(Statement::Let {
                    name: name.text.clone(),
                    mutable: *mutable,
                    ty,
                    value: self.lower_expression(value)?,
                    span: statement.span(),
                })
            }
            crate::ast::Statement::Return { value, span } => Some(Statement::Return(
                value
                    .as_ref()
                    .and_then(|value| self.lower_expression(value)),
                *span,
            )),
            crate::ast::Statement::Expression { expression, span } => {
                if let crate::ast::Expr::Binary {
                    left,
                    op: BinaryOp::Assign,
                    right,
                    ..
                } = expression
                {
                    if let crate::ast::Expr::Name(name) = left.as_ref() {
                        return Some(Statement::Assign {
                            name: name.text.clone(),
                            value: self.lower_expression(right)?,
                            span: *span,
                        });
                    }
                    self.errors.push(Diagnostic::new(
                        "native backend currently supports assignment to local names only",
                        left.span(),
                    ));
                    return None;
                }
                Some(Statement::Expression(
                    self.lower_expression(expression)?,
                    *span,
                ))
            }
            crate::ast::Statement::If {
                condition,
                then_block,
                else_branch,
                span,
            } => {
                let else_body = match else_branch {
                    Some(ElseBranch::Block(block)) => self.lower_block(block),
                    Some(ElseBranch::If(nested)) => {
                        self.lower_statement(nested).into_iter().collect()
                    }
                    None => Vec::new(),
                };
                Some(Statement::If {
                    condition: self.lower_expression(condition)?,
                    then_body: self.lower_block(then_block),
                    else_body,
                    span: *span,
                })
            }
            crate::ast::Statement::While {
                condition,
                body,
                span,
            } => Some(Statement::While {
                condition: self.lower_expression(condition)?,
                body: self.lower_block(body),
                span: *span,
            }),
        }
    }

    fn lower_expression(&mut self, expression: &crate::ast::Expr) -> Option<Expr> {
        let ty = self
            .checked
            .expression_types
            .get(&expression.span())
            .cloned()
            .and_then(|ty| self.lower_type(&ty, expression.span()))?;
        match expression {
            crate::ast::Expr::Integer(value, span) => Some(Expr::Integer(*value, *span)),
            crate::ast::Expr::Float(value, span) => Some(Expr::Float(*value, *span)),
            crate::ast::Expr::Boolean(value, span) => Some(Expr::Boolean(*value, *span)),
            crate::ast::Expr::Name(name) => Some(Expr::Name(name.text.clone(), ty, name.span)),
            crate::ast::Expr::Unary { op, operand, span } => match op {
                UnaryOp::Negate | UnaryOp::Not => Some(Expr::Unary {
                    op: *op,
                    operand: Box::new(self.lower_expression(operand)?),
                    ty,
                    span: *span,
                }),
                _ => {
                    self.errors.push(Diagnostic::new(
                        "native backend does not yet support task operations",
                        *span,
                    ));
                    None
                }
            },
            crate::ast::Expr::Binary {
                left,
                op,
                right,
                span,
            } if *op != BinaryOp::Assign => Some(Expr::Binary {
                left: Box::new(self.lower_expression(left)?),
                op: *op,
                right: Box::new(self.lower_expression(right)?),
                ty,
                operand_ty: self
                    .checked
                    .expression_types
                    .get(&left.span())
                    .cloned()
                    .and_then(|ty| self.lower_type(&ty, left.span()))?,
                span: *span,
            }),
            crate::ast::Expr::Call { callee, args, span } => {
                let crate::ast::Expr::Name(name) = callee.as_ref() else {
                    self.errors.push(Diagnostic::new(
                        "native backend only supports direct function calls",
                        callee.span(),
                    ));
                    return None;
                };
                if name.text == "print" {
                    self.errors.push(Diagnostic::new(
                        "native print runtime has not been implemented yet",
                        *span,
                    ));
                    return None;
                }
                Some(Expr::Call {
                    name: name.text.clone(),
                    args: args
                        .iter()
                        .filter_map(|arg| self.lower_expression(arg))
                        .collect(),
                    result: ty,
                    span: *span,
                })
            }
            crate::ast::Expr::Binary { span, .. } => {
                self.errors.push(Diagnostic::new(
                    "nested assignment is not supported by the native backend",
                    *span,
                ));
                None
            }
            _ => {
                self.errors.push(Diagnostic::new(
                    "native scalar backend does not support this expression yet",
                    expression.span(),
                ));
                None
            }
        }
    }
}
