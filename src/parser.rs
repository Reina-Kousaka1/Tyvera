use crate::ast::*;
use crate::diagnostic::{Diagnostic, Span};
use crate::lexer::lex;
use crate::token::{Token, TokenKind};

/// Parse one complete Tyvera source file.
pub fn parse(source: &str) -> Result<Module, Diagnostic> {
    let tokens = lex(source).map_err(|mut errors| errors.remove(0))?;
    Parser::new(tokens).module()
}

struct Parser {
    tokens: Vec<Token>,
    current: usize,
}

impl Parser {
    fn new(tokens: Vec<Token>) -> Self {
        Self { tokens, current: 0 }
    }

    fn module(mut self) -> Result<Module, Diagnostic> {
        let start = self.peek().span.start;
        let name = if self.take(&TokenKind::Module) {
            let name = self.identifier("module name")?;
            self.expect(&TokenKind::Semicolon, "';' after module declaration")?;
            Some(name)
        } else {
            return Err(self.error("expected a module declaration"));
        };

        let mut items = Vec::new();
        while !self.at(&TokenKind::Eof) {
            let before = self.current;
            items.push(self.item()?);
            if self.current == before {
                return Err(self.error("parser made no progress at top level"));
            }
        }
        let end = self.peek().span.end;
        Ok(Module {
            name,
            items,
            span: Span::new(start, end),
        })
    }

    fn item(&mut self) -> Result<Item, Diagnostic> {
        let start = self.peek().span.start;
        let is_public = self.take(&TokenKind::Pub);

        if self.at(&TokenKind::Import) {
            if is_public {
                return Err(self.error("'pub' cannot modify an import"));
            }
            return self.import_decl().map(Item::Import);
        }
        if self.at(&TokenKind::Task) || self.at(&TokenKind::Fn) {
            return self.function_decl(is_public, start).map(Item::Function);
        }
        if self.take(&TokenKind::Struct) {
            return self.struct_decl(is_public, start).map(Item::Struct);
        }
        if self.take(&TokenKind::Enum) {
            return self.enum_decl(is_public, start).map(Item::Enum);
        }
        if self.take(&TokenKind::Type) {
            return self.type_alias_decl(is_public, start).map(Item::TypeAlias);
        }
        if self.take(&TokenKind::Const) {
            return self.constant_decl(is_public, start).map(Item::Constant);
        }
        Err(self.error("expected import, declaration, or function"))
    }

    fn import_decl(&mut self) -> Result<ImportDecl, Diagnostic> {
        let start = self.expect(&TokenKind::Import, "'import'")?.span.start;
        let token = self.advance().clone();
        let TokenKind::String(path) = token.kind else {
            return Err(Diagnostic::new("expected a quoted import path", token.span));
        };
        let alias = if self.take(&TokenKind::As) {
            Some(self.identifier("import alias")?)
        } else {
            None
        };
        let end = self
            .expect(&TokenKind::Semicolon, "';' after import")?
            .span
            .end;
        Ok(ImportDecl {
            path,
            alias,
            span: Span::new(start, end),
        })
    }

    fn function_decl(&mut self, is_public: bool, start: usize) -> Result<FunctionDecl, Diagnostic> {
        let is_task = self.take(&TokenKind::Task);
        self.expect(&TokenKind::Fn, "'fn'")?;
        let name = self.identifier("function name")?;
        self.expect(&TokenKind::LParen, "'(' after function name")?;
        let params = self.parameter_list()?;
        self.expect(&TokenKind::RParen, "')' after parameters")?;
        let result = if self.take(&TokenKind::Arrow) {
            self.ty()?
        } else {
            Type::Unit
        };
        let body = self.block()?;
        let span = Span::new(start, body.span.end);
        Ok(FunctionDecl {
            name,
            is_public,
            is_task,
            params,
            result,
            body,
            span,
        })
    }

    fn parameter_list(&mut self) -> Result<Vec<Parameter>, Diagnostic> {
        let mut params = Vec::new();
        if self.at(&TokenKind::RParen) {
            return Ok(params);
        }
        loop {
            let start = self.peek().span.start;
            let name = self.identifier("parameter name")?;
            self.expect(&TokenKind::Colon, "':' after parameter name")?;
            let ty = self.ty()?;
            let end = self.previous().span.end;
            params.push(Parameter {
                name,
                ty,
                span: Span::new(start, end),
            });
            if !self.take(&TokenKind::Comma) {
                break;
            }
            if self.at(&TokenKind::RParen) {
                break;
            }
        }
        Ok(params)
    }

