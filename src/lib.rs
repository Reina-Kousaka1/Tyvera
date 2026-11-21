//! Tyvera's compiler and runtime prototype.
//!
//! The crate currently includes a lexer, parser, single-module type checker,
//! scalar native back ends, and an early heap runtime that is not yet connected
//! to compiled Tyvera programs.

pub mod ast;
pub mod diagnostic;
pub mod ir;
pub mod lexer;
pub mod native;
pub mod parser;
pub mod runtime;
pub mod token;
pub mod typecheck;

pub use ast::Module;
pub use diagnostic::{Diagnostic, Position, Span};
pub use lexer::lex;
pub use parser::parse;
pub use typecheck::{CheckedModule, ValueType, check};
