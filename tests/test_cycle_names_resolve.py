"""Every name the cycle reads must actually exist by the time it runs.

The defect: `cand_path` was bound near the top of auto_paper_cycle.main() by the candidates
opener. When that path was deleted the binding went with it, but the final summary line kept
the reference. Python resolves names at RUNTIME, so nothing failed at import, nothing failed
in review, and the module compiled cleanly — the cycle raised NameError on its very last
statement, after every open, mark, close and prediction had already been written and after the
`finally` had released the lock. Six consecutive runs reported exit 1 to Task Scheduler while
having actually done their work.

That is the worst possible shape for a failure on this system: the postmortems for the stalled
re-mark loop turned on being able to tell a run that died from a run that finished, and the
exit code was lying in both directions.

A syntax check cannot catch this and neither can an import. This walks the AST and asserts
that every name LOADED is one that will exist — a local, a parameter, a name from an enclosing
function, a module global, or a builtin. Same move as analysis/contracts: catch the class at
the seam rather than patching the instance.

Scope is modelled properly rather than flattened. Nested functions, lambdas and comprehensions
each get their own scope chained to their parent, because collapsing them produces false
positives on every closure in the file — and a check that cries wolf gets deleted, which is
worse than not having it.
"""
import ast
import builtins
import pathlib

import pytest

MODULES = ["auto_paper_cycle.py", "multi_strategy.py", "analysis/shadow_book.py",
           "analysis/outcome_logger.py"]
BASE = pathlib.Path(__file__).resolve().parent.parent

SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
               ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _bound_by(target) -> set:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _own_bindings(scope) -> set:
    """Names bound directly in this scope, NOT descending into nested scopes."""
    bound = set()

    args = getattr(scope, "args", None)
    if isinstance(args, ast.arguments):
        bound |= {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    for gen in getattr(scope, "generators", []):
        bound |= _bound_by(gen.target)

    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                bound |= _bound_by(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            bound |= _bound_by(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound |= _bound_by(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bound |= _bound_by(node.optional_vars)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound |= set(node.names)
    return bound


def _own_nodes(scope):
    """Every node belonging to THIS scope, stopping at the boundary of any nested one.

    A nested scope node is yielded (so its name is bound here and it can be recursed into),
    but its body is not descended into — that body belongs to the nested scope, and walking it
    from here is what makes a closure's parameters look undefined.
    """
    if isinstance(scope, ast.Module):
        stack = list(scope.body)
    elif isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        stack = list(scope.body)
    elif isinstance(scope, ast.Lambda):
        stack = [scope.body]
    elif isinstance(scope, ast.DictComp):
        stack = [scope.key, scope.value]
        for gen in scope.generators:
            stack.extend(gen.ifs + [gen.iter])
    else:                                   # list/set/generator comprehension
        stack = [scope.elt]
        for gen in scope.generators:
            stack.extend(gen.ifs + [gen.iter])

    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPE_NODES):
            continue                        # its body belongs to its own scope
        stack.extend(ast.iter_child_nodes(node))


def _child_scopes(scope):
    """Nested scopes declared directly inside this one."""
    for node in _own_nodes(scope):
        if isinstance(node, SCOPE_NODES):
            yield node


def _loads(scope):
    """(name, lineno) for every name READ directly in this scope."""
    for node in _own_nodes(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            yield node.id, node.lineno


def _check(scope, available: set, label: str, problems: list) -> None:
    own = available | _own_bindings(scope)
    for name, lineno in _loads(scope):
        if name not in own:
            problems.append(f"{label}:{lineno} reads undefined name {name!r}")
    for child in _child_scopes(scope):
        child_label = f"{label}::{getattr(child, 'name', type(child).__name__)}"
        _check(child, own, child_label, problems)


# Always present in an executing module, never assigned in the source.
MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                  "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(source: str, label: str = "<src>") -> list:
    tree = ast.parse(source)
    problems: list = []
    _check(tree, set(dir(builtins)) | MODULE_DUNDERS, label, problems)
    return sorted(set(problems))


@pytest.mark.parametrize("relpath", MODULES)
def test_no_function_reads_a_name_that_does_not_exist(relpath):
    path = BASE / relpath
    problems = _undefined_names(path.read_text(encoding="utf-8"), relpath)
    assert not problems, "\n  " + "\n  ".join(problems)


def test_the_detector_actually_catches_the_bug_it_was_written_for():
    """The check must be able to FAIL, or it proves nothing. This is the shipped defect."""
    problems = _undefined_names(
        "def main():\n"
        "    opened = 1\n"
        "    print(f'summary: opened={opened} snapshot={cand_path.name}')\n")
    assert any("cand_path" in p for p in problems)
    assert not any("opened" in p for p in problems), "a bound name must not be flagged"


def test_the_detector_does_not_cry_wolf_on_closures():
    """Closures, comprehensions and lambdas are the false-positive traps that get a check like
    this deleted. A name from an enclosing scope is defined, and must not be reported."""
    problems = _undefined_names(
        "import os\n"
        "TOP = 1\n"
        "def outer(rows):\n"
        "    factor = 2\n"
        "    def inner(row):\n"
        "        return row * factor + TOP\n"
        "    doubled = [inner(r) for r in rows if r]\n"
        "    keyed = {k: v for k, v in zip(rows, doubled)}\n"
        "    pick = lambda a, b=factor: a + b\n"
        "    with open(os.devnull) as fh:\n"
        "        fh.read()\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        print(exc)\n"
        "    return doubled, keyed, pick\n")
    assert problems == [], problems