    fn struct_decl(&mut self, is_public: bool, start: usize) -> Result<StructDecl, Diagnostic> {
        let name = self.identifier("struct name")?;
        self.expect(&TokenKind::LBrace, "'{' after struct name")?;
        let mut fields = Vec::new();
        while !self.at(&TokenKind::RBrace) && !self.at(&TokenKind::Eof) {
            let field_start = self.peek().span.start;
            let field_name = self.identifier("field name")?;
            self.expect(&TokenKind::Colon, "':' after field name")?;
            let ty = self.ty()?;
            let mut end = self.previous().span.end;
            if self.take(&TokenKind::Comma) || self.take(&TokenKind::Semicolon) {
                end = self.previous().span.end;
            } else if !self.at(&TokenKind::RBrace) {
                return Err(self.error("expected ',' or '}' after struct field"));
            }
            fields.push(FieldDecl {
                name: field_name,
                ty,
                span: Span::new(field_start, end),
            });
        }
        let end = self
            .expect(&TokenKind::RBrace, "'}' after struct fields")?
            .span
            .end;
        Ok(StructDecl {
            name,
            is_public,
            fields,
            span: Span::new(start, end),
        })
    }

    fn enum_decl(&mut self, is_public: bool, start: usize) -> Result<EnumDecl, Diagnostic> {
        let name = self.identifier("enum name")?;
        self.expect(&TokenKind::LBrace, "'{' after enum name")?;
        let mut variants = Vec::new();
        while !self.at(&TokenKind::RBrace) && !self.at(&TokenKind::Eof) {
            let variant_start = self.peek().span.start;
            let variant_name = self.identifier("enum variant")?;
            let fields = if self.take(&TokenKind::LParen) {
                let fields = self.type_list(&TokenKind::RParen)?;
                self.expect(&TokenKind::RParen, "')' after enum variant fields")?;
                fields
            } else {
                Vec::new()
            };
            let mut end = self.previous().span.end;
            if self.take(&TokenKind::Comma) || self.take(&TokenKind::Semicolon) {
                end = self.previous().span.end;
            } else if !self.at(&TokenKind::RBrace) {
                return Err(self.error("expected ',' or '}' after enum variant"));
            }
            variants.push(EnumVariant {
                name: variant_name,
                fields,
                span: Span::new(variant_start, end),
            });
        }
        let end = self
            .expect(&TokenKind::RBrace, "'}' after enum variants")?
            .span
            .end;
        Ok(EnumDecl {
            name,
            is_public,
            variants,
            span: Span::new(start, end),
        })
    }

    fn type_alias_decl(
        &mut self,
        is_public: bool,
        start: usize,
    ) -> Result<TypeAliasDecl, Diagnostic> {
        let name = self.identifier("type alias name")?;
        self.expect(&TokenKind::Equal, "'=' in type alias")?;
        let target = self.ty()?;
        let end = self
            .expect(&TokenKind::Semicolon, "';' after type alias")?
            .span
            .end;
        Ok(TypeAliasDecl {
            name,
            is_public,
            target,
            span: Span::new(start, end),
        })
    }

    fn constant_decl(&mut self, is_public: bool, start: usize) -> Result<ConstantDecl, Diagnostic> {
        let name = self.identifier("constant name")?;
        let ty = if self.take(&TokenKind::Colon) {
            Some(self.ty()?)
        } else {
            None
        };
        self.expect(&TokenKind::Equal, "'=' in constant declaration")?;
        let value = self.expression(0)?;
        let end = self
            .expect(&TokenKind::Semicolon, "';' after constant")?
            .span
            .end;
        Ok(ConstantDecl {
            name,
            is_public,
            ty,
            value,
            span: Span::new(start, end),
        })
    }

    fn type_list(&mut self, terminator: &TokenKind) -> Result<Vec<Type>, Diagnostic> {
        let mut types = Vec::new();
        if self.at(terminator) {
            return Ok(types);
        }
        loop {
            types.push(self.ty()?);
            if !self.take(&TokenKind::Comma) {
                break;
            }
            if self.at(terminator) {
                break;
            }
        }
        Ok(types)
    }

