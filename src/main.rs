use std::env;
use std::fs;
use std::path::Path;
use std::process::ExitCode;
use tyvera::{lex, native, parse};

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
            "usage: tyvera <parse|check|run|modules|lex> <source.tyv>\n       tyvera object <source.tyv> <output.obj>\n       tyvera build <source.tyv> <output.exe>"
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
            let graph = tyvera::resolver::load_module_graph(path)
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
            let checked = tyvera::typecheck::check_graph(&graph)
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
            let expression_count = checked
                .modules
                .iter()
                .map(|module| module.checked.expression_types.len())
                .sum::<usize>();
            println!(
                "type check ok: {} module(s), {expression_count} typed expressions",
                checked.modules.len()
            );
        }
        "run" => {
            let graph = tyvera::resolver::load_module_graph(path)
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
            let checked = tyvera::typecheck::check_graph(&graph)
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
            let result = native::run_graph(&checked)
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
            println!("{result}");
        }
        "modules" => {
            let graph = tyvera::resolver::load_module_graph(path).map_err(|diagnostics| {
                diagnostics
                    .iter()
                    .map(|diagnostic| format!("{path}: module resolution error: {diagnostic}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            })?;
            for loaded in graph.modules() {
                let name = loaded
                    .module
                    .name
                    .as_ref()
                    .map(|name| name.text.as_str())
                    .unwrap_or("<unnamed>");
                println!("{name}: {}", loaded.path.display());
            }
        }
        "object" => {
            let checked = load_checked_graph(path)?;
            let object = native::emit_object_graph(&checked)
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
            let output_path = &args[2];
            fs::write(output_path, object)
                .map_err(|error| format!("{output_path}: could not write object file: {error}"))?;
            println!("wrote native object: {output_path}");
        }
        "build" => {
            let checked = load_checked_graph(path)?;
            let output_path = &args[2];
            native::build_executable_graph(&checked, Path::new(output_path))
                .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
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
                "unknown command '{command}'; expected 'parse', 'check', 'run', 'modules', 'object', 'build', or 'lex'"
            ));
        }
    }
    Ok(())
}

fn load_checked_graph(path: &str) -> Result<tyvera::typecheck::CheckedProgram, String> {
    let graph = tyvera::resolver::load_module_graph(path)
        .map_err(|diagnostics| render_diagnostics(&diagnostics))?;
    tyvera::typecheck::check_graph(&graph).map_err(|diagnostics| render_diagnostics(&diagnostics))
}

fn format_diagnostic(path: &str, source: &str, diagnostic: &tyvera::Diagnostic) -> String {
    let position = diagnostic.position(source);
    format!(
        "{path}:{}:{}: error: {}",
        position.line, position.column, diagnostic.message
    )
}

fn render_diagnostics(diagnostics: &[tyvera::Diagnostic]) -> String {
    diagnostics
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join("\n")
}

fn print_help() {
    println!("Tyvera 0.1.0 — native compiler prototype");
    println!("usage: tyvera <parse|check|run|modules|lex> <source.tyv>");
    println!("       tyvera object <source.tyv> <output.obj>");
    println!("       tyvera build <source.tyv> <output.exe>");
    println!("       tyvera --version");
    println!();
    println!(
        "'parse' checks syntax; 'check' checks types; 'run' JIT-compiles fn main() -> Int; 'modules' resolves source imports; 'object' emits native object code; 'build' links a Windows x64 executable."
    );
}
