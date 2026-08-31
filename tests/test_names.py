"""Names read but never bound, anywhere in the package.

Written after two of them shipped. Removing the API-key panel took
`/api/estimate` with it; removing the key panel took the `UPLOADS` registry.
Each sat beside the code being cut, neither broke an import or a start-up, and
both reached the deployed service — one surfacing as a JavaScript parse error in
a browser, the other as an upload returning 500.

Python looks globals up when the line runs, so nothing catches this earlier. A
linter would; there is none installed here, and this is the part of one that
would have caught both.

    python3 -m tests.test_names
"""

import ast
import builtins
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "ai_peer_reviewer"

#: Reported per name, not per use, so one missing global is one line.
Finding = tuple[str, int, str]


def _bound_by(node: ast.AST) -> set[str]:
    """Names this node binds in the scope it belongs to."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return {node.id}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, ast.arg):
        return {node.arg}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return set(node.names)
    if isinstance(node, ast.alias):
        return {(node.asname or node.name).split(".")[0]}
    return set()


def _scope(node: ast.AST, inherited: set[str], found: list[Finding], where: str) -> None:
    """Check one scope, then recurse into the scopes it contains.

    Everything a scope binds counts as available throughout it, however far down
    the assignment sits — which is how Python behaves for module globals, and
    close enough for a function that a false positive here would be a genuine
    oddity worth looking at anyway.
    """
    bound = set(inherited)
    inner: list[ast.AST] = []

    for child in ast.iter_child_nodes(node):
        for sub in ast.walk(child):
            bound |= _bound_by(sub)
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                inner.append(sub)

    for child in ast.iter_child_nodes(node):
        for sub in ast.walk(child):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in bound:
                    found.append((where, sub.lineno, sub.id))

    for scope in inner:
        _scope(scope, bound, found, where)


def check(path: Path) -> list[Finding]:
    tree = ast.parse(path.read_text())
    found: list[Finding] = []
    _scope(tree, set(dir(builtins)) | {"__name__", "__file__", "__doc__"},
           found, path.name)
    # One line per name: a global that vanished is read from many places.
    seen, unique = set(), []
    for where, line, name in sorted(found, key=lambda f: f[1]):
        if (where, name) in seen:
            continue
        seen.add((where, name))
        unique.append((where, line, name))
    return unique


def main() -> int:
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        problems += check(path)
    for where, line, name in problems:
        print(f"  {where}:{line}: {name} is read and never bound")
    print(f"  {len(problems)} undefined name(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
