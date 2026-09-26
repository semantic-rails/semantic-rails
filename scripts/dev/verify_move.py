"""Prove that a change between two refs only moves code between ``semantic_rails`` modules.

Usage: python scripts/dev/verify_move.py <base-ref> [<head-ref>] [--repo DIR] [--allow-comment-edits]
(head defaults to the working tree, untracked files included). Exit 0 = a pure move, 1 = not.

Checks, over every changed ``semantic_rails/**.py`` file:
1. Every top-level statement other than an import (def, class, assignment, with decorators and
   attached comments) exists exactly once on each side with byte-identical text and an identical
   AST, except that each import nested inside it is compared as a placeholder naming what it
   binds (its position and count still count), and checked by 2.
2. Every name a statement reads from module scope (per Python's scoping rules), and every name a
   nested import binds, resolves to the same definition on both sides: a definition is identified
   by the module and name it had at base. No moved statement declares ``global``, reads or binds
   a name its old or new module declares ``global`` anywhere, and every moved public name, or
   name in its old module's ``__all__``, is still reachable from the old module.
3. The only new statements allowed are lazy forwarders,
   ``def f(*args, **kwargs): from <module> import f as fn; return fn(*args, **kwargs)``; a base
   forwarder may disappear only where the module now binds the definition it forwarded to.
4. No moved statement reads ``__file__``, ``__name__``, ``__package__``, ``__spec__``, ``globals``,
   ``vars`` or ``__import__``: their meaning depends on the module the code lives in.
5. The multiset of comment lines is unchanged (``--allow-comment-edits`` reports edits instead).
6. No test patches a moved name at its old path, and no moved statement reads a name that tests
   patch on its old module (the patch would stop intercepting that call).
7. Every ``from <changed module> import name`` in the package, tests and scripts still resolves to
   the same definition (a re-export ruff dropped, or a re-pointed importer, fails here).

Limits: it reads only this repository, so importers and patches elsewhere (other repos, string
paths) need their own check; a moved private name outside ``__all__`` may leave its old module
when nothing here imports it from there; top-level imports aren't compared as statements, and
names bound inside top-level ``if``/``try`` blocks aren't modelled.
"""

from __future__ import annotations

import argparse
import ast
import functools
import subprocess
import symtable
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

HAZARDS = {"__file__", "__name__", "__package__", "__spec__", "globals", "vars", "__import__"}


@dataclass(eq=False)
class Stmt:
    module: str
    name: str
    key: tuple[str, str, str]  # (kind:name, text, AST dump), nested imports as placeholders
    loads: set[str]  # names read from module scope
    declared_global: set[str]
    nested: list[tuple[str, str, str]]  # (local name, absolute module, imported name), in order
    forward_to: tuple[str, str] | None = None
    base_id: tuple[str, str] | None = None


def _module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    pkg = module.split(".") if is_package else module.split(".")[:-1]
    base = pkg[: len(pkg) - node.level + 1]
    return ".".join(base + ([node.module] if node.module else []))


def _scope_names(source: str) -> tuple[set[str], set[str]]:
    """Names a top-level statement reads from module scope, and names it declares ``global``."""
    reads: set[str] = set()
    declared: set[str] = set()

    def walk(table: symtable.SymbolTable, top: bool) -> None:
        for sym in table.get_symbols():
            if sym.is_declared_global():
                declared.add(sym.get_name())
            if sym.is_referenced() and (top or sym.is_global()):
                reads.add(sym.get_name())
        for child in table.get_children():
            walk(child, False)

    walk(symtable.symtable(source, "<statement>", "exec"), True)
    return reads, declared


def _placeholder(node: ast.Import | ast.ImportFrom) -> str:
    return "<import " + ", ".join(a.asname or a.name for a in node.names) + ">"


def _is_forwarder(node: ast.stmt) -> bool:
    if not isinstance(node, ast.FunctionDef) or len(node.body) != 2 or node.decorator_list:
        return False
    imp, ret = node.body
    return (
        isinstance(imp, ast.ImportFrom)
        and len(imp.names) == 1
        and imp.names[0].name == node.name
        and ast.unparse(node.args) == "*args, **kwargs"
        and isinstance(ret, ast.Return)
        and ast.unparse(ret.value) == f"{imp.names[0].asname or node.name}(*args, **kwargs)"
    )


@dataclass
class Module:
    stmts: list[Stmt]
    binds: dict[str, tuple]
    comments: list[str]
    exports: set[str]  # names in ``__all__``
    rebound: set[str]  # names declared ``global`` anywhere in the module


