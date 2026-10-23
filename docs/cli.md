# Tyvera CLI reference

## Supported commands

| Command | Behavior |
| --- | --- |
| `tyvera --version` | Print the installed toolchain version. |
| `tyvera new NAME` | Create `NAME/tyvera.toml`, `src/main.tyv`, a second imported module, `.gitignore` and `.tyvera.secrets.example`. Refuses to replace an existing path. |
| `tyvera check PATH` | Load and type-check a project directory or `.tyv` source file without running `main`. |
| `tyvera run PATH` | Load, type-check and run a project directory or `.tyv` source file. |

For a project directory, `PATH` must contain `tyvera.toml`. For a source file,
Tyvera uses the nearest parent manifest when present; otherwise the file's
parent directory is the project root. The manifest supports only two quoted
string fields today:

```toml
name = "my_app"
entry = "src/main.tyv"
```

Without an explicit path, `run` and `check` use the current directory as a
project. Package commands, `test`, and `fmt` are not implemented yet.

## Planned native build

`tyvera build` is not implemented. Its acceptance condition is a successful
native executable for a selected target that runs without Python installed on
the target system. A command that only validates a project, emits source, or
launches the interpreter does not satisfy that contract. Target selection,
toolchain diagnostics, output paths, and build profiles will be finalized with
the first executable backend; see [Backend Architecture](backend-architecture.md).

