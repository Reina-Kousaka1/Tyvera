use std::collections::HashMap;
use std::mem;
use std::path::Path;
#[cfg(windows)]
use std::{
    env, fs,
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};

use cranelift::codegen::ir::UserFuncName;
use cranelift::prelude::*;
use cranelift_jit::{JITBuilder, JITModule};
use cranelift_module::{FuncId, Linkage, Module, default_libcall_names};
use cranelift_object::{ObjectBuilder, ObjectModule};

use crate::diagnostic::{Diagnostic, Span};
use crate::ir::{self, Expr, Function, IrType, Program, Statement};
use crate::typecheck::{CheckedModule, CheckedProgram};

#[cfg(windows)]
static NEXT_TEMP_FILE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy)]
struct LocalBinding {
    variable: Variable,
}

/// Compile the checked scalar IR to host machine code and call an `Int main()`.
///
/// The result is the integer returned by `main`. This is an in-process JIT
/// execution path; it does not yet emit a standalone executable.
pub fn run(checked: &CheckedModule, entry: &str) -> Result<i64, Vec<Diagnostic>> {
    run_program(ir::lower(checked)?, entry)
}

/// Compile and execute the entry module of a checked multi-module program.
pub fn run_graph(checked: &CheckedProgram) -> Result<i64, Vec<Diagnostic>> {
    run_program(ir::lower_graph(checked)?, "main")
}

fn run_program(program: Program, entry: &str) -> Result<i64, Vec<Diagnostic>> {
    let Some(entry_function) = program
        .functions
        .iter()
        .find(|function| function.name == entry)
    else {
        return Err(vec![Diagnostic::new(
            format!("entry function '{entry}' was not found"),
            Span::default(),
        )]);
    };
    if !entry_function.params.is_empty() || entry_function.result != IrType::Int {
        return Err(vec![Diagnostic::new(
            format!("entry function '{entry}' must have signature fn() -> Int"),
            entry_function.span,
        )]);
    }

    let mut flags = settings::builder();
    if let Err(error) = flags.set("use_colocated_libcalls", "false") {
        return Err(vec![Diagnostic::new(
            error.to_string(),
            entry_function.span,
        )]);
    }
    if let Err(error) = flags.set("is_pic", "false") {
        return Err(vec![Diagnostic::new(
            error.to_string(),
            entry_function.span,
        )]);
    }
    let isa_builder = cranelift_native::builder()
        .map_err(|error| vec![Diagnostic::new(error, entry_function.span)])?;
    let isa = isa_builder
        .finish(settings::Flags::new(flags))
        .map_err(|error| vec![Diagnostic::new(error.to_string(), entry_function.span)])?;
    let mut module = JITModule::new(JITBuilder::with_isa(isa, default_libcall_names()));

    let declarations = declare_functions(&mut module, &program, Linkage::Local)?;
    for function in &program.functions {
        compile_function(&mut module, function, &declarations)?;
    }
    module
        .finalize_definitions()
        .map_err(|error| vec![Diagnostic::new(error.to_string(), entry_function.span)])?;

    let function_id = declarations[entry];
    let address = module.get_finalized_function(function_id);
    // The entry signature was checked above and matches the generated ABI.
    let entry_point: extern "C" fn() -> i64 = unsafe { mem::transmute(address) };
    Ok(entry_point())
}

/// Compile a checked scalar module into a native relocatable object file.
///
/// The returned bytes use the host's object format (COFF on Windows, ELF on
/// Linux, or Mach-O on macOS). Linking a standalone executable is a separate
/// toolchain step and is not provided yet.
pub fn emit_object(checked: &CheckedModule) -> Result<Vec<u8>, Vec<Diagnostic>> {
    emit_object_program(ir::lower(checked)?)
}

/// Emit a host-native object file from every module in a checked program.
pub fn emit_object_graph(checked: &CheckedProgram) -> Result<Vec<u8>, Vec<Diagnostic>> {
    emit_object_program(ir::lower_graph(checked)?)
}

