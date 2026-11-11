"""Conservative static entropy measurements; never import business modules."""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path
import re
import tokenize

if __package__:
    from .guardrails_check import declared_public_names
else:
    from guardrails_check import declared_public_names

ROOTS = {"src", "domain", "om_strategy_core", "scripts", "plugins"}
TEXT = {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".ini", ".cfg", ".sh", ".plist"}
VERSION = "entropy-v1"


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def stable_key(rule, path, identity):
    return digest([VERSION, rule, path, identity])


def dead_code(facts):
    return all(facts.get(name) is True for name in (
        "zero_references", "no_runtime_hits", "not_public_api", "not_entry_point",
    ))


def overdesign(facts):
    return any(facts.get(name) is True for name in (
        "single_implementation", "single_switch_value", "single_caller_wrapper", "single_use_parameter",
    ))


def candidate(rule, path, identity, line, raw, *, high=False, facts=None, tool="entropy-ast-v1"):
    evidence = {"tool": tool, "path": path, "identity": identity, "raw": raw, "facts": facts or {}}
    return {"key": stable_key(rule, path, identity), "rule": rule, "line": line,
            "high_confidence": high, **evidence, "evidence_digest": digest(evidence)}


def complexity(function):
    count = 1

    def visit(node):
        nonlocal count
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler)):
            count += 1
        if isinstance(node, ast.BoolOp):
            count += len(node.values) - 1
        if isinstance(node, ast.comprehension):
            count += 1 + len(node.ifs)
        if isinstance(node, ast.match_case):
            count += not (isinstance(node.pattern, ast.MatchAs) and node.pattern.pattern is None)
            count += node.guard is not None
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in function.body:
        visit(statement)
    return count


def functions(tree):
    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                if not isinstance(child, ast.ClassDef):
                    yield name, child
                yield from walk(child, name)
            else:
                yield from walk(child, prefix)
    return walk(tree)


def tokens(source, tree):
    doc_lines = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body and isinstance(body[0], ast.Expr):
            if isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
               tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
    return [(item.string, item.start[0]) for item in tokenize.generate_tokens(io.StringIO(source).readline)
            if item.type not in ignored and item.start[0] not in doc_lines]


def duplicate_measure(token_files, selected=None, window=50, minimum_lines=10):
    selected = set(token_files) if selected is None else set(selected)
    seen, groups, matches = {}, {}, {}
    covered = defaultdict(set)
    word_files = {path: [value for value, _ in rows] for path, rows in token_files.items()}
    for path, rows in sorted(token_files.items()):
        words = word_files[path]
        for index in range(max(0, len(rows) - window + 1)):
            if index and rows[index][1] == rows[index - 1][1]:
                continue
            signature = hashlib.blake2b(json.dumps(words[index:index + window]).encode(), digest_size=16).digest()
            other, start = seen.setdefault(signature, (path, index))
            if other == path or not ({other, path} & selected):
                continue
            pair = (other, path)
            prior = matches.get(pair)
            if prior and prior[0] <= start and start + window <= prior[1] and prior[2] <= index and index + window <= prior[3]:
                continue
            other_words = word_files[other]
            left, other_left = index, start
            while left and other_left and words[left - 1] == other_words[other_left - 1]:
                left -= 1
                other_left -= 1
            right, other_right = index + window, start + window
            while right < len(words) and other_right < len(other_words) and words[right] == other_words[other_right]:
                right += 1
                other_right += 1
            matches[pair] = (other_left, other_right, left, right)
            other_rows = token_files[other]
            if (rows[right - 1][1] - rows[left][1] + 1 < minimum_lines
                    or other_rows[other_right - 1][1] - other_rows[other_left][1] + 1 < minimum_lines):
                continue
            for owner, begin, end in ((path, left, right), (other, other_left, other_right)):
                if owner in selected:
                    covered[owner].update(range(begin, end))
            owner, line = (path, rows[left][1]) if path in selected else (other, other_rows[other_left][1])
            if pair not in groups:
                groups[pair] = candidate("duplicate", owner, f"{other}|{path}|{signature.hex()}", line,
                    f"{right - left} identical tokens: {other}:{other_rows[other_left][1]} / {path}:{rows[left][1]}",
                    high=True, tool="entropy-token-v1", facts={"token_count": right - left,
                        "token_digest": digest(words[left:right]),
                        "locations": [{"path": other, "start": other_rows[other_left][1],
                                       "end": other_rows[other_right - 1][1]},
                                      {"path": path, "start": rows[left][1], "end": rows[right - 1][1]}]})
                groups[pair]["key"] = stable_key("duplicate", f"{other}|{path}", signature.hex())
    total = sum(len(rows) for path, rows in token_files.items() if path in selected)
    repeated = sum(len(positions) for positions in covered.values())
    return {"duplicate_tokens": repeated, "total_tokens": total,
            "duplicate_ratio": repeated / total if total else 0.0}, list(groups.values())


