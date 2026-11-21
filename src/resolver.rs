//! Source-file module graph loading and import-path validation.

use std::collections::HashMap;
use std::fs;
use std::path::{Component, Path, PathBuf};

use crate::ast::{ImportDecl, Module};
use crate::diagnostic::{Diagnostic, Span};
use crate::parse;

const MAX_IMPORT_DEPTH: usize = 256;

/// One parsed source module. Modules are stored in dependency-first order.
#[derive(Clone, Debug)]
pub struct SourceModule {
    pub path: PathBuf,
    pub module: Module,
}

/// All source modules reachable from an entry file.
#[derive(Clone, Debug)]
pub struct ModuleGraph {
    entry: PathBuf,
    modules: Vec<SourceModule>,
}

impl ModuleGraph {
    /// The canonical path passed to [`load_module_graph`].
    pub fn entry(&self) -> &Path {
        &self.entry
    }

    /// Modules in dependency-first order, with each file included once.
    pub fn modules(&self) -> &[SourceModule] {
        &self.modules
    }

    /// Find a loaded source file by its canonical path.
    pub fn get(&self, path: &Path) -> Option<&SourceModule> {
        self.modules.iter().find(|module| module.path == path)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum VisitState {
    Visiting,
    Loaded,
}

/// Load an entry `.tyv` file and its transitive imports.
///
/// Import paths are relative to the importing file. A `.tyv` extension is
/// added when omitted. Resolved files must stay inside the entry file's
/// directory tree, including after symlink resolution.
pub fn load_module_graph(entry: impl AsRef<Path>) -> Result<ModuleGraph, Vec<Diagnostic>> {
    let entry = entry.as_ref().canonicalize().map_err(|error| {
        vec![Diagnostic::new(
            format!(
                "could not resolve entry module '{}': {error}",
                entry.as_ref().display()
            ),
            Span::default(),
        )]
    })?;
    let root = entry.parent().unwrap_or(Path::new(".")).to_path_buf();
    let mut loader = Loader {
        root,
        states: HashMap::new(),
        module_names: HashMap::new(),
        modules: Vec::new(),
        errors: Vec::new(),
    };
    loader.visit(&entry, Span::default(), 0);
    if loader.errors.is_empty() {
        Ok(ModuleGraph {
            entry,
            modules: loader.modules,
        })
    } else {
        Err(loader.errors)
    }
}

struct Loader {
    root: PathBuf,
    states: HashMap<PathBuf, VisitState>,
    module_names: HashMap<String, PathBuf>,
    modules: Vec<SourceModule>,
    errors: Vec<Diagnostic>,
}

impl Loader {
    fn visit(&mut self, path: &Path, edge_span: Span, depth: usize) {
        if depth > MAX_IMPORT_DEPTH {
            self.errors.push(Diagnostic::new(
                format!("module import depth exceeds the limit of {MAX_IMPORT_DEPTH}"),
                edge_span,
            ));
            return;
        }
        let path = match path.canonicalize() {
            Ok(path) => path,
            Err(error) => {
                self.errors.push(Diagnostic::new(
                    format!(
                        "could not resolve imported module '{}': {error}",
                        path.display()
                    ),
                    edge_span,
                ));
                return;
            }
        };
        if !path.starts_with(&self.root) {
            self.errors.push(Diagnostic::new(
                format!(
                    "module '{}' resolves outside the entry directory",
                    path.display()
                ),
                edge_span,
            ));
            return;
        }
        match self.states.get(&path) {
            Some(VisitState::Loaded) => return,
            Some(VisitState::Visiting) => {
                self.errors.push(Diagnostic::new(
                    format!("cyclic module import involving '{}'", path.display()),
                    edge_span,
                ));
                return;
            }
            None => {}
        }
        self.states.insert(path.clone(), VisitState::Visiting);

        let source = match fs::read_to_string(&path) {
            Ok(source) => source,
            Err(error) => {
                self.errors.push(Diagnostic::new(
                    format!("could not read module '{}': {error}", path.display()),
                    edge_span,
                ));
                self.states.remove(&path);
                return;
            }
        };
        let module = match parse(&source) {
            Ok(module) => module,
            Err(mut diagnostic) => {
                diagnostic.message = format!("{}: {}", path.display(), diagnostic.message);
                self.errors.push(diagnostic);
                self.states.remove(&path);
                return;
            }
        };

        if let Some(name) = &module.name {
            if let Some(previous) = self.module_names.get(&name.text) {
                if previous != &path {
                    self.errors.push(Diagnostic::new(
                        format!(
                            "module name '{}' is declared by both '{}' and '{}'",
                            name.text,
                            previous.display(),
                            path.display()
                        ),
                        name.span,
                    ));
                }
            } else {
                self.module_names.insert(name.text.clone(), path.clone());
            }
        }

        for import in module.items.iter().filter_map(|item| match item {
            crate::ast::Item::Import(import) => Some(import),
            _ => None,
        }) {
            if let Some(import_path) = self.resolve_import(&path, import) {
                self.visit(&import_path, import.span, depth + 1);
            }
        }

        self.states.insert(path.clone(), VisitState::Loaded);
        self.modules.push(SourceModule { path, module });
    }

    fn resolve_import(&mut self, importer: &Path, import: &ImportDecl) -> Option<PathBuf> {
        let relative = Path::new(&import.path);
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| matches!(component, Component::Prefix(_)))
        {
            self.errors.push(Diagnostic::new(
                "import path must be relative to its source module",
                import.span,
            ));
            return None;
        }
        let mut target = importer.parent().unwrap_or(&self.root).join(relative);
        if target.extension().is_none() {
            target.set_extension("tyv");
        }
        Some(target)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_PROJECT: AtomicU64 = AtomicU64::new(0);

    struct TempProject(PathBuf);

    impl TempProject {
        fn new() -> Self {
            let id = NEXT_PROJECT.fetch_add(1, Ordering::Relaxed);
            let path =
                std::env::temp_dir().join(format!("tyvera-resolver-{}-{id}", std::process::id()));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn write(&self, relative: &str, contents: &str) -> PathBuf {
            let path = self.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, contents).unwrap();
            path
        }
    }

    impl Drop for TempProject {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn loads_shared_imports_once_in_dependency_order() {
        let project = TempProject::new();
        let entry = project.write(
            "main.tyv",
            "module app; import \"lib/geometry\"; import \"lib/math.tyv\";",
        );
        project.write(
            "lib/geometry.tyv",
            "module geometry; import \"math\" as math;",
        );
        project.write("lib/math.tyv", "module math;");

        let graph = load_module_graph(entry).unwrap();

        let names = graph
            .modules()
            .iter()
            .map(|loaded| loaded.module.name.as_ref().unwrap().text.as_str())
            .collect::<Vec<_>>();
        assert_eq!(names, ["math", "geometry", "app"]);
        assert_eq!(graph.modules().len(), 3);
    }

    #[test]
    fn reports_cycles_and_duplicate_module_names() {
        let project = TempProject::new();
        let entry = project.write("main.tyv", "module app; import \"a\";");
        project.write("a.tyv", "module shared; import \"b\";");
        project.write("b.tyv", "module shared; import \"a\";");

        let errors = load_module_graph(entry).unwrap_err();

        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("cyclic module import"))
        );
        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("declared by both"))
        );
    }

    #[test]
    fn rejects_imports_that_resolve_outside_the_project_root() {
        let project = TempProject::new();
        let sibling = project.0.file_name().unwrap().to_string_lossy();
        let outside_import = format!("../{sibling}-outside");
        let entry = project.write(
            "main.tyv",
            &format!("module app; import \"{outside_import}\";"),
        );
        let outside = project.0.with_file_name(format!("{sibling}-outside.tyv"));
        fs::write(&outside, "module outside;").unwrap();

        let errors = load_module_graph(entry).unwrap_err();

        assert!(
            errors
                .iter()
                .any(|error| error.message.contains("outside the entry directory"))
        );
        let _ = fs::remove_file(outside);
    }
}
