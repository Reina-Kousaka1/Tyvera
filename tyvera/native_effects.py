"""Conservative execution effects for the private C++ bootstrap backend.

This is an allowlist over validated typed IR, not a new Tyvera type or promise
of parallel scheduling. Unknown constructs always retain serialized execution.
"""

from __future__ import annotations

from .ir import (
    IRAssign, IRBinary, IRBlock, IRCall, IRExpression, IRExpressionStatement,
    IRIf, IRLet, IRLiteral, IRLocalRef, IRLoopControl, IRModule, IRReturn,
    IRStatement, IRType, IRUnary, IRWhile,
)


_PRIVATE_VALUE_TYPES = frozenset({
    IRType.INT, IRType.FLOAT, IRType.BOOL, IRType.STRING, IRType.BYTE,
    IRType.DURATION, IRType.VOID,
})


def independent_scalar_functions(module: IRModule) -> frozenset[str]:
    """Prove which functions touch only private scalar values.

    Synchronous callees must already have the same proof. Recursive cycles,
    async calls, builtins, output, waits, handles and every aggregate are kept
    serialized. Adding an IR construct cannot silently widen this boundary.
    """
    proven: set[str] = set()
    functions = {function.symbol: function for function in module.functions}

    def expression(value: IRExpression) -> bool:
        if value.type not in _PRIVATE_VALUE_TYPES:
            return False
        if isinstance(value, (IRLiteral, IRLocalRef)):
            return True
        if isinstance(value, IRUnary):
            return expression(value.operand)
        if isinstance(value, IRBinary):
            return expression(value.left) and expression(value.right)
        if isinstance(value, IRCall):
            callee = functions.get(value.function)
            return (callee is not None and not callee.is_async
                    and value.function in proven
                    and all(expression(argument) for argument in value.arguments))
        return False

    def statement(value: IRStatement) -> bool:
        if isinstance(value, IRLet):
            return value.variable.type in _PRIVATE_VALUE_TYPES and expression(value.value)
        if isinstance(value, IRAssign):
            return expression(value.value)
        if isinstance(value, IRExpressionStatement):
            return expression(value.expression)
        if isinstance(value, IRReturn):
            return value.value is None or expression(value.value)
        if isinstance(value, IRBlock):
            return all(statement(item) for item in value.statements)
        if isinstance(value, IRIf):
            return (expression(value.condition)
                    and all(statement(item) for item in (*value.then_body, *value.else_body)))
        if isinstance(value, IRWhile):
            return expression(value.condition) and all(statement(item) for item in value.body)
        return isinstance(value, IRLoopControl)

    changed = True
    while changed:
        changed = False
        for function in module.functions:
            if function.symbol in proven or function.actor_name is not None:
                continue
            if (function.return_type in _PRIVATE_VALUE_TYPES
                    and all(parameter.type in _PRIVATE_VALUE_TYPES for parameter in function.parameters)
                    and all(statement(item) for item in function.body)):
                proven.add(function.symbol)
                changed = True
    return frozenset(proven)