def scan(root: Path, selected: list[str], coverage=None, *, vulture_unused=None, radon_complexity=None):
    """Measure the selected files.

    vulture_unused maps a relative path to the definition lines vulture reports as
    unused functions or methods.  It never creates a candidate by itself: it can
    only corroborate (or veto) a function the built-in whole-tree reference index
    already found unreferenced.  radon_complexity maps a relative path to
    {definition line: cyclomatic complexity}; when present it is the authoritative
    complexity measurement and the built-in counter is only a fallback.
    """
    scoped = [path for path in sorted(set(selected)) if Path(path).suffix == ".py"
              and Path(path).parts[0] in ROOTS and (root / path).is_file() and not (root / path).is_symlink()]
    words, calls, subclasses = Counter(), Counter(), Counter()
    arguments, token_files = defaultdict(list), {}
    star_imports = False
    paths = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink()) if scoped else []
    for file in paths:
        if file.suffix not in TEXT:
            continue
        source = file.read_text(encoding="utf-8", errors="replace")
        words.update(re.findall(r"\b[A-Za-z_]\w*\b", source))
        if file.suffix != ".py":
            continue
        tree = ast.parse(source, filename=str(file))
        relative = file.relative_to(root).as_posix()
        if file.relative_to(root).parts[0] in ROOTS:
            token_files[relative] = tokens(source, tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                star_imports = True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls[node.func.id] += 1
                arguments[node.func.id].append(node)
            if isinstance(node, ast.ClassDef):
                subclasses.update(base.id for base in node.bases if isinstance(base, ast.Name))
    vulture_analyzed = set(vulture_unused.get("analyzed", ())) if vulture_unused is not None else set()
    vulture_lines = vulture_unused.get("unused", {}) if vulture_unused is not None else {}
    findings, scores = [], []
    loc = code_loc = zero_refs = unused_config = unreachable = missed_branches = 0
    vulture_rejected = unmatched_complexity = 0
    runtime_complete = coverage is not None
    for path in scoped:
        source = (root / path).read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=path)
        loc += len(lines)
        code_loc += sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines)
        try:
            public = declared_public_names(source)
        except ValueError:
            public = None
        runtime = (coverage or {}).get(path)
        if runtime is None or "missing_branches" not in runtime:
            runtime_complete = False
        else:
            missed_branches += len(runtime["missing_branches"])
        dynamic = star_imports or path.startswith(("plugins/", "scripts/")) or any(words[name] for name in (
            "getattr", "globals", "locals", "vars", "__dict__", "__builtins__", "eval", "exec",
            "__import__", "import_module", "register", "entry_points"))
        top = {id(node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        function_items = list(functions(tree))
        measured_complexity = (radon_complexity or {}).get(path)
        for name, node in function_items:
            cc = measured_complexity.get(node.lineno) if measured_complexity is not None else None
            if cc is None:
                cc = complexity(node)
                if radon_complexity is not None:
                    unmatched_complexity += 1
            scores.append(cc)
            basis = "radon" if measured_complexity is not None and node.lineno in measured_complexity else "entropy-ast-v1"
            if cc > 10:
                findings.append(candidate("complexity", path, name, node.lineno,
                    f"{basis} cyclomatic complexity={cc} >10", high=True,
                    facts={"complexity": cc, "threshold": 10,
                           "source_digest": digest(ast.dump(node, include_attributes=False))},
                    tool="radon" if basis == "radon" else "entropy-ast-v1"))
            if id(node) not in top or not node.name.startswith("_") or node.name.startswith("__"):
                continue
            if words[node.name] == 1:
                corroborated = (vulture_unused is None or path not in vulture_analyzed
                                or node.lineno in vulture_lines.get(path, ()))
                if not corroborated:
                    vulture_rejected += 1
                else:
                    zero_refs += 1
                    body = list(node.body)
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        body = body[1:]
                    span = {item.lineno for statement in body for item in ast.walk(statement) if isinstance(item, ast.stmt)}
                    measured = set((runtime or {}).get("executed_lines", [])) | set((runtime or {}).get("missing_lines", []))
                    facts = {"zero_references": True,
                             "no_runtime_hits": (not bool(span & set(runtime["executed_lines"])))
                             if runtime and span and span <= measured else None,
                             "not_public_api": node.name not in public if public is not None else None,
                             "not_entry_point": None if dynamic or node.decorator_list else True}
                    if vulture_unused is not None and path in vulture_analyzed:
                        facts["vulture_unused"] = True
                    facts["source_digest"] = digest(ast.dump(node, include_attributes=False))
                    findings.append(candidate("dead-code", path, name, node.lineno,
                        f"AST/text reference count=0; gates={facts}", high=dead_code(facts), facts=facts,
                        tool="vulture+entropy-ast-v1" if vulture_unused is not None and path in vulture_analyzed
                        else "entropy-ast-v1"))
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            wrapper = len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Call)
            if calls[node.name] == 1 and not node.decorator_list:
                if wrapper:
                    findings.append(candidate("overdesign-wrapper", path, name, node.lineno,
                        "one explicit caller; body forwards one call", facts={"single_caller_wrapper": True}))
                invocation = arguments[node.name][0]
                parameters = node.args.posonlyargs + node.args.args
                supplied = dict(zip((arg.arg for arg in parameters), invocation.args))
                supplied.update((kw.arg, kw.value) for kw in invocation.keywords if kw.arg)
                forwarded = {arg.id for item in ast.walk(node) if isinstance(item, ast.Call)
                             for arg in [*item.args, *(kw.value for kw in item.keywords)] if isinstance(arg, ast.Name)}
                for parameter, value in supplied.items():
                    if parameter in forwarded and isinstance(value, ast.Constant):
                        findings.append(candidate("overdesign-parameter", path, f"{name}:{parameter}", node.lineno,
                            f"one caller forwards constant parameter {parameter}={value.value!r}",
                            facts={"single_use_parameter": True}))
        assignments, conditions = defaultdict(list), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                (isinstance(base, ast.Name) and base.id in {"ABC", "Protocol"}) or
                (isinstance(base, ast.Attribute) and base.attr in {"ABC", "Protocol"}) for base in node.bases
            ) and subclasses[node.name] == 1:
                findings.append(candidate("overdesign-abstraction", path, node.name, node.lineno,
                    "ABC/Protocol has one explicit direct subclass; structural implementations unknown",
                    facts={"single_implementation": True}))
            if isinstance(node, (ast.If, ast.While)):
                conditions.update(item.id for item in ast.walk(node.test) if isinstance(item, ast.Name))
                if isinstance(node.test, ast.Constant) and node.test.value is False and node.body:
                    unreachable += 1
                    owners = [(function.end_lineno - function.lineno, name) for name, function in function_items
                              if function.lineno <= node.lineno <= function.end_lineno]
                    owner = min(owners)[1] if owners else "<module>"
                    identity = [owner, ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False)]
                    findings.append(candidate("unreachable-branch", path, digest(identity), node.lineno,
                        "\n".join(lines[node.lineno - 1:node.body[0].lineno + 2]),
                        high=True, facts={"literal_false": True}))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    assignments[target.id].append(node.value)
                    if "CONFIG" not in target.id.upper() or not isinstance(node.value, ast.Dict):
                        continue
                    for item in ast.walk(node.value):
                        if isinstance(item, ast.Dict):
                            for key in item.keys:
                                if isinstance(key, ast.Constant) and isinstance(key.value, str) and words[key.value] == 1:
                                    unused_config += 1
                                    findings.append(candidate("unused-config", path, f"{target.id}:{key.value}", key.lineno,
                                        f"configuration literal {key.value!r} has no other text occurrence"))
        for name, values in assignments.items():
            if name in conditions and values and all(isinstance(value, ast.Constant) and type(value.value) is bool
                                                     for value in values) and len({value.value for value in values}) == 1:
                findings.append(candidate("overdesign-switch", path, name, values[0].lineno,
                    f"one observed boolean assignment value: {values[0].value}", facts={"single_switch_value": True}))
    duplication, duplicates = duplicate_measure(token_files, scoped)
    findings.extend(duplicates)
    bins = {"1-5": sum(value <= 5 for value in scores), "6-10": sum(6 <= value <= 10 for value in scores),
            "11-20": sum(11 <= value <= 20 for value in scores), ">20": sum(value > 20 for value in scores)}
    dead_rows = [row for row in findings if row["rule"] == "dead-code"]
    dead_complete = runtime_complete and all(all(value is not None for value in row["facts"].values()) for row in dead_rows)
    metrics = {"files": len(scoped), "loc": loc, "code_loc": code_loc,
               "dead_code_symbols": sum(row["high_confidence"] for row in dead_rows)
               if dead_complete else None, "dead_code_candidates": zero_refs,
               "dead_code_basis": "vulture+entropy-ast-v1" if vulture_unused is not None else "entropy-ast-v1",
               "dead_code_vulture_rejected": vulture_rejected if vulture_unused is not None else None,
               "unreachable_branches": unreachable,
               "untested_branches": missed_branches if runtime_complete else None,
               "complexity_distribution": bins, "complexity_gt10": sum(value > 10 for value in scores),
               "complexity_basis": ("radon" if radon_complexity is not None and not unmatched_complexity else
                                    "entropy-ast-v1" if radon_complexity is None else "radon+entropy-ast-v1-fallback"),
               "complexity_unmatched": unmatched_complexity if radon_complexity is not None else None,
               "functions": len(scores), "unused_config_items": None, "unused_config_candidates": unused_config,
               **duplication}
    return metrics, list({row["key"]: row for row in findings}.values())