    fn ty(&mut self) -> Result<Type, Diagnostic> {
        if self.take(&TokenKind::Question) {
            return Ok(Type::Optional(Box::new(self.ty()?)));
        }
        if self.take(&TokenKind::LBracket) {
            let element = self.ty()?;
            self.expect(&TokenKind::RBracket, "']' after array element type")?;
            return Ok(Type::Array(Box::new(element)));
        }
        if self.take(&TokenKind::Chan) {
            self.expect(&TokenKind::Less, "'<' after 'chan'")?;
            let element = self.ty()?;
            self.expect(&TokenKind::Greater, "'>' after channel element type")?;
            return Ok(Type::Channel(Box::new(element)));
        }
        if self.take(&TokenKind::Fn) {
            self.expect(&TokenKind::LParen, "'(' after 'fn' in function type")?;
            let params = self.type_list(&TokenKind::RParen)?;
            self.expect(&TokenKind::RParen, "')' after function type parameters")?;
            self.expect(&TokenKind::Arrow, "'->' in function type")?;
            return Ok(Type::Function {
                params,
                result: Box::new(self.ty()?),
            });
        }

        let name = self.identifier("type")?;
        if name.text == "Task" && self.take(&TokenKind::Less) {
            let result = self.ty()?;
            self.expect(&TokenKind::Greater, "'>' after task result type")?;
            return Ok(Type::Task(Box::new(result)));
        }
        if self.take(&TokenKind::Less) {
            let args = self.type_list(&TokenKind::Greater)?;
            self.expect(&TokenKind::Greater, "'>' after generic type arguments")?;
            Ok(Type::Generic { base: name, args })
        } else {
            Ok(Type::Named(name))
        }
    }

    fn block(&mut self) -> Result<Block, Diagnostic> {
        let start = self
            .expect(&TokenKind::LBrace, "'{' to start block")?
            .span
            .start;
        let mut statements = Vec::new();
        while !self.at(&TokenKind::RBrace) && !self.at(&TokenKind::Eof) {
            let before = self.current;
            statements.push(self.statement()?);
            if self.current == before {
                return Err(self.error("parser made no progress in block"));
            }
        }
        let end = self
            .expect(&TokenKind::RBrace, "'}' to end block")?
            .span
            .end;
        Ok(Block {
            statements,
            span: Span::new(start, end),
        })
    }

    fn statement(&mut self) -> Result<Statement, Diagnostic> {
        let start = self.peek().span.start;
        if self.at(&TokenKind::Let) || self.at(&TokenKind::Var) {
            return self.let_statement(start);
        }
        if self.take(&TokenKind::Return) {
            let value = if self.at(&TokenKind::Semicolon) {
                None
            } else {
                Some(self.expression(0)?)
            };
            let end = self
                .expect(&TokenKind::Semicolon, "';' after return")?
                .span
                .end;
            return Ok(Statement::Return {
                value,
                span: Span::new(start, end),
            });
        }
        if self.take(&TokenKind::If) {
            let condition = self.expression(0)?;
            let then_block = self.block()?;
            let else_branch = if self.take(&TokenKind::Else) {
                if self.take(&TokenKind::If) {
                    let nested_start = self.previous().span.start;
                    Some(ElseBranch::If(Box::new(self.if_statement(nested_start)?)))
                } else {
                    Some(ElseBranch::Block(self.block()?))
                }
            } else {
                None
            };
            let end = match &else_branch {
                Some(ElseBranch::Block(block)) => block.span.end,
                Some(ElseBranch::If(statement)) => statement.span().end,
                None => then_block.span.end,
            };
            return Ok(Statement::If {
                condition,
                then_block,
                else_branch,
                span: Span::new(start, end),
            });
        }
        if self.take(&TokenKind::While) {
            let condition = self.expression(0)?;
            let body = self.block()?;
            return Ok(Statement::While {
                condition,
                span: Span::new(start, body.span.end),
                body,
            });
        }

        let expression = self.expression(0)?;
        let end = self
            .expect(&TokenKind::Semicolon, "';' after expression")?
            .span
            .end;
        Ok(Statement::Expression {
            expression,
            span: Span::new(start, end),
        })
    }

