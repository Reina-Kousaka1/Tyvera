use std::env;
use std::fs;
use std::process::ExitCode;
use tyvera::{check, lex, parse};

fn main() -> ExitCode {
    match run(env::args().skip(1).collect()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: Vec<String>) -> Result<(), String> {
    if args.as_slice() == ["--version"] || args.as_slice() == ["-V"] {
        println!("tyvera {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.is_empty() || args == ["--help"] || args == ["-h"] {
        print_help();
        return Ok(());
    }
    if args.len() != 2 {
        return Err("usage: tyvera <parse|lex> <source.tyv>".into());
    }
    let command = &args[0];
    let path = &args[1];
    let source = fs::read_to_string(path)
        .map_err(|error| format!("{path}: could not read source: {error}"))?;

    match command.as_str() {
        "parse" => match parse(&source) {
            Ok(module) => println!(
                "syntax ok: module {} ({} top-level items)",
                module
                    .name
                    .as_ref()
                    .map(|name| name.text.as_str())
                    .unwrap_or("<anonymous>"),
                module.items.len()
            ),
            Err(diagnostic) => return Err(format_diagnostic(path, &source, &diagnostic)),
        },
        "check" => {
            let module = parse(&source)
                .map_err(|diagnostic| format_diagnostic(path, &source, &diagnostic))?;
            match check(&module) {
                Ok(checked) => println!(
                    "type check ok: module {} ({} typed expressions)",
                    checked
                        .module
                        .name
                        .as_ref()
                        .map(|name| name.text.as_str())
                        .unwrap_or("<anonymous>"),
                    checked.expression_types.len()
                ),
                Err(diagnostics) => {
                    let rendered = diagnostics
                        .iter()
                        .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                        .collect::<Vec<_>>()
                        .join("\n");
                    return Err(rendered);
                }
            }
        }
        "lex" => match lex(&source) {
            Ok(tokens) => {
                for token in tokens {
                    println!(
                        "{:?} @ {}..{}",
                        token.kind, token.span.start, token.span.end
                    );
                }
            }
            Err(diagnostics) => {
                let rendered = diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n");
                return Err(rendered);
            }
        },
        _ => {
            return Err(format!(
                "unknown command '{command}'; expected 'parse', 'check', or 'lex'"
            ));
        }
    }
    Ok(())
}

fn format_diagnostic(path: &str, source: &str, diagnostic: &tyvera::Diagnostic) -> String {
    let position = diagnostic.position(source);
    format!(
        "{path}:{}:{}: error: {}",
        position.line, position.column, diagnostic.message
    )
}

fn print_help() {
    println!("Tyvera 0.1.0 — native compiler frontend");
    println!("usage: tyvera <parse|check|lex> <source.tyv>");
    println!("       tyvera --version");
    println!();
    println!(
        "'parse' checks syntax; 'check' also checks the supported single-module type rules. Code generation is not implemented yet."
    );
}