_IMPORTS = (ast.Import, ast.ImportFrom)


class _Placeholders(ast.NodeTransformer):
    """Replace each nested import with a placeholder that keeps its position and bound names."""

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> ast.Expr:
        return ast.Expr(ast.Constant(_placeholder(node)))

    visit_ImportFrom = visit_Import


def parse_module(path: str, text: str) -> Module:
    """Statements, top-level bindings, comment lines, ``__all__`` and rebound globals of a module."""
    module, is_package = _module_name(path), path.endswith("__init__.py")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    stmts: list[Stmt] = []
    binds: dict[str, tuple] = {}
    exports: set[str] = set()
    rebound = {n for sub in ast.walk(tree) if isinstance(sub, ast.Global) for n in sub.names}
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                binds[a.asname or a.name] = ("imp", _absolute(node, module, is_package), a.name)
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                binds[a.asname or a.name.split(".")[0]] = ("ext", a.name, "")
            continue
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        name = getattr(node, "name", None) or ast.unparse(node).split("=")[0].split(":")[0].strip()
        for target in (
            [node]
            if hasattr(node, "name")
            else getattr(node, "targets", [getattr(node, "target", None)])
        ):
            bound = getattr(target, "name", None) or getattr(target, "id", None)
            if bound:
                binds[bound] = ("def", module, bound)
            if bound == "__all__":
                exports = set(ast.literal_eval(node.value))
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        forward_to = None
        if _is_forwarder(node):
            imp = node.body[0]
            forward_to = (_absolute(imp, module, is_package), node.name)
        source = "".join(lines[start - 1 : node.end_lineno])
        loads, declared = _scope_names(source)
        nested: list[tuple[str, str, str]] = []
        imports = [] if forward_to else [n for n in ast.walk(node) if isinstance(n, _IMPORTS)]
        offsets = [0]
        for ln in lines[start - 1 : node.end_lineno]:
            offsets.append(offsets[-1] + len(ln))
        for sub in sorted(imports, key=lambda n: (n.lineno, n.col_offset), reverse=True):
            for a in reversed(sub.names):
                target = (a.name, "")
                if isinstance(sub, ast.ImportFrom):
                    target = (_absolute(sub, module, is_package), a.name)
                nested.insert(0, (a.asname or a.name.split(".")[0], *target))
            lo = offsets[sub.lineno - start] + sub.col_offset
            hi = offsets[sub.end_lineno - start] + (sub.end_col_offset or 0)
            source = source[:lo] + _placeholder(sub) + source[hi:]
        dump = ast.dump(_Placeholders().visit(node))
        stmts.append(
            Stmt(
                module,
                name,
                (f"{type(node).__name__}:{name}", source, dump),
                loads,
                declared,
                nested,
                forward_to,
            )
        )
    comments = [ln.strip() for ln in lines if ln.strip().startswith("#")]
    return Module(stmts, binds, comments, exports, rebound)


class Side:
    """One side of the change: statements of the changed files, bindings of every module."""

    def __init__(self, files: dict[str, str], changed: set[str]):
        self.stmts: list[Stmt] = []
        self.binds: dict[str, dict[str, tuple]] = {}
        self.changed_modules = {_module_name(p) for p in changed if p in files}
        self.comments: Counter[str] = Counter()
        self.defs: dict[tuple[str, str], Stmt] = {}
        self.modules: dict[str, Module] = {}
        for path, text in files.items():
            parsed = self.modules[_module_name(path)] = parse_module(path, text)
            self.binds[_module_name(path)] = parsed.binds
            for st in parsed.stmts:
                self.defs[(st.module, st.name)] = st
            if path in changed:
                self.stmts += parsed.stmts
                self.comments.update(parsed.comments)

    def resolve(self, module: str, name: str, depth: int = 0) -> tuple:
        bound = self.binds.get(module, {}).get(name)
        if bound is None:
            if f"{module}.{name}" in self.binds:
                return ("module", f"{module}.{name}")
            return ("unbound", name) if module in self.binds else ("ext", module, name)
        kind, mod, nm = bound
        if kind == "def":
            st = self.defs.get((mod, nm))
            if st is not None and st.forward_to and depth < 10:
                return self.resolve(*st.forward_to, depth + 1)
            kind_ = st.key[0].split(":")[0] if st is not None else ""
            return ("def", *(st.base_id if st is not None and st.base_id else (mod, nm)), kind_)
        if kind == "imp" and depth < 10:
            return self.resolve(mod, nm, depth + 1)
        return ("ext", mod, nm)


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout


