use tyvera::{
    ast::{Expr, Item, Statement},
    check, lex, parse,
};

#[test]
fn lex_and_parse_the_checked_in_hello_program() {
    let source = include_str!("../examples/hello.tyv");
    assert!(lex(source).is_ok());
    let module = parse(source).unwrap();
    assert_eq!(module.name.as_ref().unwrap().text, "hello");
    assert_eq!(module.items.len(), 1);
    assert!(check(&module).is_ok());
}

#[test]
fn parser_preserves_byte_spans_for_non_ascii_string_contents() {
    let source = "module hi; fn main() { let text = \"Grüße\"; }";
    let module = parse(source).unwrap();
    let Item::Function(function) = &module.items[0] else {
        panic!("expected function");
    };
    let Statement::Let {
        value: Expr::String(_, span),
        ..
    } = &function.body.statements[0]
    else {
        panic!("expected string binding");
    };
    assert_eq!(&source[span.start..span.end], "\"Grüße\"");
}
