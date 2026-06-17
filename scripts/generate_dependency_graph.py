#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INCLUDE_ROOTS = ("src", "domain", "scripts", "tests")
PRODUCTION_ROOTS = ("src", "domain", "scripts")
APPLICATION_SUBPACKAGES = {
    "research",
    "inbound",
    "ledger",
    "multi_tick",
    "positions",
    "settings",
    "setup",
    "trades",
}
DOMAIN_SUBPACKAGES = {"engine", "ledger"}


@dataclass(frozen=True)
class DependencyEdge:
    importer: str
    dependency: str


@dataclass(frozen=True)
class BoundaryViolation:
    importer: str
    dependency: str
    reason: str


@dataclass(frozen=True)
class DependencyGraph:
    modules: dict[str, Path]
    edges: list[DependencyEdge]
    parse_errors: list[tuple[str, str]]
    module_sccs: list[list[str]]
    package_sccs: list[list[str]]
    boundary_violations: list[BoundaryViolation]

    @property
    def production_edges(self) -> list[DependencyEdge]:
        return [edge for edge in self.edges if not edge.importer.startswith("tests")]


def repo_base() -> Path:
    return Path(__file__).resolve().parents[1]


def module_from_path(path: Path) -> str:
    rel = path.with_suffix("").as_posix()
    if rel.endswith("/__init__"):
        rel = rel[:-9]
    return rel.replace("/", ".")


def collect_modules(base: Path, include_roots: tuple[str, ...] = DEFAULT_INCLUDE_ROOTS) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for root in include_roots:
        for path in sorted((base / root).rglob("*.py")):
            modules[module_from_path(path.relative_to(base))] = path
    return modules


def resolve_internal_imports(importer: str, node: ast.AST, module_names: set[str]) -> list[str]:
    def is_internal(name: str) -> bool:
        return name in module_names or any(module.startswith(name + ".") for module in module_names)

    def import_package_parts() -> list[str]:
        if any(module.startswith(importer + ".") for module in module_names):
            return importer.split(".")
        return importer.split(".")[:-1]

    out: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            for length in range(len(parts), 0, -1):
                candidate = ".".join(parts[:length])
                if is_internal(candidate):
                    out.append(candidate)
                    break
        return out

    if not isinstance(node, ast.ImportFrom):
        return out

    level = int(node.level or 0)
    imported_module = node.module or ""
    if level:
        package_parts = import_package_parts()
        base = ".".join(package_parts[: len(package_parts) - level + 1]) if level <= len(package_parts) + 1 else ""
        base_name = ".".join(part for part in (base, imported_module) if part)
    else:
        base_name = imported_module
    if not base_name:
        return out

    candidates: list[str] = []
    if is_internal(base_name):
        candidates.append(base_name)
    for alias in node.names:
        candidate = f"{base_name}.{alias.name}"
        if is_internal(candidate):
            candidates.append(candidate)
    return candidates or ([base_name] if is_internal(base_name) else [])


def layer_name(module: str) -> str:
    if module.startswith("src.interfaces"):
        return "interfaces"
    if module.startswith("src.application"):
        return "application"
    if module.startswith("src.infrastructure"):
        return "infrastructure"
    if module.startswith("domain.domain"):
        return "domain"
    if module.startswith("domain.storage"):
        return "storage"
    if module.startswith("domain.services"):
        return "domain_services"
    if module.startswith("scripts"):
        return "scripts"
    if module.startswith("tests"):
        return "tests"
    return module.split(".")[0]


def package_group(module: str) -> str:
    parts = module.split(".")
    if parts[0] == "src":
        if len(parts) >= 3 and parts[1] == "application":
            return ".".join(parts[:3]) if parts[2] in APPLICATION_SUBPACKAGES else "src.application"
        return ".".join(parts[:2])
    if parts[0] == "domain":
        if len(parts) >= 3 and parts[1] == "domain":
            return ".".join(parts[:3]) if parts[2] in DOMAIN_SUBPACKAGES else "domain.domain"
        return ".".join(parts[:2])
    if parts[0] == "tests":
        return "tests"
    if parts[0] == "scripts":
        return "scripts"
    return parts[0]