    fn if_statement(&mut self, start: usize) -> Result<Statement, Diagnostic> {
        let condition = self.expression(0)?;
        let then_block = self.block()?;
        let else_branch = if self.take(&TokenKind::Else) {
            if self.take(&TokenKind::If) {
                let nested_start = self.previous().span.start;
                Some(ElseBranch::If(Box::new(self.if_statement(nested_start)?)))
            } else {
                Some(ElseBranch::Block(self.block()?))
            }
        } else {
            None
        };
        let end = match &else_branch {
            Some(ElseBranch::Block(block)) => block.span.end,
            Some(ElseBranch::If(statement)) => statement.span().end,
            None => then_block.span.end,
        };
        Ok(Statement::If {
            condition,
            then_block,
            else_branch,
            span: Span::new(start, end),
        })
    }

    fn let_statement(&mut self, start: usize) -> Result<Statement, Diagnostic> {
        let mutable = self.take(&TokenKind::Var);
        if !mutable {
            self.expect(&TokenKind::Let, "'let' or 'var'")?;
        }
        let name = self.identifier("binding name")?;
        let annotation = if self.take(&TokenKind::Colon) {
            Some(self.ty()?)
        } else {
            None
        };
        self.expect(&TokenKind::Equal, "'=' in binding")?;
        let value = self.expression(0)?;
        let end = self
            .expect(&TokenKind::Semicolon, "';' after binding")?
            .span
            .end;
        Ok(Statement::Let {
            mutable,
            name,
            annotation,
            value,
            span: Span::new(start, end),
        })
    }

    fn expression(&mut self, min_binding_power: u8) -> Result<Expr, Diagnostic> {
        let token = self.advance().clone();
        let mut left = match token.kind {
            TokenKind::Integer(text) => {
                let normalized = text.replace('_', "");
                let value = normalized.parse::<i64>().map_err(|_| {
                    Diagnostic::new(
                        "integer literal is outside the supported 64-bit range",
                        token.span,
                    )
                })?;
                Expr::Integer(value, token.span)
            }
            TokenKind::Float(text) => {
                let normalized = text.replace('_', "");
                let value = normalized
                    .parse::<f64>()
                    .map_err(|_| Diagnostic::new("invalid floating-point literal", token.span))?;
                if !value.is_finite() {
                    return Err(Diagnostic::new(
                        "floating-point literal must be finite",
                        token.span,
                    ));
                }
                Expr::Float(value, token.span)
            }
            TokenKind::String(value) => Expr::String(value, token.span),
            TokenKind::True => Expr::Boolean(true, token.span),
            TokenKind::False => Expr::Boolean(false, token.span),
            TokenKind::Nil => Expr::Nil(token.span),
            TokenKind::Identifier(text) => Expr::Name(Ident {
                text,
                span: token.span,
            }),
            TokenKind::LParen => {
                let expression = self.expression(0)?;
                self.expect(&TokenKind::RParen, "')' after expression")?;
                expression
            }
            TokenKind::LBracket => self.array_literal(token.span.start)?,
            TokenKind::Minus | TokenKind::Bang | TokenKind::Spawn | TokenKind::Await => {
                let op = match token.kind {
                    TokenKind::Minus => UnaryOp::Negate,
                    TokenKind::Bang => UnaryOp::Not,
                    TokenKind::Spawn => UnaryOp::Spawn,
                    TokenKind::Await => UnaryOp::Await,
                    _ => unreachable!(),
                };
                let operand = self.expression(12)?;
                let span = token.span.join(operand.span());
                Expr::Unary {
                    op,
                    operand: Box::new(operand),
                    span,
                }
            }
            _ => {
                return Err(Diagnostic::new(
                    format!("expected expression, found {}", token.kind.label()),
                    token.span,
                ));
            }
        };

        loop {
            if self.at(&TokenKind::LParen) {
                if 14 < min_binding_power {
                    break;
                }
                self.advance();
                let mut args = Vec::new();
                if !self.at(&TokenKind::RParen) {
                    loop {
                        args.push(self.expression(0)?);
                        if !self.take(&TokenKind::Comma) {
                            break;
                        }
                        if self.at(&TokenKind::RParen) {
                            break;
                        }
                    }
                }
                let end = self
                    .expect(&TokenKind::RParen, "')' after call arguments")?
                    .span
                    .end;
                let span = Span::new(left.span().start, end);
                left = Expr::Call {
                    callee: Box::new(left),
                    args,
                    span,
                };
                continue;
            }
            if self.take(&TokenKind::Dot) {
                if 14 < min_binding_power {
                    return Err(self.error("field access cannot follow this expression"));
                }
                let field = self.identifier("field name after '.'")?;
                let span = Span::new(left.span().start, field.span.end);
                left = Expr::Field {
                    object: Box::new(left),
                    field,
                    span,
                };
                continue;
            }
            if self.take(&TokenKind::LBracket) {
                if 14 < min_binding_power {
                    return Err(self.error("indexing cannot follow this expression"));
                }
                let index = self.expression(0)?;
                let end = self
                    .expect(&TokenKind::RBracket, "']' after index")?
                    .span
                    .end;
                let span = Span::new(left.span().start, end);
                left = Expr::Index {
                    object: Box::new(left),
                    index: Box::new(index),
                    span,
                };
                continue;
            }

            let Some((op, left_bp, right_bp)) = self.binary_operator() else {
                break;
            };
            if left_bp < min_binding_power {
                break;
            }
            self.advance();
            let right = self.expression(right_bp)?;
            let span = left.span().join(right.span());
            left = Expr::Binary {
                left: Box::new(left),
                op,
                right: Box::new(right),
                span,
            };
        }
        Ok(left)
    }