fn emit_object_program(program: Program) -> Result<Vec<u8>, Vec<Diagnostic>> {
    let span = program
        .functions
        .first()
        .map(|function| function.span)
        .unwrap_or_default();
    let isa_builder =
        cranelift_native::builder().map_err(|error| vec![Diagnostic::new(error, span)])?;
    let isa = isa_builder
        .finish(settings::Flags::new(settings::builder()))
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])?;
    let object_builder = ObjectBuilder::new(isa, "tyvera", default_libcall_names())
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])?;
    let mut module = ObjectModule::new(object_builder);
    let declarations = declare_functions(&mut module, &program, Linkage::Export)?;
    for function in &program.functions {
        compile_function(&mut module, function, &declarations)?;
    }
    #[cfg(all(windows, target_arch = "x86_64"))]
    if let Some(main) = program
        .functions
        .iter()
        .find(|function| function.name == "main")
        && main.params.is_empty()
        && main.result == IrType::Int
    {
        emit_windows_start_stub(&mut module, declarations["main"], main.span)?;
    }
    module
        .finish()
        .emit()
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])
}

/// Link a supported module into a standalone Windows console executable.
pub fn build_executable(checked: &CheckedModule, output: &Path) -> Result<(), Vec<Diagnostic>> {
    build_executable_program(ir::lower(checked)?, output)
}

/// Link all modules in a checked source graph into one executable.
pub fn build_executable_graph(
    checked: &CheckedProgram,
    output: &Path,
) -> Result<(), Vec<Diagnostic>> {
    build_executable_program(ir::lower_graph(checked)?, output)
}

fn build_executable_program(program: Program, output: &Path) -> Result<(), Vec<Diagnostic>> {
    let Some(main) = program
        .functions
        .iter()
        .find(|function| function.name == "main")
    else {
        return Err(vec![Diagnostic::new(
            "standalone build requires fn main() -> Int",
            Span::default(),
        )]);
    };
    if !main.params.is_empty() || main.result != IrType::Int {
        return Err(vec![Diagnostic::new(
            "standalone build requires fn main() -> Int",
            main.span,
        )]);
    }

    #[cfg(not(windows))]
    {
        let _ = (program, output);
        Err(vec![Diagnostic::new(
            "standalone executable linking is currently implemented only for Windows",
            main.span,
        )])
    }

    #[cfg(all(windows, not(target_arch = "x86_64")))]
    {
        let _ = (program, output);
        Err(vec![Diagnostic::new(
            "standalone executable linking is currently implemented only for Windows x64",
            main.span,
        )])
    }

    #[cfg(all(windows, target_arch = "x86_64"))]
    {
        let object = emit_object_program(program.clone())?;
        let (linker, kernel32) = find_windows_link_tools()
            .map_err(|message| vec![Diagnostic::new(message, main.span)])?;
        if let Some(parent) = output
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent).map_err(|error| {
                vec![Diagnostic::new(
                    format!("could not create output directory: {error}"),
                    main.span,
                )]
            })?;
        }
        let id = NEXT_TEMP_FILE.fetch_add(1, Ordering::Relaxed);
        let temporary_object =
            env::temp_dir().join(format!("tyvera-{}-{id}.obj", std::process::id()));
        fs::write(&temporary_object, object).map_err(|error| {
            vec![Diagnostic::new(
                format!("could not write temporary object file: {error}"),
                main.span,
            )]
        })?;

        let result = Command::new(&linker)
            .arg("/nologo")
            .arg("/SUBSYSTEM:CONSOLE")
            .arg("/ENTRY:tyvera_start")
            .arg("/NODEFAULTLIB")
            .arg("/MACHINE:X64")
            .arg(format!("/OUT:{}", output.display()))
            .arg(&temporary_object)
            .arg(&kernel32)
            .output();
        let _ = fs::remove_file(&temporary_object);
        let result = result.map_err(|error| {
            vec![Diagnostic::new(
                format!(
                    "could not start the MSVC linker '{}': {error}",
                    linker.display()
                ),
                main.span,
            )]
        })?;
        if !result.status.success() {
            return Err(vec![Diagnostic::new(
                format!(
                    "MSVC linker failed: {}",
                    String::from_utf8_lossy(&result.stderr).trim()
                ),
                main.span,
            )]);
        }
        Ok(())
    }
}