def strongly_connected(nodes: set[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    graph: dict[str, set[str]] = {node: set() for node in nodes}
    for src, dst in edges:
        if src in graph and dst in graph:
            graph[src].add(dst)

    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = lowlink[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for nxt in graph[node]:
            if nxt not in index:
                visit(nxt)
                lowlink[node] = min(lowlink[node], lowlink[nxt])
            elif nxt in on_stack:
                lowlink[node] = min(lowlink[node], index[nxt])

        if lowlink[node] != index[node]:
            return

        component: list[str] = []
        while True:
            item = stack.pop()
            on_stack.remove(item)
            component.append(item)
            if item == node:
                break
        if len(component) > 1:
            components.append(sorted(component))

    for node in sorted(nodes):
        if node not in index:
            visit(node)

    return sorted(components, key=lambda component: (-len(component), component[0]))


def boundary_violations(edges: list[DependencyEdge]) -> list[BoundaryViolation]:
    violations: list[BoundaryViolation] = []
    for edge in edges:
        importer = edge.importer
        dependency = edge.dependency
        if importer.startswith("domain.domain") and (dependency.startswith("src.") or dependency.startswith("scripts.")):
            violations.append(BoundaryViolation(importer, dependency, "domain.domain imports src/scripts"))
        if importer.startswith("src.application") and dependency.startswith("scripts."):
            violations.append(BoundaryViolation(importer, dependency, "src.application imports scripts"))
    return violations


def analyze_repo(base: Path) -> DependencyGraph:
    modules = collect_modules(base)
    module_names = set(modules)
    edges: list[DependencyEdge] = []
    parse_errors: list[tuple[str, str]] = []

    for module, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            parse_errors.append((str(path.relative_to(base)), f"{type(exc).__name__}: {exc}"))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for dependency in resolve_internal_imports(module, node, module_names):
                if dependency != module:
                    edges.append(DependencyEdge(module, dependency))

    production_modules = {
        module
        for module in modules
        if any(module == root or module.startswith(root + ".") for root in PRODUCTION_ROOTS)
    }
    production_edges = [(edge.importer, edge.dependency) for edge in edges if edge.importer in production_modules]
    module_sccs = strongly_connected(production_modules, production_edges)

    package_edges: set[tuple[str, str]] = set()
    package_nodes: set[str] = set()
    for src, dst in production_edges:
        grouped_src = package_group(src)
        grouped_dst = package_group(dst)
        if grouped_src == grouped_dst:
            continue
        package_edges.add((grouped_src, grouped_dst))
        package_nodes.add(grouped_src)
        package_nodes.add(grouped_dst)
    package_sccs = strongly_connected(package_nodes, sorted(package_edges))

    return DependencyGraph(
        modules=modules,
        edges=edges,
        parse_errors=parse_errors,
        module_sccs=module_sccs,
        package_sccs=package_sccs,
        boundary_violations=boundary_violations([edge for edge in edges if not edge.importer.startswith("tests")]),
    )


def markdown_table(rows: list[tuple[object, ...]], headers: tuple[str, ...]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines)


def mermaid_id(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


def render_layer_mermaid(layer_edges: Counter[tuple[str, str]]) -> str:
    layer_order = ["tests", "scripts", "interfaces", "application", "infrastructure", "domain_services", "domain", "storage"]
    labels = {
        "tests": "tests",
        "scripts": "scripts",
        "interfaces": "src.interfaces",
        "application": "src.application",
        "infrastructure": "src.infrastructure",
        "domain_services": "domain.services",
        "domain": "domain.domain",
        "storage": "domain.storage",
    }
    present = {src for src, _dst in layer_edges} | {dst for _src, dst in layer_edges}
    lines = ["flowchart LR"]
    for node in layer_order:
        if node in present:
            lines.append(f'  {node}["{labels[node]}"]')
    for (src, dst), count in sorted(layer_edges.items(), key=lambda item: (item[0][0], item[0][1])):
        lines.append(f"  {src} -->|{count}| {dst}")
    return "\n".join(lines)


def render_package_mermaid(package_edges: Counter[tuple[str, str]]) -> str:
    lines = ["flowchart LR"]
    for (src, dst), count in sorted(package_edges.items(), key=lambda item: (-item[1], item[0][0], item[0][1])):
        lines.append(f'  {mermaid_id(src)}["{src}"] -->|{count}| {mermaid_id(dst)}["{dst}"]')
    return "\n".join(lines) + "\n"


def render_sccs(items: list[list[str]], *, noun: str) -> str:
    if not items:
        return f"- No production {noun} cycles detected."
    return "\n".join(
        f"- {len(component)} {noun}s: " + ", ".join(f"`{item}`" for item in component)
        for component in items
    )


def render_boundary_violations(violations: list[BoundaryViolation]) -> str:
    if not violations:
        return "No forbidden imports found for `domain.domain -> src/scripts` or `src.application -> scripts`."
    return "\n".join(
        f"- `{item.importer}` -> `{item.dependency}`: {item.reason}"
        for item in violations
    )


def build_outputs(graph: DependencyGraph) -> tuple[str, str]:
    file_counts = Counter(path.relative_to(repo_base()).parts[0] for path in graph.modules.values())
    production_edges = graph.production_edges
    layer_edges = Counter(
        (layer_name(edge.importer), layer_name(edge.dependency))
        for edge in graph.edges
        if layer_name(edge.importer) != layer_name(edge.dependency)
    )
    production_layer_edges = Counter(
        (layer_name(edge.importer), layer_name(edge.dependency))
        for edge in production_edges
        if layer_name(edge.importer) != layer_name(edge.dependency)
    )
    package_edges = Counter(
        (package_group(edge.importer), package_group(edge.dependency))
        for edge in production_edges
        if package_group(edge.importer) != package_group(edge.dependency)
    )
    incoming = Counter(edge.dependency for edge in production_edges)
    outgoing = Counter(edge.importer for edge in production_edges)

    layer_rows = [(src, dst, count) for (src, dst), count in production_layer_edges.most_common()]
    test_layer_rows = [(src, dst, count) for (src, dst), count in layer_edges.most_common() if src == "tests"]
    package_rows = [(src, dst, count) for (src, dst), count in package_edges.most_common(60)]
    incoming_rows = [(module, count) for module, count in incoming.most_common(15)]
    outgoing_rows = [(module, count) for module, count in outgoing.most_common(15)]
    boundary_status = "PASS" if not graph.boundary_violations else "FAIL"
    mermaid = render_package_mermaid(package_edges)

    markdown = f"""# Dependency Graph

Generated by `python3 scripts/generate_dependency_graph.py` from the current working tree.

Check mode:

```bash
python3 scripts/generate_dependency_graph.py --check
```

Limitations: this captures Python import edges only. It does not see dynamic imports, subprocess calls, shell entrypoints, runtime config references, Feishu/OpenD API coupling, or data-flow dependencies through files and SQLite.

## Summary

- Python files scanned: {len(graph.modules)} (`src`: {file_counts['src']}, `domain`: {file_counts['domain']}, `scripts`: {file_counts['scripts']}, `tests`: {file_counts['tests']})
- Internal import edges: {len(graph.edges)} total, {len(production_edges)} production/script edges excluding tests
- Parse errors: {len(graph.parse_errors)}
- Boundary guard status: **{boundary_status}**
- Production module cycles: {len(graph.module_sccs)}
- Production package cycles after compression: {len(graph.package_sccs)}

## Layer Graph

```mermaid
{render_layer_mermaid(layer_edges)}
```

### Production Layer Edges

{markdown_table(layer_rows, ("from", "to", "imports"))}

### Test Layer Edges

{markdown_table(test_layer_rows, ("from", "to", "imports"))}

## Compressed Production Package Graph

The full compressed Mermaid graph is in [`docs/dependency_graph.mmd`](dependency_graph.mmd). The table below keeps the strongest package-level edges.

{markdown_table(package_rows, ("from", "to", "imports"))}

## Boundary Checks

{render_boundary_violations(graph.boundary_violations)}

This matches the current architecture rule that `domain/domain/` must not import `src/` or `scripts/`, and `src/application/` must not import `scripts/`.

## Cycles

### Module-Level Cycles

{render_sccs(graph.module_sccs, noun="module")}

### Package-Level Cycles

{render_sccs(graph.package_sccs, noun="package")}

Package-level cycles are expected to be noisier because many flat `src.application.*` modules are compressed into broad buckets. Module-level SCCs are usually the more actionable cleanup targets.

## Hotspots

### Most Imported Production Modules

{markdown_table(incoming_rows, ("module", "incoming imports"))}

### Highest Fan-Out Production Modules

{markdown_table(outgoing_rows, ("module", "outgoing imports"))}

## Reading

- `src.interfaces` is a thin facade by intent. Keep `src.interfaces.cli.main` as the public `om` dispatcher, and keep command-specific application imports in focused `src.interfaces.cli.*_ops` owners.
- `src.application.agent_tools` owns Tool Gateway tool metadata and execution; keep each domain `TOOLS` module aligned with registry discovery and permission tests.
- The strongest production dependency remains `src.application -> domain.domain`, which is the intended direction: application orchestration calls deterministic domain policy.
- `src.application -> src.infrastructure` is also expected, but new external-system access should stay in infrastructure adapters rather than leaking directly into domain code.
- Keep shared constants and pure config-section helpers in neutral modules rather than importing them across feature owners.
"""
    return markdown, mermaid


def write_outputs(base: Path, markdown: str, mermaid: str) -> None:
    (base / "docs" / "DEPENDENCY_GRAPH.md").write_text(markdown, encoding="utf-8")
    (base / "docs" / "dependency_graph.mmd").write_text(mermaid, encoding="utf-8")


def check_outputs(base: Path, markdown: str, mermaid: str, graph: DependencyGraph) -> list[str]:
    issues: list[str] = []
    expected = {
        base / "docs" / "DEPENDENCY_GRAPH.md": markdown,
        base / "docs" / "dependency_graph.mmd": mermaid,
    }
    for path, content in expected.items():
        if not path.exists():
            issues.append(f"missing generated file: {path.relative_to(base)}")
            continue
        if path.read_text(encoding="utf-8") != content:
            issues.append(f"generated file is stale: {path.relative_to(base)}")
    if graph.parse_errors:
        issues.append(f"parse errors: {len(graph.parse_errors)}")
    if graph.boundary_violations:
        issues.append(f"forbidden imports: {len(graph.boundary_violations)}")
    if graph.module_sccs:
        issues.append(f"production module cycles: {len(graph.module_sccs)}")
    return issues


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="generate and check the options-monitor Python dependency graph")
    parser.add_argument("--check", action="store_true", help="do not write files; fail if generated docs or architecture guards are stale")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = repo_base()
    graph = analyze_repo(base)
    markdown, mermaid = build_outputs(graph)

    if args.check:
        issues = check_outputs(base, markdown, mermaid, graph)
        if issues:
            for issue in issues:
                print(f"[DEPENDENCY_GRAPH_ERROR] {issue}", file=sys.stderr)
            return 1
        print(f"[OK] dependency graph current; production_modules={len([m for m in graph.modules if not m.startswith('tests')])} cycles=0")
        return 0

    write_outputs(base, markdown, mermaid)
    print(f"[OK] dependency graph generated; production_modules={len([m for m in graph.modules if not m.startswith('tests')])} cycles={len(graph.module_sccs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