    fn array_literal(&mut self, start: usize) -> Result<Expr, Diagnostic> {
        let mut elements = Vec::new();
        if !self.at(&TokenKind::RBracket) {
            loop {
                elements.push(self.expression(0)?);
                if !self.take(&TokenKind::Comma) {
                    break;
                }
                if self.at(&TokenKind::RBracket) {
                    break;
                }
            }
        }
        let end = self
            .expect(&TokenKind::RBracket, "']' after array literal")?
            .span
            .end;
        Ok(Expr::Array(elements, Span::new(start, end)))
    }

    fn binary_operator(&self) -> Option<(BinaryOp, u8, u8)> {
        let (op, left_bp, right_bp) = match self.peek().kind {
            TokenKind::Equal => (BinaryOp::Assign, 1, 1),
            TokenKind::OrOr => (BinaryOp::Or, 2, 3),
            TokenKind::AndAnd => (BinaryOp::And, 4, 5),
            TokenKind::EqualEqual => (BinaryOp::Equal, 6, 7),
            TokenKind::BangEqual => (BinaryOp::NotEqual, 6, 7),
            TokenKind::Less => (BinaryOp::Less, 6, 7),
            TokenKind::LessEqual => (BinaryOp::LessEqual, 6, 7),
            TokenKind::Greater => (BinaryOp::Greater, 6, 7),
            TokenKind::GreaterEqual => (BinaryOp::GreaterEqual, 6, 7),
            TokenKind::Plus => (BinaryOp::Add, 8, 9),
            TokenKind::Minus => (BinaryOp::Subtract, 8, 9),
            TokenKind::Star => (BinaryOp::Multiply, 10, 11),
            TokenKind::Slash => (BinaryOp::Divide, 10, 11),
            TokenKind::Percent => (BinaryOp::Remainder, 10, 11),
            _ => return None,
        };
        Some((op, left_bp, right_bp))
    }

    fn identifier(&mut self, expected: &str) -> Result<Ident, Diagnostic> {
        let token = self.advance().clone();
        match token.kind {
            TokenKind::Identifier(text) => Ok(Ident {
                text,
                span: token.span,
            }),
            _ => Err(Diagnostic::new(
                format!("expected {expected}, found {}", token.kind.label()),
                token.span,
            )),
        }
    }

    fn expect(&mut self, kind: &TokenKind, expected: &str) -> Result<Token, Diagnostic> {
        if self.at(kind) {
            Ok(self.advance().clone())
        } else {
            Err(self.error(format!("expected {expected}")))
        }
    }

    fn take(&mut self, kind: &TokenKind) -> bool {
        if self.at(kind) {
            self.advance();
            true
        } else {
            false
        }
    }