#[cfg(all(windows, target_arch = "x86_64"))]
fn find_windows_link_tools() -> Result<(PathBuf, PathBuf), String> {
    let program_files_x86 = env::var_os("ProgramFiles(x86)")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Program Files (x86)"));
    let vswhere = program_files_x86
        .join("Microsoft Visual Studio")
        .join("Installer")
        .join("vswhere.exe");
    if !vswhere.is_file() {
        return Err(format!(
            "Visual Studio linker discovery failed: '{}' was not found",
            vswhere.display()
        ));
    }
    let linker_output = Command::new(&vswhere)
        .args([
            "-latest",
            "-products",
            "*",
            "-find",
            r"VC\Tools\MSVC\*\bin\Hostx64\x64\link.exe",
        ])
        .output()
        .map_err(|error| format!("could not run vswhere.exe: {error}"))?;
    if !linker_output.status.success() {
        return Err("vswhere.exe could not locate the MSVC linker".to_owned());
    }
    let linker = String::from_utf8_lossy(&linker_output.stdout)
        .lines()
        .next()
        .map(PathBuf::from)
        .filter(|path| path.is_file())
        .ok_or_else(|| "MSVC link.exe was not found in Visual Studio Build Tools".to_owned())?;

    let sdk_lib = env::var_os("WindowsSdkDir")
        .map(PathBuf::from)
        .unwrap_or_else(|| program_files_x86.join(r"Windows Kits\10"))
        .join("Lib");
    let versions = fs::read_dir(&sdk_lib)
        .map_err(|error| format!("could not inspect Windows SDK libraries: {error}"))?;
    let mut candidates = versions
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let version = entry.file_name().to_string_lossy().to_string();
            let version_key = version
                .split('.')
                .map(str::parse::<u64>)
                .collect::<Result<Vec<_>, _>>()
                .ok()?;
            let library = entry.path().join(r"um\x64\kernel32.lib");
            library.is_file().then_some((version_key, library))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|left, right| left.0.cmp(&right.0));
    let kernel32 = candidates.pop().map(|(_, path)| path).ok_or_else(|| {
        format!(
            "kernel32.lib was not found under '{}' (install the Windows SDK with the MSVC Build Tools)",
            sdk_lib.display()
        )
    })?;
    Ok((linker, kernel32))
}

fn declare_functions<M: Module>(
    module: &mut M,
    program: &Program,
    linkage: Linkage,
) -> Result<HashMap<String, FuncId>, Vec<Diagnostic>> {
    let mut declarations = HashMap::new();
    for function in &program.functions {
        let mut signature = module.make_signature();
        for (name, ty) in &function.params {
            let Some(ty) = clif_type(*ty) else {
                return Err(vec![Diagnostic::new(
                    format!("unit parameter '{name}' is not supported by the native backend"),
                    function.span,
                )]);
            };
            signature.params.push(AbiParam::new(ty));
        }
        if function.result != IrType::Unit {
            signature.returns.push(AbiParam::new(
                clif_type(function.result).expect("non-unit scalar types have a Cranelift type"),
            ));
        }
        let id = module
            .declare_function(&backend_function_name(&function.name), linkage, &signature)
            .map_err(|error| vec![Diagnostic::new(error.to_string(), function.span)])?;
        declarations.insert(function.name.clone(), id);
    }
    Ok(declarations)
}

fn backend_function_name(name: &str) -> String {
    format!("__tyvera_fn_{name}")
}

#[cfg(all(windows, target_arch = "x86_64"))]
fn emit_windows_start_stub(
    module: &mut ObjectModule,
    main_id: FuncId,
    span: Span,
) -> Result<(), Vec<Diagnostic>> {
    let mut exit_signature = module.make_signature();
    exit_signature.params.push(AbiParam::new(types::I32));
    let exit_id = module
        .declare_function("ExitProcess", Linkage::Import, &exit_signature)
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])?;

    let start_signature = module.make_signature();
    let start_id = module
        .declare_function("tyvera_start", Linkage::Export, &start_signature)
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])?;
    let mut context = module.make_context();
    context.func.signature = start_signature;
    context.func.name = UserFuncName::user(0, start_id.as_u32());
    let mut builder_context = FunctionBuilderContext::new();
    {
        let mut builder = FunctionBuilder::new(&mut context.func, &mut builder_context);
        let entry = builder.create_block();
        builder.switch_to_block(entry);
        let main_ref = module.declare_func_in_func(main_id, builder.func);
        let main_call = builder.ins().call(main_ref, &[]);
        let result = builder.inst_results(main_call)[0];
        let exit_code = builder.ins().ireduce(types::I32, result);
        let exit_ref = module.declare_func_in_func(exit_id, builder.func);
        builder.ins().call(exit_ref, &[exit_code]);
        builder.ins().return_(&[]);
        builder.seal_all_blocks();
        builder.finalize(module.target_config());
    }
    context
        .verify(module.isa())
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])?;
    module
        .define_function(start_id, &mut context)
        .map_err(|error| vec![Diagnostic::new(error.to_string(), span)])?;
    module.clear_context(&mut context);
    Ok(())
}

