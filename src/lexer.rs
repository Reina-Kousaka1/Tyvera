use crate::diagnostic::{Diagnostic, Span};
use crate::token::{Token, TokenKind};

/// Convert Tyvera source text into tokens. Spans are UTF-8 byte offsets.
pub fn lex(source: &str) -> Result<Vec<Token>, Vec<Diagnostic>> {
    Scanner::new(source).scan()
}

struct Scanner<'a> {
    source: &'a str,
    bytes: &'a [u8],
    offset: usize,
    tokens: Vec<Token>,
    errors: Vec<Diagnostic>,
}

impl<'a> Scanner<'a> {
    fn new(source: &'a str) -> Self {
        Self {
            source,
            bytes: source.as_bytes(),
            offset: 0,
            tokens: Vec::new(),
            errors: Vec::new(),
        }
    }

    fn scan(mut self) -> Result<Vec<Token>, Vec<Diagnostic>> {
        while !self.at_end() {
            self.skip_trivia();
            if self.at_end() {
                break;
            }
            self.scan_token();
        }
        self.tokens.push(Token::new(
            TokenKind::Eof,
            Span::new(self.offset, self.offset),
        ));
        if self.errors.is_empty() {
            Ok(self.tokens)
        } else {
            Err(self.errors)
        }
    }

    fn at_end(&self) -> bool {
        self.offset >= self.bytes.len()
    }

    fn byte(&self) -> Option<u8> {
        self.bytes.get(self.offset).copied()
    }

    fn byte_after(&self, distance: usize) -> Option<u8> {
        self.bytes.get(self.offset + distance).copied()
    }

    fn advance(&mut self) -> Option<u8> {
        let byte = self.byte()?;
        self.offset += 1;
        Some(byte)
    }

    fn push(&mut self, kind: TokenKind, start: usize) {
        self.tokens
            .push(Token::new(kind, Span::new(start, self.offset)));
    }

    fn error(&mut self, message: impl Into<String>, start: usize) {
        self.errors.push(Diagnostic::new(
            message,
            Span::new(start, self.offset.max(start + 1)),
        ));
    }

    fn skip_trivia(&mut self) {
        loop {
            match (self.byte(), self.byte_after(1)) {
                (Some(b' ' | b'\r' | b'\t' | b'\n'), _) => {
                    self.advance();
                }
                (Some(b'/'), Some(b'/')) => {
                    while !matches!(self.byte(), None | Some(b'\n')) {
                        self.advance();
                    }
                }
                (Some(b'/'), Some(b'*')) => {
                    let start = self.offset;
                    self.advance();
                    self.advance();
                    let mut closed = false;
                    while !self.at_end() {
                        if self.byte() == Some(b'*') && self.byte_after(1) == Some(b'/') {
                            self.advance();
                            self.advance();
                            closed = true;
                            break;
                        }
                        self.advance();
                    }
                    if !closed {
                        self.errors.push(Diagnostic::new(
                            "unterminated block comment",
                            Span::new(start, self.offset),
                        ));
                    }
                }
                _ => break,
            }
        }
    }

    fn scan_token(&mut self) {
        let start = self.offset;
        let Some(byte) = self.advance() else {
            return;
        };

        if is_identifier_start(byte) {
            self.scan_identifier(start);
            return;
        }
        if byte.is_ascii_digit() {
            self.scan_number(start);
            return;
        }

        let kind = match byte {
            b'"' => {
                self.scan_string(start);
                return;
            }
            b'(' => Some(TokenKind::LParen),
            b')' => Some(TokenKind::RParen),
            b'{' => Some(TokenKind::LBrace),
            b'}' => Some(TokenKind::RBrace),
            b'[' => Some(TokenKind::LBracket),
            b']' => Some(TokenKind::RBracket),
            b',' => Some(TokenKind::Comma),
            b':' => Some(TokenKind::Colon),
            b';' => Some(TokenKind::Semicolon),
            b'.' => Some(TokenKind::Dot),
            b'?' => Some(TokenKind::Question),
            b'+' => Some(TokenKind::Plus),
            b'*' => Some(TokenKind::Star),
            b'%' => Some(TokenKind::Percent),
            b'-' if self.take(b'>') => Some(TokenKind::Arrow),
            b'-' => Some(TokenKind::Minus),
            b'/' => Some(TokenKind::Slash),
            b'!' if self.take(b'=') => Some(TokenKind::BangEqual),
            b'!' => Some(TokenKind::Bang),
            b'=' if self.take(b'=') => Some(TokenKind::EqualEqual),
            b'=' => Some(TokenKind::Equal),
            b'<' if self.take(b'=') => Some(TokenKind::LessEqual),
            b'<' => Some(TokenKind::Less),
            b'>' if self.take(b'=') => Some(TokenKind::GreaterEqual),
            b'>' => Some(TokenKind::Greater),
            b'&' if self.take(b'&') => Some(TokenKind::AndAnd),
            b'|' if self.take(b'|') => Some(TokenKind::OrOr),
            _ => None,
        };

        if let Some(kind) = kind {
            self.push(kind, start);
        } else {
            let ch = self.source[start..].chars().next().unwrap_or('\u{fffd}');
            if ch.len_utf8() > 1 {
                self.offset = start + ch.len_utf8();
            }
            self.error(format!("unexpected character {ch:?}"), start);
        }
    }

    fn take(&mut self, expected: u8) -> bool {
        if self.byte() == Some(expected) {
            self.advance();
            true
        } else {
            false
        }
    }