def _changed(repo: str, base: str, head: str | None) -> list[str]:
    git = functools.partial(_git, repo)
    paths = git(
        "diff",
        "--name-only",
        "--no-renames",
        base,
        *([head] if head else []),
        "--",
        "semantic_rails",
    ).split()
    if not head:
        paths += git("ls-files", "--others", "--exclude-standard", "semantic_rails").split()
    return sorted({p for p in paths if p.endswith(".py")})


@functools.cache
def _files(repo: str, ref: str | None, root: str = "") -> dict[str, str]:
    """Every .py file under ``semantic_rails``, ``tests`` and ``scripts`` (or only ``root``) at
    ``ref``, or in the working tree when ``ref`` is None; a ref is read in one ``git cat-file``."""
    if root:
        return {p: t for p, t in _files(repo, ref).items() if p.startswith(root)}
    roots = ("semantic_rails", "tests", "scripts")
    if ref is None:
        return {
            str(p.relative_to(repo)): p.read_text()
            for r in roots
            for p in sorted(Path(repo, r).rglob("*.py"))
        }
    listing = _git(repo, "ls-tree", "-r", ref, *roots).splitlines()
    blobs = [(line.split("\t", 1)[1], line.split()[2]) for line in listing if line.endswith(".py")]
    out = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch"],
        input="".join(f"{sha}\n" for _, sha in blobs).encode(),
        capture_output=True,
        check=True,
    ).stdout
    files, pos = {}, 0
    for path, _ in blobs:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        files[path] = out[header_end + 1 : header_end + 1 + size].decode()
        pos = header_end + 1 + size + 1
    return files


def _patched(repo: str, head: str | None, modules: set[str]) -> dict[tuple[str, str], str]:
    """(module, name) -> first test location that patches it, for modules among ``modules``."""
    out: dict[tuple[str, str], str] = {}
    for rel, text in sorted(_files(repo, head, "tests/").items()):
        tree = ast.parse(text)
        alias = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                alias.update(
                    {a.asname: a.name for a in node.names if a.asname and a.name in modules}
                )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
                "semantic_rails"
            ):
                alias.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and ("setattr" in ast.unparse(node.func) or "patch" in ast.unparse(node.func))
            ):
                continue
            args = node.args
            target = None
            if (
                len(args) >= 2
                and isinstance(args[1], ast.Constant)
                and isinstance(args[1].value, str)
            ):
                obj = ast.unparse(args[0])
                target = (alias.get(obj, obj), args[1].value)
            elif args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                target = tuple(args[0].value.rsplit(".", 1)) if "." in args[0].value else None
            if target and target[0] in modules:
                out.setdefault(target, f"{rel}:{node.lineno}")
    return out