fn compile_function<M: Module>(
    module: &mut M,
    function: &Function,
    declarations: &HashMap<String, FuncId>,
) -> Result<(), Vec<Diagnostic>> {
    let function_id = declarations[&function.name];
    let mut context = module.make_context();
    context.func.signature = module.make_signature();
    for (_, ty) in &function.params {
        let Some(ty) = clif_type(*ty) else {
            return Err(vec![Diagnostic::new(
                "unit parameters are not supported by the native backend",
                function.span,
            )]);
        };
        context.func.signature.params.push(AbiParam::new(ty));
    }
    if function.result != IrType::Unit {
        context
            .func
            .signature
            .returns
            .push(AbiParam::new(clif_type(function.result).unwrap()));
    }
    context.func.name = UserFuncName::user(0, function_id.as_u32());

    let mut builder_context = FunctionBuilderContext::new();
    {
        let mut builder = FunctionBuilder::new(&mut context.func, &mut builder_context);
        let entry = builder.create_block();
        builder.switch_to_block(entry);
        builder.append_block_params_for_function_params(entry);
        let parameters = builder.block_params(entry).to_vec();
        let mut compiler = FunctionCompiler {
            builder,
            module,
            declarations,
            scopes: vec![HashMap::new()],
        };
        for ((name, ty), value) in function.params.iter().zip(parameters) {
            let variable = compiler.new_variable(*ty, function.span)?;
            compiler.builder.def_var(variable, value);
            compiler
                .scopes
                .last_mut()
                .unwrap()
                .insert(name.clone(), LocalBinding { variable });
        }

        let falls_through = compiler.compile_block(&function.body)?;
        if falls_through {
            if function.result == IrType::Unit {
                compiler.builder.ins().return_(&[]);
            } else {
                return Err(vec![Diagnostic::new(
                    "native backend reached the end of a non-Unit function",
                    function.span,
                )]);
            }
        }
        compiler.builder.seal_all_blocks();
        compiler.builder.finalize(compiler.module.target_config());
    }
    context
        .verify(module.isa())
        .map_err(|error| vec![Diagnostic::new(error.to_string(), function.span)])?;
    module
        .define_function(function_id, &mut context)
        .map_err(|error| vec![Diagnostic::new(error.to_string(), function.span)])?;
    module.clear_context(&mut context);
    Ok(())
}

struct FunctionCompiler<'a, M: Module> {
    builder: FunctionBuilder<'a>,
    module: &'a mut M,
    declarations: &'a HashMap<String, FuncId>,
    scopes: Vec<HashMap<String, LocalBinding>>,
}