    fn scan_identifier(&mut self, start: usize) {
        while self.byte().is_some_and(is_identifier_continue) {
            self.advance();
        }
        let text = &self.source[start..self.offset];
        let kind = match text {
            "module" => TokenKind::Module,
            "import" => TokenKind::Import,
            "as" => TokenKind::As,
            "pub" => TokenKind::Pub,
            "fn" => TokenKind::Fn,
            "task" => TokenKind::Task,
            "struct" => TokenKind::Struct,
            "enum" => TokenKind::Enum,
            "type" => TokenKind::Type,
            "const" => TokenKind::Const,
            "let" => TokenKind::Let,
            "var" => TokenKind::Var,
            "return" => TokenKind::Return,
            "if" => TokenKind::If,
            "else" => TokenKind::Else,
            "while" => TokenKind::While,
            "spawn" => TokenKind::Spawn,
            "await" => TokenKind::Await,
            "chan" => TokenKind::Chan,
            "true" => TokenKind::True,
            "false" => TokenKind::False,
            "nil" => TokenKind::Nil,
            _ => TokenKind::Identifier(text.to_owned()),
        };
        self.push(kind, start);
    }

    fn scan_number(&mut self, start: usize) {
        while self
            .byte()
            .is_some_and(|byte| byte.is_ascii_digit() || byte == b'_')
        {
            self.advance();
        }

        let mut is_float = false;
        if self.byte() == Some(b'.') && self.byte_after(1).is_some_and(|b| b.is_ascii_digit()) {
            is_float = true;
            self.advance();
            while self
                .byte()
                .is_some_and(|byte| byte.is_ascii_digit() || byte == b'_')
            {
                self.advance();
            }
        }

        if matches!(self.byte(), Some(b'e' | b'E')) {
            is_float = true;
            self.advance();
            if matches!(self.byte(), Some(b'+' | b'-')) {
                self.advance();
            }
            let exponent_start = self.offset;
            while self
                .byte()
                .is_some_and(|byte| byte.is_ascii_digit() || byte == b'_')
            {
                self.advance();
            }
            if exponent_start == self.offset {
                self.error("expected digits in exponent", start);
            }
        }

        let text = self.source[start..self.offset].to_owned();
        self.push(
            if is_float {
                TokenKind::Float(text)
            } else {
                TokenKind::Integer(text)
            },
            start,
        );
    }

    fn scan_string(&mut self, start: usize) {
        let mut value = String::new();
        let mut terminated = false;

        while !self.at_end() {
            match self.byte() {
                Some(b'"') => {
                    self.advance();
                    terminated = true;
                    break;
                }
                Some(b'\n') => {
                    self.error("newline in string literal", start);
                    break;
                }
                Some(b'\\') => {
                    self.advance();
                    match self.advance() {
                        Some(b'n') => value.push('\n'),
                        Some(b'r') => value.push('\r'),
                        Some(b't') => value.push('\t'),
                        Some(b'0') => value.push('\0'),
                        Some(b'"') => value.push('"'),
                        Some(b'\\') => value.push('\\'),
                        Some(other) => self.error(
                            format!("unknown string escape \\{}", other as char),
                            self.offset.saturating_sub(2),
                        ),
                        None => break,
                    }
                }
                Some(_) => {
                    let ch = self.source[self.offset..].chars().next().unwrap();
                    value.push(ch);
                    self.offset += ch.len_utf8();
                }
                None => break,
            }
        }

        if !terminated {
            self.error("unterminated string literal", start);
        }
        self.push(TokenKind::String(value), start);
    }
}

fn is_identifier_start(byte: u8) -> bool {
    byte.is_ascii_alphabetic() || byte == b'_'
}

fn is_identifier_continue(byte: u8) -> bool {
    is_identifier_start(byte) || byte.is_ascii_digit()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lexes_keywords_operators_and_utf8_strings_with_byte_spans() {
        let tokens = lex("module λ;").unwrap_err();
        assert_eq!(tokens[0].message, "unexpected character 'λ'");

        let tokens = lex("module main; let s = \"hé\"; a->b && c!=d").unwrap();
        assert!(matches!(tokens[0].kind, TokenKind::Module));
        assert!(matches!(&tokens[1].kind, TokenKind::Identifier(name) if name == "main"));
        assert!(matches!(&tokens[6].kind, TokenKind::String(value) if value == "hé"));
        assert!(tokens.iter().any(|token| token.kind == TokenKind::Arrow));
        assert!(tokens.iter().any(|token| token.kind == TokenKind::AndAnd));
        assert!(
            tokens
                .iter()
                .any(|token| token.kind == TokenKind::BangEqual)
        );
    }

    #[test]
    fn skips_line_and_block_comments_and_lexes_numbers() {
        let tokens = lex("12 3.5e-2 // rest\n/* block */ 7_000").unwrap();
        assert!(matches!(&tokens[0].kind, TokenKind::Integer(value) if value == "12"));
        assert!(matches!(&tokens[1].kind, TokenKind::Float(value) if value == "3.5e-2"));
        assert!(matches!(&tokens[2].kind, TokenKind::Integer(value) if value == "7_000"));
        assert!(matches!(tokens[3].kind, TokenKind::Eof));
    }

    #[test]
    fn reports_unterminated_comments_and_strings() {
        assert!(lex("/* never closes").is_err());
        assert!(lex("\"never closes").is_err());
    }
}
