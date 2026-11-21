use std::env;
use std::fs;
use std::path::Path;
use std::process::ExitCode;
use tyvera::{check, lex, native, parse};

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
    let command = &args[0];
    let expected_args = if matches!(command.as_str(), "object" | "build") {
        3
    } else {
        2
    };
    if args.len() != expected_args {
        return Err(
            "usage: tyvera <parse|check|run|lex> <source.tyv>\n       tyvera object <source.tyv> <output.obj>\n       tyvera build <source.tyv> <output.exe>"
                .into(),
        );
    }
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
        "run" => {
            let module = parse(&source)
                .map_err(|diagnostic| format_diagnostic(path, &source, &diagnostic))?;
            let checked = check(&module).map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            let result = native::run(&checked, "main").map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            println!("{result}");
        }
        "object" => {
            let module = parse(&source)
                .map_err(|diagnostic| format_diagnostic(path, &source, &diagnostic))?;
            let checked = check(&module).map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            let object = native::emit_object(&checked).map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            let output_path = &args[2];
            fs::write(output_path, object)
                .map_err(|error| format!("{output_path}: could not write object file: {error}"))?;
            println!("wrote native object: {output_path}");
        }
        "build" => {
            let module = parse(&source)
                .map_err(|diagnostic| format_diagnostic(path, &source, &diagnostic))?;
            let checked = check(&module).map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            let output_path = &args[2];
            native::build_executable(&checked, Path::new(output_path)).map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format_diagnostic(path, &source, diagnostic))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            println!("built native executable: {output_path}");
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
                "unknown command '{command}'; expected 'parse', 'check', 'run', 'object', 'build', or 'lex'"
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
    println!("Tyvera 0.1.0 — native compiler prototype");
    println!("usage: tyvera <parse|check|run|lex> <source.tyv>");
    println!("       tyvera object <source.tyv> <output.obj>");
    println!("       tyvera build <source.tyv> <output.exe>");
    println!("       tyvera --version");
    println!();
    println!(
        "'parse' checks syntax; 'check' checks types; 'run' JIT-compiles fn main() -> Int; 'object' emits native object code; 'build' links a Windows x64 executable."
    );
}