impl<M: Module> FunctionCompiler<'_, M> {
    fn new_variable(&mut self, ty: IrType, span: Span) -> Result<Variable, Vec<Diagnostic>> {
        let Some(clif_type) = clif_type(ty) else {
            return Err(vec![Diagnostic::new(
                "Unit values cannot be stored in local variables",
                span,
            )]);
        };
        let variable = self.builder.declare_var(clif_type);
        Ok(variable)
    }

    fn compile_block(&mut self, statements: &[Statement]) -> Result<bool, Vec<Diagnostic>> {
        self.scopes.push(HashMap::new());
        let mut falls_through = true;
        for statement in statements {
            if !falls_through {
                break;
            }
            falls_through = self.compile_statement(statement)?;
        }
        self.scopes.pop();
        Ok(falls_through)
    }

    fn compile_statement(&mut self, statement: &Statement) -> Result<bool, Vec<Diagnostic>> {
        match statement {
            Statement::Let {
                name,
                ty,
                value,
                span,
                ..
            } => {
                let value = self.compile_expression(value)?;
                let variable = self.new_variable(*ty, *span)?;
                self.builder.def_var(variable, value);
                self.scopes
                    .last_mut()
                    .unwrap()
                    .insert(name.clone(), LocalBinding { variable });
                Ok(true)
            }
            Statement::Assign { name, value, span } => {
                let binding = self.lookup(name).ok_or_else(|| {
                    vec![Diagnostic::new(
                        format!("native backend could not find local '{name}'"),
                        *span,
                    )]
                })?;
                let value = self.compile_expression(value)?;
                self.builder.def_var(binding.variable, value);
                Ok(true)
            }
            Statement::Return(value, span) => {
                let values = if let Some(value) = value {
                    vec![self.compile_expression(value)?]
                } else {
                    Vec::new()
                };
                self.builder.ins().return_(&values);
                let _ = span;
                Ok(false)
            }
            Statement::Expression(expression, _) => {
                self.compile_expression(expression)?;
                Ok(true)
            }
            Statement::If {
                condition,
                then_body,
                else_body,
                span,
            } => self.compile_if(condition, then_body, else_body, *span),
            Statement::While {
                condition,
                body,
                span,
            } => self.compile_while(condition, body, *span),
        }
    }

    fn compile_if(
        &mut self,
        condition: &Expr,
        then_body: &[Statement],
        else_body: &[Statement],
        span: Span,
    ) -> Result<bool, Vec<Diagnostic>> {
        let condition = self.compile_condition(condition)?;
        let then_block = self.builder.create_block();
        let else_block = self.builder.create_block();
        let both_return = block_returns(then_body) && block_returns(else_body);
        let join_block = (!both_return).then(|| self.builder.create_block());
        self.builder
            .ins()
            .brif(condition, then_block, &[], else_block, &[]);

        self.builder.switch_to_block(then_block);
        let then_falls = self.compile_block(then_body)?;
        if then_falls {
            if let Some(join_block) = join_block {
                self.builder.ins().jump(join_block, &[]);
            } else {
                return Err(vec![Diagnostic::new(
                    "internal control-flow lowering error",
                    span,
                )]);
            }
        }
        self.builder.seal_block(then_block);

        self.builder.switch_to_block(else_block);
        let else_falls = self.compile_block(else_body)?;
        if else_falls {
            if let Some(join_block) = join_block {
                self.builder.ins().jump(join_block, &[]);
            } else {
                return Err(vec![Diagnostic::new(
                    "internal control-flow lowering error",
                    span,
                )]);
            }
        }
        self.builder.seal_block(else_block);

        if let Some(join_block) = join_block {
            self.builder.switch_to_block(join_block);
            self.builder.seal_block(join_block);
            Ok(true)
        } else {
            Ok(false)
        }
    }

    fn compile_while(
        &mut self,
        condition: &Expr,
        body: &[Statement],
        span: Span,
    ) -> Result<bool, Vec<Diagnostic>> {
        let header = self.builder.create_block();
        let loop_body = self.builder.create_block();
        let exit = self.builder.create_block();
        self.builder.ins().jump(header, &[]);
        self.builder.switch_to_block(header);
        let condition = self.compile_condition(condition)?;
        self.builder
            .ins()
            .brif(condition, loop_body, &[], exit, &[]);
        self.builder.switch_to_block(loop_body);
        if self.compile_block(body)? {
            self.builder.ins().jump(header, &[]);
        }
        self.builder.seal_block(loop_body);
        self.builder.seal_block(header);
        self.builder.switch_to_block(exit);
        self.builder.seal_block(exit);
        let _ = span;
        Ok(true)
    }

    fn compile_condition(&mut self, expression: &Expr) -> Result<Value, Vec<Diagnostic>> {
        let value = self.compile_expression(expression)?;
        Ok(self.builder.ins().icmp_imm_s(IntCC::NotEqual, value, 0))
    }

    fn compile_expression(&mut self, expression: &Expr) -> Result<Value, Vec<Diagnostic>> {
        match expression {
            Expr::Integer(value, _) => Ok(self.builder.ins().iconst(types::I64, *value)),
            Expr::Float(value, _) => Ok(self.builder.ins().f64const(*value)),
            Expr::Boolean(value, _) => Ok(self.builder.ins().iconst(types::I8, i64::from(*value))),
            Expr::Name(name, _, span) => {
                let binding = self.lookup(name).ok_or_else(|| {
                    vec![Diagnostic::new(
                        format!("native backend could not find local '{name}'"),
                        *span,
                    )]
                })?;
                Ok(self.builder.use_var(binding.variable))
            }
            Expr::Unary {
                op,
                operand,
                ty,
                span,
            } => {
                let value = self.compile_expression(operand)?;
                match op {
                    crate::ast::UnaryOp::Negate => Ok(match ty {
                        IrType::Int => self.builder.ins().ineg(value),
                        IrType::Float => self.builder.ins().fneg(value),
                        _ => return Err(self.internal_error("invalid type for unary minus", *span)),
                    }),
                    crate::ast::UnaryOp::Not => {
                        let is_false = self.builder.ins().icmp_imm_s(IntCC::Equal, value, 0);
                        Ok(is_false)
                    }
                    _ => Err(self.internal_error("unsupported task operation in IR", *span)),
                }
            }
            Expr::Binary {
                left,
                op,
                right,
                ty,
                operand_ty,
                span,
            } => {
                let left_value = self.compile_expression(left)?;
                let right_value = self.compile_expression(right)?;
                self.compile_binary(left_value, *op, right_value, *ty, *operand_ty, *span)
            }
            Expr::Call {
                name,
                args,
                result,
                span,
            } => {
                let function_id = self.declarations.get(name).copied().ok_or_else(|| {
                    vec![Diagnostic::new(
                        format!("native function '{name}' was not declared"),
                        *span,
                    )]
                })?;
                let args = args
                    .iter()
                    .map(|arg| self.compile_expression(arg))
                    .collect::<Result<Vec<_>, _>>()?;
                let function_ref = self
                    .module
                    .declare_func_in_func(function_id, self.builder.func);
                let call = self.builder.ins().call(function_ref, &args);
                if *result == IrType::Unit {
                    Ok(self.builder.ins().iconst(types::I8, 0))
                } else {
                    self.builder
                        .inst_results(call)
                        .first()
                        .copied()
                        .ok_or_else(|| self.internal_error("native call has no result", *span))
                }
            }
        }
    }

    fn compile_binary(
        &mut self,
        left: Value,
        op: crate::ast::BinaryOp,
        right: Value,
        result_ty: IrType,
        operand_ty: IrType,
        span: Span,
    ) -> Result<Value, Vec<Diagnostic>> {
        use crate::ast::BinaryOp as Op;
        match op {
            Op::Add => Ok(if operand_ty == IrType::Float {
                self.builder.ins().fadd(left, right)
            } else {
                self.builder.ins().iadd(left, right)
            }),
            Op::Subtract => Ok(if operand_ty == IrType::Float {
                self.builder.ins().fsub(left, right)
            } else {
                self.builder.ins().isub(left, right)
            }),
            Op::Multiply => Ok(if operand_ty == IrType::Float {
                self.builder.ins().fmul(left, right)
            } else {
                self.builder.ins().imul(left, right)
            }),
            Op::Divide => Ok(if operand_ty == IrType::Float {
                self.builder.ins().fdiv(left, right)
            } else {
                self.builder.ins().sdiv(left, right)
            }),
            Op::Remainder if operand_ty == IrType::Int => Ok(self.builder.ins().srem(left, right)),
            Op::Remainder => Err(self.internal_error(
                "floating-point remainder is not supported by the native backend",
                span,
            )),
            Op::And => Ok(self.builder.ins().band(left, right)),
            Op::Or => Ok(self.builder.ins().bor(left, right)),
            Op::Equal
            | Op::NotEqual
            | Op::Less
            | Op::LessEqual
            | Op::Greater
            | Op::GreaterEqual => {
                let result = if operand_ty == IrType::Float {
                    let condition = match op {
                        Op::Equal => FloatCC::Equal,
                        Op::NotEqual => FloatCC::NotEqual,
                        Op::Less => FloatCC::LessThan,
                        Op::LessEqual => FloatCC::LessThanOrEqual,
                        Op::Greater => FloatCC::GreaterThan,
                        Op::GreaterEqual => FloatCC::GreaterThanOrEqual,
                        _ => unreachable!(),
                    };
                    self.builder.ins().fcmp(condition, left, right)
                } else {
                    let condition = match op {
                        Op::Equal => IntCC::Equal,
                        Op::NotEqual => IntCC::NotEqual,
                        Op::Less => IntCC::SignedLessThan,
                        Op::LessEqual => IntCC::SignedLessThanOrEqual,
                        Op::Greater => IntCC::SignedGreaterThan,
                        Op::GreaterEqual => IntCC::SignedGreaterThanOrEqual,
                        _ => unreachable!(),
                    };
                    self.builder.ins().icmp(condition, left, right)
                };
                if result_ty != IrType::Bool {
                    return Err(self.internal_error(
                        "native comparison does not have Boolean result type",
                        span,
                    ));
                }
                Ok(result)
            }
            Op::Assign => {
                Err(self.internal_error("assignment must be lowered as a statement", span))
            }
        }
    }

    fn lookup(&self, name: &str) -> Option<LocalBinding> {
        self.scopes
            .iter()
            .rev()
            .find_map(|scope| scope.get(name).copied())
    }

    fn internal_error(&self, message: &str, span: Span) -> Vec<Diagnostic> {
        vec![Diagnostic::new(message, span)]
    }
}

