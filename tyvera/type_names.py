from __future__ import annotations


def split_type(name: str) -> tuple[str, tuple[str, ...]]:
    """Split the compact source representation ``Name<A,B>`` recursively."""
    opening = name.find("<")
    if opening < 0:
        return name, ()
    base = name[:opening]
    if not name.endswith(">"):
        return name, ()
    inner = name[opening + 1:-1]
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(inner):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(inner[start:index])
            start = index + 1
    parts.append(inner[start:])
    return base, tuple(parts)


def make_type(base: str, arguments: tuple[str, ...] | list[str]) -> str:
    if not arguments:
        return base
    return f"{base}<{','.join(arguments)}>"


def substitute_type(name: str, substitutions: dict[str, str]) -> str:
    if name in substitutions:
        return substitutions[name]
    base, arguments = split_type(name)
    if not arguments:
        return name
    return make_type(base, [substitute_type(argument, substitutions) for argument in arguments])