def verify(
    repo: str, base: str, head: str | None = None, allow_comment_edits: bool = False
) -> list[str]:
    files = _changed(repo, base, head)
    base_side = Side(_files(repo, base, "semantic_rails/"), set(files))
    head_side = Side(_files(repo, head, "semantic_rails/"), set(files))
    problems: list[str] = []
    # 1 + 3: pair statements by content; forwarders are handled separately
    pool: dict[tuple, list[Stmt]] = defaultdict(list)
    for st in base_side.stmts:
        pool[st.key].append(st)
    pairs: list[tuple[Stmt, Stmt]] = []
    unmatched_head: list[Stmt] = []
    # head module -> base modules its code came from
    origins: dict[str, Counter[str]] = defaultdict(Counter)

    def pair(st: Stmt, pick: Stmt) -> None:
        pool[st.key].remove(pick)
        st.base_id = (pick.module, pick.name)
        origins[st.module][pick.module] += 1
        pairs.append((pick, st))

    later = []
    # unique content first, then duplicates by where their neighbours came from
    for st in head_side.stmts:
        candidates = pool.get(st.key) or []
        if len(candidates) == 1:
            pair(st, candidates[0])
        elif candidates:
            later.append(st)
        else:
            unmatched_head.append(st)
    for st in later:
        candidates = pool.get(st.key) or []
        if not candidates:
            unmatched_head.append(st)
            continue
        same = [b for b in candidates if b.module == st.module]
        pair(st, same[0] if same else max(candidates, key=lambda b: origins[st.module][b.module]))
    for st in unmatched_head:
        if st.forward_to:
            target = head_side.resolve(*st.forward_to)
            if target[0] != "def" or target[3] not in ("FunctionDef", "AsyncFunctionDef"):
                problems.append(
                    f"forwarder {st.module}.{st.name} doesn't reach a definition: {target}"
                )
        else:
            problems.append(f"new or changed statement: {st.module}.{st.name}")
    for st in [b for group in pool.values() for b in group]:
        # a base forwarder may go only where its module now binds the same definition
        if st.forward_to:
            if base_side.resolve(*st.forward_to) != head_side.resolve(st.module, st.name):
                problems.append(
                    f"forwarder {st.module}.{st.name} was removed without its definition taking its place"
                )
        else:
            problems.append(f"missing or changed statement: {st.module}.{st.name}")
    # 2 + 4: names resolve to the same definitions; moved code doesn't depend on its module
    for b, h in pairs:
        for name in sorted(b.loads | h.loads):
            before, after = base_side.resolve(b.module, name), head_side.resolve(h.module, name)
            if before != after:
                problems.append(
                    f"{h.module}.{h.name}: '{name}' resolved to {before[1:]}, now {after[1:]}"
                )
        # identical placeholders: the same imports in the same places, binding the same names
        for (local, *was), (_, *now) in zip(b.nested, h.nested, strict=True):
            before, after = base_side.resolve(*was), head_side.resolve(*now)
            if before != after:
                problems.append(
                    f"{h.module}.{h.name}: lazy import '{local}' resolved to {before}, now {after}"
                )
        if (
            b.forward_to
            and h.forward_to
            and base_side.resolve(*b.forward_to) != head_side.resolve(*h.forward_to)
        ):
            problems.append(f"{h.module}.{h.name} now forwards to a different definition")
        if b.module == h.module:
            continue
        if b.loads & HAZARDS:
            problems.append(f"{b.module}.{b.name} moved but reads {sorted(b.loads & HAZARDS)}")
        rebound = base_side.modules[b.module].rebound | head_side.modules[h.module].rebound
        if b.declared_global or (b.loads | {b.name}) & rebound:
            problems.append(
                f"{b.module}.{b.name} moved but declares, reads or binds a global that its module"
                f" rebinds: {sorted(b.declared_global | ((b.loads | {b.name}) & rebound))}"
            )
        public = not b.name.startswith("_") or b.name in base_side.modules[b.module].exports
        if public and head_side.resolve(b.module, b.name)[:3] != ("def", b.module, b.name):
            problems.append(f"{b.name} moved to {h.module} but {b.module} no longer provides it")
    # 5: comments
    edits = [f"-{c}" for c in base_side.comments - head_side.comments] + [
        f"+{c}" for c in head_side.comments - base_side.comments
    ]
    if edits:
        (print if allow_comment_edits else problems.append)(f"comment edits: {edits}")
    # 6: test patches
    moved = [(b, h) for b, h in pairs if b.module != h.module]
    patched = _patched(repo, head, {b.module for b, _ in moved})
    for b, h in moved:
        if (b.module, b.name) in patched:
            problems.append(
                f"{patched[(b.module, b.name)]} patches {b.module}.{b.name}, which moved to {h.module}"
            )
        for name in sorted(b.loads):
            if (b.module, name) in patched:
                problems.append(
                    f"{b.name} moved to {h.module} but reads '{name}', which "
                    f"{patched[(b.module, name)]} patches on {b.module}"
                )
    # 7: every import of a changed module that exists on both sides still resolves the same
    changed_modules = base_side.changed_modules | head_side.changed_modules

    def import_sites(ref: str | None) -> set[tuple[str, str, str]]:
        sites = set()
        for rel, text in _files(repo, ref).items():
            module, is_package = _module_name(rel), rel.endswith("__init__.py")
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom):
                    target = _absolute(node, module, is_package)
                    if target in changed_modules and target != module:
                        sites |= {(rel, target, a.name) for a in node.names}
        return sites

    for rel, target, name in sorted(import_sites(base) & import_sites(head)):
        before, after = base_side.resolve(target, name), head_side.resolve(target, name)
        if before != after:
            problems.append(f"{rel} imports {target}.{name}: was {before}, now {after}")
    moved_lines = sum(b.key[1].count("\n") for b, _ in moved)
    print(
        f"{len(files)} files; {len(pairs)} statements matched; {len(moved)} moved ({moved_lines} lines)"
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("head", nargs="?")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--allow-comment-edits", action="store_true")
    args = parser.parse_args(argv)
    problems = verify(args.repo, args.base, args.head, args.allow_comment_edits)
    for problem in problems:
        print(f"FAIL: {problem}")
    print("not a pure move" if problems else "OK: a pure move")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