fn clif_type(ty: IrType) -> Option<Type> {
    match ty {
        IrType::Int => Some(types::I64),
        IrType::Float => Some(types::F64),
        IrType::Bool => Some(types::I8),
        IrType::Unit => None,
    }
}

fn block_returns(statements: &[Statement]) -> bool {
    statements.iter().any(statement_returns)
}

fn statement_returns(statement: &Statement) -> bool {
    match statement {
        Statement::Return(_, _) => true,
        Statement::If {
            then_body,
            else_body,
            ..
        } => block_returns(then_body) && block_returns(else_body),
        Statement::Let { .. } | Statement::Assign { .. } | Statement::Expression(..) => false,
        Statement::While { .. } => false,
    }
}

#[cfg(test)]
mod tests {
    use crate::parser::parse;
    use crate::typecheck::check;

    use super::{emit_object, run};

    #[test]
    fn jits_and_executes_an_integer_program_with_calls_and_branches() {
        let module = parse(
            r#"
                module arithmetic;
                fn double(value: Int) -> Int { return value * 2; }
                fn main() -> Int {
                    var total: Int = double(21);
                    if total == 42 { return total; } else { return 0; }
                }
            "#,
        )
        .unwrap();
        let checked = check(&module).unwrap();
        assert_eq!(run(&checked, "main").unwrap(), 42);
    }