    fn at(&self, kind: &TokenKind) -> bool {
        std::mem::discriminant(&self.peek().kind) == std::mem::discriminant(kind)
    }

    fn peek(&self) -> &Token {
        &self.tokens[self.current.min(self.tokens.len() - 1)]
    }

    fn previous(&self) -> &Token {
        &self.tokens[self.current.saturating_sub(1)]
    }

    fn advance(&mut self) -> &Token {
        let index = self.current.min(self.tokens.len() - 1);
        if !self.at(&TokenKind::Eof) {
            self.current = (self.current + 1).min(self.tokens.len() - 1);
        }
        &self.tokens[index]
    }

    fn error(&self, message: impl Into<String>) -> Diagnostic {
        Diagnostic::new(message, self.peek().span)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_modules_declarations_and_typed_task_functions() {
        let module = parse(
            r#"
            module demo;
            import "std/io" as io;
            pub struct Point { x: Int, y: Int, }
            pub enum Result { Ok(Int), Error(String), }
            type PointList = [Point];
            type Table = Map<String, Int>;
            const answer: Int = 40 + 2;
            pub task fn work(input: chan<Int>) -> Task<Int> {
                let value: Int = await read(input);
                return value;
            }
            "#,
        )
        .unwrap();

        assert_eq!(module.name.as_ref().unwrap().text, "demo");
        assert_eq!(module.items.len(), 7);
        let Item::Function(function) = &module.items[6] else {
            panic!("expected function");
        };
        assert!(function.is_public);
        assert!(function.is_task);
        assert_eq!(function.params.len(), 1);
    }

    #[test]
    fn parses_precedence_calls_arrays_fields_assignment_and_control_flow() {
        let module = parse(
            r#"
            module calc;
            fn run() -> Int {
                var result = a + b * c == 7 && ok || fallback;
                result = add(result, values[0]).count;
                if result > 2 { return result; } else if result == 2 { return 2; } else { return 0; }
                while result < 1 { result = result + 1; }
            }
            "#,
        )
        .unwrap();
        let Item::Function(function) = &module.items[0] else {
            panic!("expected function");
        };
        assert_eq!(function.body.statements.len(), 4);
        let Statement::Let { value, .. } = &function.body.statements[0] else {
            panic!("expected binding");
        };
        let Expr::Binary {
            op: BinaryOp::Or,
            left,
            ..
        } = value
        else {
            panic!("or should bind weakest");
        };
        let Expr::Binary {
            op: BinaryOp::And,
            left: and_left,
            ..
        } = left.as_ref()
        else {
            panic!("and should bind more tightly than or");
        };
        let Expr::Binary {
            op: BinaryOp::Equal,
            left: equal_left,
            ..
        } = and_left.as_ref()
        else {
            panic!("equality should bind more tightly than and");
        };
        let Expr::Binary {
            op: BinaryOp::Add,
            right: add_right,
            ..
        } = equal_left.as_ref()
        else {
            panic!("addition should bind more tightly than equality");
        };
        assert!(matches!(
            add_right.as_ref(),
            Expr::Binary {
                op: BinaryOp::Multiply,
                ..
            }
        ));

        let Statement::Expression { expression, .. } = &function.body.statements[1] else {
            panic!("expected assignment expression");
        };
        let Expr::Binary {
            op: BinaryOp::Assign,
            right: assigned,
            ..
        } = expression
        else {
            panic!("expected assignment operator");
        };
        let Expr::Field { object, field, .. } = assigned.as_ref() else {
            panic!("expected field access on call result");
        };
        assert_eq!(field.text, "count");
        let Expr::Call { args, .. } = object.as_ref() else {
            panic!("expected call before field access");
        };
        assert!(matches!(&args[1], Expr::Index { .. }));

        let Statement::If { else_branch, .. } = &function.body.statements[2] else {
            panic!("expected if statement");
        };
        assert!(matches!(else_branch, Some(ElseBranch::If(_))));
        assert!(matches!(
            function.body.statements[3],
            Statement::While { .. }
        ));
    }

    #[test]
    fn reports_unexpected_token_at_one_based_source_position() {
        let error = parse("module bad;\nfn f( { }").unwrap_err();
        assert!(error.message.contains("parameter name"));
        assert_eq!(error.position("module bad;\nfn f( { }").line, 2);
    }
}
