//! Tyvera's first native front end.
//!
//! This crate intentionally starts with source locations, lexing, a syntax tree,
//! and parsing. Semantic analysis and code generation are separate future stages.

pub mod ast;
pub mod diagnostic;
pub mod lexer;
pub mod parser;
pub mod token;

pub use ast::Module;
pub use diagnostic::{Diagnostic, Position, Span};
pub use lexer::lex;
pub use parser::parse;