    #[test]
    fn jits_loops_and_boolean_conditions() {
        let module = parse(
            r#"
                module loops;
                fn main() -> Int {
                    var counter: Int = 0;
                    while counter < 10 { counter = counter + 1; }
                    if counter == 10 { return counter; } else { return 0; }
                }
            "#,
        )
        .unwrap();
        let checked = check(&module).unwrap();
        assert_eq!(run(&checked, "main").unwrap(), 10);
    }

    #[test]
    fn jits_float_arithmetic_and_comparisons() {
        let module = parse(
            r#"
                module floats;
                fn twice(value: Float) -> Float { return value * 2.0; }
                fn main() -> Int {
                    let result: Float = twice(21.0);
                    if result == 42.0 { return 42; } else { return 0; }
                }
            "#,
        )
        .unwrap();
        let checked = check(&module).unwrap();
        assert_eq!(run(&checked, "main").unwrap(), 42);
    }

    #[cfg(all(windows, target_arch = "x86_64"))]
    #[test]
    fn emits_a_windows_coff_object_file() {
        let module = parse("module object_test; pub fn main() -> Int { return 42; }").unwrap();
        let checked = check(&module).unwrap();
        let object = emit_object(&checked).unwrap();
        assert!(object.len() > 64);
        assert_eq!(&object[..2], &[0x64, 0x86]);
    }
}
