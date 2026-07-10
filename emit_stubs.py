#!/usr/bin/env python3
"""Deterministic concept-to-code stub generator.

Reads a concept JSON document conforming to schemas/spec.schema.json and emits:
- a Rust module with verbatim Rustdoc and unimplemented stubs;
- a proptest scaffold file derived from constraints and adversary cases.

The script is intentionally stdlib-only so it can run without extra Python
dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

FORBIDDEN_QUERY_MUT = "&mut self"
QUERY_SIG_RE = re.compile(r"^fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^)]*)\)\s*(?P<ret>(?:->\s*[^;{]+)?)\s*$")
COMMAND_SIG_RE = re.compile(r"^fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^)]*)\)\s*(?P<ret>(?:->\s*[^;{]+)?)\s*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VERUS_CFG = '#[cfg(any(verus, feature = "verus"))]'
NOT_VERUS_CFG = '#[cfg(not(any(verus, feature = "verus")))]'

# Verifier-contracts facade crate paths. Overridden in main() from
# --contracts-crate; defaults assume a crate named `contracts` exposing
# top-level `creusot` and `verus::prelude` modules.
CONTRACTS_CREUSOT = "contracts::creusot"
CONTRACTS_VERUS_PRELUDE = "contracts::verus::prelude"

# Root directory searched for another crate's spec JSON when resolving
# `implements`/`trait_ref`/`variants` cross-crate references. Overridden in
# main() from --specs-search-root; defaults to the parent of --crate-dir so
# a single-crate invocation with no cross-crate references is unaffected.
SPECS_SEARCH_ROOT = Path(".")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Method:
    kind: str
    name: str
    english: str
    rust_sig: str


def snake_case(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper()):
            out.append("_")
        out.append(ch.lower())
    return "".join(out).replace("-", "_")


def strategy_name(spec: dict[str, Any]) -> str:
    return f"{snake_case(spec['concept'])}_strategy"


def rust_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def doc_lines(text: str, prefix: str = "///") -> list[str]:
    return [f"{prefix} {line}" if line else prefix for line in text.splitlines()]


def method_name(signature: str, *, query: bool) -> str:
    pattern = QUERY_SIG_RE if query else COMMAND_SIG_RE
    match = pattern.match(signature.strip().rstrip(";"))
    if not match:
        raise SpecError(f"invalid {'query' if query else 'command'} signature: {signature!r}")
    return match.group("name")


def returns_unit(signature: str) -> bool:
    signature = signature.strip().rstrip(";")
    match = QUERY_SIG_RE.match(signature)
    if not match:
        raise SpecError(f"invalid query signature: {signature!r}")
    ret = match.group("ret").strip()
    return ret == "" or ret == "-> ()"


def concept_kind(spec: dict[str, Any]) -> str:
    kind = spec.get("kind", "struct")
    if kind not in {"struct", "trait", "enum"}:
        raise SpecError(f"kind must be one of: struct, trait, enum (got {kind!r})")
    return kind


def validate_enum_spec(spec: dict[str, Any]) -> list[Method]:
    """Lighter validation for kind='enum'. No queries/commands of its own --
    methods to dispatch come from resolving trait_ref."""
    required = ["schema_version", "concept", "cluster", "english_description", "verifier", "trait_ref", "variants"]
    missing = [key for key in required if key not in spec]
    if missing:
        raise SpecError(f"missing required fields for kind=enum: {', '.join(missing)}")
    if spec["schema_version"] != "1.0":
        raise SpecError("schema_version must be '1.0'")
    if not re.match(r"^[A-Z][A-Za-z0-9]*$", spec["concept"]):
        raise SpecError("concept must match ^[A-Z][A-Za-z0-9]*$")
    if spec["verifier"] not in {"kani", "creusot", "verus"}:
        raise SpecError("verifier must be one of: kani, creusot, verus")
    trait_ref = spec["trait_ref"]
    if "crate" not in trait_ref or "concept" not in trait_ref:
        raise SpecError("trait_ref requires 'crate' and 'concept'")
    variants = spec.get("variants") or []
    if not variants:
        raise SpecError("variants must be present and non-empty for kind=enum")
    seen_names: set[str] = set()
    for variant in variants:
        name = variant.get("name", "")
        if not re.match(r"^[A-Z][A-Za-z0-9]*$", name):
            raise SpecError(f"variant name must match ^[A-Z][A-Za-z0-9]*$: {name!r}")
        if name in seen_names:
            raise SpecError(f"duplicate variant name: {name}")
        seen_names.add(name)
        wraps = variant.get("wraps", {})
        if "crate" not in wraps or "concept" not in wraps:
            raise SpecError(f"variant {name!r} 'wraps' requires 'crate' and 'concept'")
    return []


def validate_spec(spec: dict[str, Any]) -> list[Method]:
    kind = concept_kind(spec)
    if kind == "enum":
        return validate_enum_spec(spec)

    required = [
        "schema_version",
        "concept",
        "cluster",
        "english_description",
        "queries",
        "commands",
        "constraints",
        "adversary_table",
        "verifier",
    ]
    missing = [key for key in required if key not in spec]
    if missing:
        raise SpecError(f"missing required fields: {', '.join(missing)}")
    if spec["schema_version"] != "1.0":
        raise SpecError("schema_version must be '1.0'")
    if not re.match(r"^[A-Z][A-Za-z0-9]*$", spec["concept"]):
        raise SpecError("concept must match ^[A-Z][A-Za-z0-9]*$")
    if spec["verifier"] not in {"kani", "creusot", "verus"}:
        raise SpecError("verifier must be one of: kani, creusot, verus")
    if not spec.get("adversary_table"):
        raise SpecError("adversary_table must be present and non-empty")
    checks = spec.get("kani_f64_checks", [])
    if checks and spec["verifier"] != "creusot":
        raise SpecError("kani_f64_checks are only valid for creusot-primary concepts")
    check_names: set[str] = set()
    for check in checks:
        name = check.get("name", "")
        if not re.match(r"^[a-z][a-z0-9_]*$", name):
            raise SpecError(f"invalid kani_f64_checks name: {name!r}")
        if name in check_names:
            raise SpecError(f"duplicate kani_f64_checks name: {name}")
        check_names.add(name)
        symbolic_f64s = check.get("symbolic_f64s", [])
        if not symbolic_f64s:
            raise SpecError(f"kani_f64_checks {name!r} requires symbolic_f64s")
        if len(symbolic_f64s) != len(set(symbolic_f64s)):
            raise SpecError(f"kani_f64_checks {name!r} has duplicate symbolic_f64s")
        if any(not IDENT_RE.match(symbol) for symbol in symbolic_f64s):
            raise SpecError(f"kani_f64_checks {name!r} has an invalid symbolic f64 name")
        for required_list in ("assumptions", "statements"):
            if not check.get(required_list):
                raise SpecError(f"kani_f64_checks {name!r} requires {required_list}")
        if check.get("expected") not in {"pass", "panic"}:
            raise SpecError(f"kani_f64_checks {name!r} expected must be pass or panic")

    methods: list[Method] = []
    for query in spec.get("queries", []):
        sig = query.get("rust_sig", "").strip().rstrip(";")
        if query.get("pure") is not True:
            raise SpecError(f"query {sig!r} must set pure: true")
        if FORBIDDEN_QUERY_MUT in sig:
            raise SpecError(f"query {sig!r} must not use &mut self")
        if returns_unit(sig):
            raise SpecError(f"query {sig!r} must return a value, not ()")
        name = method_name(sig, query=True)
        methods.append(Method("query", name, query.get("english", ""), sig))

    for command in spec.get("commands", []):
        sig = command.get("rust_sig", "").strip().rstrip(";")
        name = method_name(sig, query=False)
        methods.append(Method("command", name, command.get("english", ""), sig))

    if not methods:
        raise SpecError("at least one query or command is required")

    method_names = {m.name for m in methods}
    for trait_name, trait_info in spec.get("implements", {}).items():
        if "crate" not in trait_info or "methods" not in trait_info:
            raise SpecError(f"implements[{trait_name!r}] requires 'crate' and 'methods'")
        unknown = [name for name in trait_info["methods"] if name not in method_names]
        if unknown:
            raise SpecError(
                f"implements[{trait_name!r}].methods names methods not in this "
                f"concept's own queries/commands: {unknown}"
            )
    return methods


def resolve_cross_crate_spec(crate: str, concept: str) -> dict[str, Any]:
    """Load another crate's concept JSON by convention.

    Used by `implements`/`trait_ref`/`variants` references, which are the
    first things in this generator that need to read a spec file other than
    the one passed on the command line. Path convention: `<specs-search-root>/
    <crate>/specs/<snake_case(concept)>.json`, where `--specs-search-root`
    defaults to the parent of `--crate-dir` (see SPECS_SEARCH_ROOT/main()) --
    so a single-crate invocation with no cross-crate references is unaffected.
    A missing file is a hard error, never a silent skip.
    """
    path = SPECS_SEARCH_ROOT / crate / "specs" / f"{snake_case(concept)}.json"
    if not path.exists():
        raise SpecError(
            f"cross-crate spec not found: {path} (referenced as {crate}::{concept})"
        )
    return json.loads(path.read_text())


def constraint_applies_to(constraint: dict[str, Any]) -> list[str]:
    applies = constraint.get("applies_to") or []
    if isinstance(applies, str):
        return [applies]
    return list(applies)


def applicable_constraints(spec: dict[str, Any], method: Method) -> Iterable[dict[str, Any]]:
    for constraint in spec.get("constraints", []):
        applies = constraint_applies_to(constraint)
        if not applies or method.name in applies:
            yield constraint


def _find_matching(text: str, open_index: int, open_char: str, close_char: str) -> int:
    depth = 0
    for idx in range(open_index, len(text)):
        char = text[idx]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return idx
    raise SpecError(f"unbalanced {open_char}{close_char} expression in Verus logic: {text}")


def _find_top_level(text: str, needle: str) -> int:
    paren = bracket = 0
    idx = 0
    while idx <= len(text) - len(needle):
        char = text[idx]
        if char == "(":
            paren += 1
        elif char == ")":
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket -= 1
        if paren == 0 and bracket == 0 and text.startswith(needle, idx):
            return idx
        idx += 1
    return -1


def _split_top_level(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    paren = bracket = 0
    start = 0
    idx = 0
    while idx < len(text):
        char = text[idx]
        if char == "(":
            paren += 1
        elif char == ")":
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket -= 1
        if paren == 0 and bracket == 0 and text.startswith(sep, idx):
            parts.append(text[start:idx].strip())
            idx += len(sep)
            start = idx
            continue
        idx += 1
    parts.append(text[start:].strip())
    return parts


def _cast_quantified_refs(expr: str, quantified: list[str], query_spec_map: dict[str, str] | None = None) -> str:
    if not quantified:
        return expr

    def cast_arg(arg: str) -> str:
        stripped = arg.strip()
        if stripped in quantified:
            return f"{stripped} as usize"
        return stripped

    def cast_method_args(match: re.Match[str]) -> str:
        args = ", ".join(cast_arg(arg) for arg in match.group(2).split(","))
        return f"{match.group(1)}{args})"

    query_names = set(query_spec_map or {})
    query_names.update((query_spec_map or {}).values())
    query_names.update({"equilibrium_frequency", "rate"})
    name_alternation = "|".join(re.escape(name) for name in sorted(query_names, key=len, reverse=True))
    out = re.sub(
        rf"((?:self|old\(self\)|final\(self\))\.(?:{name_alternation})\()([^)]*)\)",
        cast_method_args,
        expr,
    )

    def cast_index(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        inner = re.sub(r"self\.state_count\(\)", "((self.state_count()) as int)", inner)
        return f"out@[{inner}]"

    return re.sub(r"out\[([^]]+)\]", cast_index, out)


def _suffix_verus_float_literals(expr: str) -> str:
    return re.sub(r"(?<![A-Za-z0-9_])(\d+\.\d+)(?![A-Za-z0-9_])", r"\1f64", expr)


_INT_CLOSURE_PARAM_RE = re.compile(r"\|\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*int\s*\|")


def _cast_bare_slice_index_to_int(expr: str, quantified: list[str]) -> str:
    """Cast a bare (non-quantified) identifier used as a whole slice index to
    `int`, e.g. `values[pattern]` -> `values[(pattern) as int]`.

    Verus's `Seq::spec_index` requires an `int` argument. A compound index
    expression that already mixes in a quantified `int` variable (e.g.
    `values[m * n + pattern]`) type-checks via Verus's arithmetic promotion
    once an `int` operand is present, but a bare `usize`-typed identifier
    used alone as the entire index has nothing to promote it, so it needs an
    explicit cast.

    Skips identifiers already known to be `int`: those in `quantified` (the
    caller's own nesting depth) plus any `|name: int|` closure parameter
    already present in `expr` -- the latter matters because this function
    also runs on the *outer* string after `_replace_sums`/`_replace_products`/
    `_replace_foralls` have already spliced in fully translated inner
    closures; at that point `quantified` no longer describes those inner
    closures' bound variables, so without rescanning the text itself a
    variable like `s` in an already-correct `|s: int| ...values[s]...` would
    be misidentified as an untouched bare `usize` and incorrectly re-cast.
    Also skips `out`, which this module's own `_cast_quantified_refs` already
    rewrites to `out@[...]` (a different, dedicated convention for the
    generated output-buffer parameter)."""
    protected = set(quantified)
    protected.update(_INT_CLOSURE_PARAM_RE.findall(expr))

    def cast(match: re.Match[str]) -> str:
        name, index = match.group(1), match.group(2)
        if name == "out" or index in protected:
            return match.group(0)
        return f"{name}[({index}) as int]"

    return re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\[([A-Za-z_][A-Za-z0-9_]*)\]", cast, expr)


def _use_final_mut_refs(expr: str, refs: Iterable[str]) -> str:
    out = expr
    for name in refs:
        escaped = re.escape(name)
        out = re.sub(rf"\b{escaped}\.len\(\)", f"final({name})@.len()", out)
        out = re.sub(rf"\b{escaped}@\[", f"final({name})@[", out)
    return out


def _wrap_final_self(logic: str) -> str:
    """Disambiguate bare `self.` references in &mut self postconditions as `final(self).`.

    Verus's mutable-reference postcondition support requires every dereference of
    `self` in an ensures clause to be wrapped in `old(...)` or `final(...)`; bare
    `self.foo()` is rejected as ambiguous. `old(self).foo()` is left untouched.

    Caller must only invoke this for `&mut self` methods — `final(self)` requires a
    mutable receiver and does not typecheck on `&self` queries, where bare `self` in
    a postcondition is unambiguous (there is no pre/post distinction to disambiguate).
    """
    return re.sub(r"(?<!old\()\bself\.", "final(self).", logic)


def mutable_ref_args(method: Method) -> list[str]:
    signature = method.rust_sig.strip().rstrip(";")
    match = QUERY_SIG_RE.match(signature) or COMMAND_SIG_RE.match(signature)
    if not match:
        raise SpecError(f"invalid method signature: {method.rust_sig!r}")
    names: list[str] = []
    for raw_arg in match.group("args").split(","):
        arg = raw_arg.strip()
        if ":" not in arg:
            continue
        name, ty = [part.strip() for part in arg.split(":", 1)]
        if ty.startswith("&mut"):
            names.append(name)
    return names


def _parse_range_clause(clause: str) -> tuple[list[str], str, str | None, str]:
    colon = _find_top_level(clause, ":")
    if colon < 0:
        raise SpecError(f"forall/sum clause missing ':' in Verus logic: {clause}")
    head = clause[:colon].strip()
    body = clause[colon + 1 :].strip()
    where_idx = _find_top_level(head, " where ")
    guard = None
    if where_idx >= 0:
        guard = head[where_idx + len(" where ") :].strip()
        head = head[:where_idx].strip()
    if " in 0.." not in head:
        raise SpecError(f"unsupported range clause in Verus logic: {clause}")
    vars_part, end_expr = head.split(" in 0..", 1)
    vars_ = [var.strip() for var in vars_part.split(",") if var.strip()]
    return vars_, end_expr.strip(), guard, body


def _replace_if_then_else(expr: str) -> str:
    return re.sub(
        r"\bif\s+(.+?)\s+then\s+(.+?)\s+else\s+(.+?)(?=\)|\s+&&|\s+\|\||$)",
        r"(if \1 { \2 } else { \3 })",
        expr,
    )


def _replace_f64_is_finite(expr: str) -> str:
    return re.sub(
        r"([A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?(?:\[[^]]+\])?(?:@\[[^]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*\([^)]*\))*)\.is_finite\(\)",
        r"verus_f64_is_finite(\1)",
        expr,
    )


def _replace_abs(expr: str, quantified: list[str], query_spec_map: dict[str, str] | None = None) -> str:
    out = expr
    while True:
        start = out.find("abs(")
        if start < 0:
            return out
        open_index = start + len("abs")
        close_index = _find_matching(out, open_index, "(", ")")
        inner = _translate_verus_expr(out[open_index + 1 : close_index], quantified, query_spec_map=query_spec_map)
        out = f"{out[:start]}abs_f64({inner}){out[close_index + 1:]}"


def _replace_sums(expr: str, quantified: list[str], query_spec_map: dict[str, str] | None = None) -> str:
    out = expr
    while True:
        _sum_match = re.search(r"(?<![A-Za-z0-9_])sum\(", out)
        start = _sum_match.start() if _sum_match else -1
        if start < 0:
            return out
        open_index = start + len("sum")
        close_index = _find_matching(out, open_index, "(", ")")
        vars_, end_expr, guard, body = _parse_range_clause(out[open_index + 1 : close_index])
        if len(vars_) != 1:
            raise SpecError(f"sum supports one range variable, got {vars_}")
        var = vars_[0]
        nested_quantified = quantified + [var]
        translated_body = _translate_verus_expr(body, nested_quantified, query_spec_map=query_spec_map)
        end = f"({end_expr}) as int"
        if guard:
            translated_guard = _cast_quantified_refs(
                _translate_verus_expr(guard, nested_quantified, query_spec_map=query_spec_map),
                nested_quantified,
                query_spec_map,
            )
            replacement = (
                f"verus_sum_f64_where(0, {end}, |{var}: int| {translated_guard}, "
                f"|{var}: int| {translated_body})"
            )
        else:
            replacement = f"verus_sum_f64(0, {end}, |{var}: int| {translated_body})"
        out = f"{out[:start]}{replacement}{out[close_index + 1:]}"


def _replace_products(expr: str, quantified: list[str], query_spec_map: dict[str, str] | None = None) -> str:
    out = expr
    while True:
        _product_match = re.search(r"(?<![A-Za-z0-9_])product\(", out)
        start = _product_match.start() if _product_match else -1
        if start < 0:
            return out
        open_index = start + len("product")
        close_index = _find_matching(out, open_index, "(", ")")
        vars_, end_expr, guard, body = _parse_range_clause(out[open_index + 1 : close_index])
        if len(vars_) != 1:
            raise SpecError(f"product supports one range variable, got {vars_}")
        var = vars_[0]
        nested_quantified = quantified + [var]
        translated_body = _translate_verus_expr(body, nested_quantified, query_spec_map=query_spec_map)
        end = f"({end_expr}) as int"
        if guard:
            translated_guard = _cast_quantified_refs(
                _translate_verus_expr(guard, nested_quantified, query_spec_map=query_spec_map),
                nested_quantified,
                query_spec_map,
            )
            replacement = (
                f"verus_product_f64_where(0, {end}, |{var}: int| {translated_guard}, "
                f"|{var}: int| {translated_body})"
            )
        else:
            replacement = f"verus_product_f64(0, {end}, |{var}: int| {translated_body})"
        out = f"{out[:start]}{replacement}{out[close_index + 1:]}"


_TRIGGER_RECEIVER_CALL_RE = re.compile(r"(?:final\(self\)|old\(self\)|self|result)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_TRIGGER_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_TRIGGER_HELPER_CALL_RE = re.compile(
    r"\b(?:verus_sum_f64_where|verus_sum_f64|verus_product_f64_where|verus_product_f64|abs_f64|verus_f64_is_finite)\b"
)


def _extract_trigger_candidates(text: str) -> list[str]:
    """Find subterms of `text` that could serve as Verus quantifier triggers.

    Candidates are receiver method calls (`self.foo_spec(...)`,
    `result.foo_spec(...)`, `final(self).foo_spec(...)`,
    `old(self).foo_spec(...)`), array/slice indexing (`name[...]`,
    including chained `name[...][...]`), and calls to the generated
    spec-helper functions (`verus_sum_f64`, `verus_sum_f64_where`,
    `verus_product_f64`, `verus_product_f64_where`, `abs_f64`,
    `verus_f64_is_finite`). These are the "open spec fn" and index shapes
    that appear in generated bounded-forall bodies. Helper-call candidates
    are appended last so receiver/index candidates are preferred when both
    cover the required variables.
    """
    candidates: list[str] = []
    for match in _TRIGGER_RECEIVER_CALL_RE.finditer(text):
        end = match.end()
        if end < len(text) and text[end] == "(":
            close = _find_matching(text, end, "(", ")")
            candidates.append(text[match.start() : close + 1])
    for match in _TRIGGER_IDENT_RE.finditer(text):
        end = match.end()
        if end >= len(text) or text[end] != "[":
            continue
        close = _find_matching(text, end, "[", "]")
        while close + 1 < len(text) and text[close + 1] == "[":
            close = _find_matching(text, close + 1, "[", "]")
        candidates.append(text[match.start() : close + 1])
    for match in _TRIGGER_HELPER_CALL_RE.finditer(text):
        end = match.end()
        if end < len(text) and text[end] == "(":
            close = _find_matching(text, end, "(", ")")
            candidate = text[match.start() : close + 1]
            # Verus rejects `#![trigger ...]` terms containing a closure
            # (e.g. `verus_sum_f64(0, n, |j: int| ...)`) with
            # "the argument to `closure_to_fn` must be a closure". Skip any
            # helper-call candidate whose argument list still contains one.
            if "|" not in candidate:
                candidates.append(candidate)
    return candidates


def _select_trigger_terms(body: str, vars_: list[str]) -> list[str] | None:
    """Pick Verus `#![trigger ...]` terms for a bounded forall over `vars_`.

    Only the part of `body` before any nested `forall` is searched: a
    nested forall's own trigger covers its bound variables, and a term
    that mentions a deeper-bound variable is not a valid trigger for this
    (outer) quantifier. Returns `None` when no candidate mentions every
    variable in `vars_` (single term) or when no pair of candidates
    jointly covers `vars_` (multi-trigger).
    """
    nested_forall = re.search(r"\bforall\b", body)
    source = body[: nested_forall.start()] if nested_forall else body
    if not source.strip():
        return None
    candidates = _extract_trigger_candidates(source)
    var_patterns = {var: re.compile(rf"\b{re.escape(var)}\b") for var in vars_}

    def covers(span: str) -> set[str]:
        return {var for var, pattern in var_patterns.items() if pattern.search(span)}

    full = set(vars_)
    for span in candidates:
        if covers(span) == full:
            return [span]
    if len(vars_) > 1:
        for i, first in enumerate(candidates):
            first_cover = covers(first)
            if not first_cover or first_cover == full:
                continue
            for second in candidates[i + 1 :]:
                second_cover = covers(second)
                if first_cover | second_cover == full and first_cover != second_cover:
                    return [first, second]
    return None


def _replace_foralls(expr: str, quantified: list[str], query_spec_map: dict[str, str] | None = None) -> str:
    match = re.search(r"\bforall\b", expr)
    if not match:
        return expr
    prefix = expr[: match.start()]
    clause = expr[match.end() :].strip()
    vars_, end_expr, guard, body = _parse_range_clause(clause)

    # Flatten immediately-nested foralls with mutually independent bounds
    # (`forall a in 0..A: forall b in 0..B: BODY`) into a single multi-variable
    # forall (`forall|a, b| (bounds_a) && (bounds_b) ==> BODY`). This gives
    # `_select_trigger_terms` a body with no remaining nested `forall`, so a
    # single term mentioning all bound variables (e.g. `arr[a * B + b]`) can
    # serve as the trigger for the whole quantifier. Without flattening, Verus
    # cannot infer a trigger for an outer quantifier whose entire body is
    # another quantifier.
    all_vars = list(vars_)
    bound_groups: list[tuple[list[str], str, str | None]] = [(vars_, end_expr, guard)]
    while True:
        stripped = body.lstrip()
        if not stripped.startswith("forall"):
            break
        inner_clause = stripped[len("forall") :].strip()
        try:
            inner_vars, inner_end_expr, inner_guard, inner_body = _parse_range_clause(inner_clause)
        except SpecError:
            break
        bound_var_pattern = re.compile(r"\b(?:" + "|".join(re.escape(v) for v in all_vars) + r")\b")
        if bound_var_pattern.search(inner_end_expr) or (inner_guard and bound_var_pattern.search(inner_guard)):
            break
        all_vars.extend(inner_vars)
        bound_groups.append((inner_vars, inner_end_expr, inner_guard))
        body = inner_body

    nested_quantified = quantified + all_vars
    bounds: list[str] = []
    for group_vars, group_end_expr, group_guard in bound_groups:
        for var in group_vars:
            bounds.extend([f"0 <= {var}", f"{var} < ({group_end_expr}) as int"])
        if group_guard:
            bounds.append(_cast_quantified_refs(
                _translate_verus_expr(group_guard, nested_quantified, query_spec_map=query_spec_map),
                nested_quantified,
                query_spec_map,
            ))
    translated_body = _translate_verus_expr(body, nested_quantified, query_spec_map=query_spec_map)
    params = ", ".join(f"{var}: int" for var in all_vars)
    triggers = _select_trigger_terms(translated_body, all_vars)
    trigger_clause = f" #![trigger {', '.join(triggers)}]" if triggers else ""
    replacement = f"forall|{params}|{trigger_clause} {' && '.join(bounds)} ==> {translated_body}"
    return f"{prefix}{replacement}"


def _translate_verus_expr(expr: str, quantified: list[str], final_mut_refs: Iterable[str] = (), query_spec_map: dict[str, str] | None = None) -> str:
    out = expr.strip()
    out = re.sub(r"\bimplies\b", "==>", out)
    out = _replace_if_then_else(out)
    out = _replace_abs(out, quantified, query_spec_map)
    out = _replace_sums(out, quantified, query_spec_map)
    out = _replace_products(out, quantified, query_spec_map)
    out = _replace_foralls(out, quantified, query_spec_map)
    out = _cast_quantified_refs(out, quantified, query_spec_map)
    out = _cast_bare_slice_index_to_int(out, quantified)
    out = _use_final_mut_refs(out, final_mut_refs)
    out = _replace_query_calls(out, query_spec_map or {})
    out = _replace_f64_is_finite(out)
    out = _suffix_verus_float_literals(out)
    return out


def translate_logic_to_verus(logic: str, final_mut_refs: Iterable[str] = (), query_spec_map: dict[str, str] | None = None) -> str:
    """Translate restricted-English `logic` from spec JSON to Verus syntax.

    Handles implication, simple if/then/else expressions, bounded forall
    quantifiers, and `sum(i in 0..N: EXPR)` aggregates. Aggregate sums are
    emitted through generated ghost/spec helper functions so the original
    arithmetic structure is preserved instead of degrading to `true`.
    """
    src = logic.strip()
    if not src:
        return src
    return _translate_verus_expr(src, [], final_mut_refs, query_spec_map)


def rust_return_type(signature: str) -> str:
    stripped = signature.strip().rstrip(";")
    match = COMMAND_SIG_RE.match(stripped) or QUERY_SIG_RE.match(stripped)
    if not match:
        raise SpecError(f"invalid method signature: {signature!r}")
    ret = match.group("ret").strip()
    return ret.removeprefix("->").strip() if ret else "()"


def verus_stub_body_lines(method: Method) -> list[str]:
    if rust_return_type(method.rust_sig) == "()":
        return ["        assume(false);"]
    return ["        assume(false);", "        loop {}"]


def rust_signature_with_named_result(signature: str) -> str:
    """Return a Verus-compatible function signature with `result` bound.

    Verus does not provide an implicit post-state return variable in `ensures`
    clauses. Generated methods whose contracts mention `result` must name the
    return slot directly in the signature.
    """
    stripped = signature.strip().rstrip(";")
    match = COMMAND_SIG_RE.match(stripped) or QUERY_SIG_RE.match(stripped)
    if not match:
        raise SpecError(f"invalid method signature: {signature!r}")
    ret = match.group("ret").strip()
    ret_ty = ret.removeprefix("->").strip() if ret else "()"
    prefix = stripped[: match.start("ret")].rstrip() if ret else stripped
    return f"{prefix} -> (result: {ret_ty})"


def _parse_bare_creusot_quantifier_clause(clause: str) -> tuple[list[str], str]:
    """Parse a bare `forall <vars>: BODY` clause (no `in 0..` range).

    Returns (vars_, body). Used by `_replace_creusot_forall` when the range
    form is absent, e.g. `forall label: self.contains(label) == ...` --
    quantifying over a domain value rather than an index range.
    """
    colon = _find_top_level(clause, ":")
    if colon < 0:
        raise SpecError(f"bare forall clause missing ':' in Creusot logic: {clause}")
    head = clause[:colon].strip()
    body = clause[colon + 1 :].strip()
    where_idx = _find_top_level(head, " where ")
    if where_idx >= 0:
        head = head[:where_idx].strip()
    vars_ = [var.strip() for var in head.split(",") if var.strip()]
    if not vars_:
        raise SpecError(f"bare forall clause has no binders: {clause}")
    for var in vars_:
        if not re.fullmatch(r"[A-Za-z_]\w*", var):
            raise SpecError(f"bare forall binder is not a plain identifier: {var!r}")
    return vars_, body


def _replace_creusot_forall(src: str) -> str:
    match = re.search(r"\bforall\b", src)
    if not match:
        return src
    prefix = src[: match.start()]
    clause = src[match.end() :].strip()
    antecedent: str | None
    try:
        vars_, end_expr, guard, body = _parse_range_clause(clause)
        bounds: list[str] = []
        for var in vars_:
            bounds.extend([f"0 <= {var}", f"{var} < {end_expr}"])
        if guard:
            bounds.append(guard)
        antecedent = " && ".join(bounds)
    except SpecError:
        try:
            vars_, body = _parse_bare_creusot_quantifier_clause(clause)
        except SpecError:
            return "true /* TODO(concept-to-code): unsupported forall clause needs Pearlite translation */"
        antecedent = None
    if re.search(r"\bforall\b|\bAND\b|\bOR\b", body):
        return "true /* TODO(concept-to-code): unsupported forall clause needs Pearlite translation */"
    params = ", ".join(vars_)
    if antecedent is not None:
        replacement = f"forall<{params}> {antecedent} ==> {body}"
    else:
        replacement = f"forall<{params}> {body}"
    return f"{prefix}{replacement}"


def method_has_self(method: Method) -> bool:
    signature = method.rust_sig.strip().rstrip(";")
    match = QUERY_SIG_RE.match(signature) or COMMAND_SIG_RE.match(signature)
    if not match:
        raise SpecError(f"invalid method signature: {method.rust_sig!r}")
    args = match.group("args").strip()
    return args == "self" or args.startswith("self,") or args.startswith("&self") or args.startswith("&mut self")


def method_has_mut_self(method: Method) -> bool:
    signature = method.rust_sig.strip().rstrip(";")
    match = QUERY_SIG_RE.match(signature) or COMMAND_SIG_RE.match(signature)
    if not match:
        raise SpecError(f"invalid method signature: {method.rust_sig!r}")
    args = match.group("args").strip()
    return args == "&mut self" or args.startswith("&mut self,")


def method_returns_result(method: Method) -> bool:
    signature = method.rust_sig.strip().rstrip(";")
    match = QUERY_SIG_RE.match(signature) or COMMAND_SIG_RE.match(signature)
    if not match:
        raise SpecError(f"invalid method signature: {method.rust_sig!r}")
    ret = match.group("ret").strip()
    return ret.removeprefix("->").strip().startswith("Result<")


CREUSOT_USIZE_MODEL_CALLS = {
    "len",
    "size",
    "observed_count",
    "child_count",
    "parent",
    "node_count",
    "leaf_count",
    "root",
    "staged_mutation_count",
}

CREUSOT_SCOPE_RESERVED = {
    "true",
    "false",
    "Ok",
    "Err",
    "Some",
    "None",
    "Result",
    "Self",
    "usize",
    "isize",
    "f64",
    "bool",
    "_",
    # Restricted-English quantifier syntax (#458): the scope check runs on
    # `creusot_raw` before `_replace_creusot_forall` strips this syntax, so
    # these keywords must not be mistaken for free variable names.
    "forall",
    "exists",
    "in",
    "where",
}

CREUSOT_IGNORED_CALLS = {
    "is_valid_trait_name",
    "is_valid_taxon_label",
    "creusot_f64_eq",
    "creusot_f64_ge",
    "creusot_f64_gt",
    "creusot_f64_le",
    "creusot_f64_lt",
    "creusot_f64_is_finite",
    "creusot_f64_is_nan",
    "creusot_f64_lt_zero",
    "creusot_f64_le_zero",
    "creusot_f64_gt_zero",
    "creusot_f64_ge_zero",
    "creusot_f64_eq_zero",
    "match",
}


def method_arg_names(method: Method) -> set[str]:
    _name, args, _ret_ty = method_signature_parts(method.rust_sig)
    names: set[str] = set()
    if not args:
        return names
    for raw_arg in args.split(","):
        arg = raw_arg.strip()
        if arg in {"self", "&self", "&mut self"}:
            names.add("self")
            continue
        if ":" not in arg:
            continue
        name = arg.split(":", 1)[0].strip()
        name = name.removeprefix("mut ").strip()
        name = name.removeprefix("&mut ").strip()
        name = name.removeprefix("&").strip()
        if name:
            names.add(name)
    return names


def _creusot_usize_param_names(method: Method) -> frozenset[str]:
    """Return the set of parameter names that have plain usize type.

    Used to distinguish bare `index@ <` (plain usize) from `index.0@ <`
    (newtype wrappers) in Pearlite constraints.
    """
    _name, args, _ret = method_signature_parts(method.rust_sig)
    if not args:
        return frozenset()
    names: set[str] = set()
    for raw_arg in args.split(","):
        arg = raw_arg.strip()
        if ":" not in arg:
            continue
        name_part, type_part = arg.split(":", 1)
        name = name_part.strip().removeprefix("mut ").strip().removeprefix("&mut ").strip().removeprefix("&").strip()
        if type_part.strip() == "usize" and name:
            names.add(name)
    return frozenset(names)


def _creusot_domain_index_param_names(method: Method) -> frozenset[str]:
    """Return parameters whose newtype names end in `Index`."""
    _name, args, _ret = method_signature_parts(method.rust_sig)
    return frozenset(
        name
        for name, ty in parse_arg_types(args).items()
        if ty.rsplit("::", 1)[-1].endswith("Index")
    )


def creusot_free_identifiers(logic: str) -> set[str]:
    logic = re.sub(r"/\*.*?\*/", " ", logic, flags=re.DOTALL)
    logic = re.sub(r"//.*", " ", logic)
    free: set[str] = set()
    for match in re.finditer(r"\b[A-Za-z_]\w*\b", logic):
        token = match.group(0)
        if token in CREUSOT_SCOPE_RESERVED or token in CREUSOT_IGNORED_CALLS:
            continue
        if token.isupper() or token[0].isupper():
            continue
        prev = logic[match.start() - 1] if match.start() > 0 else ""
        if prev == ".":
            continue
        rest = logic[match.end() :].lstrip()
        if rest.startswith("("):
            continue
        free.add(token)
    return free


def creusot_quantifier_bound_vars(logic: str) -> set[str]:
    """Collect variable names bound by `forall`/`exists` headers in `logic` (#458).

    Runs on the restricted-English form, before `_replace_creusot_forall`
    converts headers to Pearlite, so it parses the same `<vars> in 0..<end>`
    / bare `<vars>` head shape as `_parse_range_clause` without requiring a
    range to be present (`forall label: ...` has none). Each `forall`/`exists`
    occurrence (including nested ones) is scanned independently since only
    the union of bound names is needed here, not proper lexical nesting.
    """
    bound: set[str] = set()
    for match in re.finditer(r"\b(?:forall|exists)\b", logic):
        rest = logic[match.end() :]
        colon = _find_top_level(rest, ":")
        if colon < 0:
            continue
        head = rest[:colon].strip()
        where_idx = _find_top_level(head, " where ")
        if where_idx >= 0:
            head = head[:where_idx].strip()
        vars_part = head.split(" in 0..", 1)[0] if " in 0.." in head else head
        for var in vars_part.split(","):
            var = var.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", var):
                bound.add(var)
    return bound


def creusot_constraint_in_scope(logic: str, method: Method) -> bool:
    scope = method_arg_names(method)
    if method_has_self(method):
        scope.add("self")
    # Creusot ensures bind the return value as `result` for all return
    # types, including plain values returned from &self / &mut self methods
    # (#531) -- not just Result-returning or self-less methods.
    scope.add("result")
    for binder in re.findall(r"\b(?:Ok|Some)\((\w+)\)", logic):
        scope.add(binder)
    scope |= creusot_quantifier_bound_vars(logic)
    missing = creusot_free_identifiers(logic) - scope
    return not missing


def _model_creusot_usize_calls(src: str) -> str:
    def repl(match: re.Match[str]) -> str:
        expr = match.group(0)
        name = match.group("name")
        if name not in CREUSOT_USIZE_MODEL_CALLS or expr.endswith("@"):
            return expr
        return f"{expr}@"

    pattern = r"(?:\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*\([^()]*\))*\.)?(?P<name>[A-Za-z_]\w*)\([^()]*\)(?!@)"
    out = re.sub(pattern, repl, src)
    # A bare-identifier parameter's `.len()` (e.g. a `Vec<T>`/`&[T]` param)
    # must become `param@.len()` (Seq view's len), not `param.len()@` (program
    # Vec::len/slice::len with @ on the returned usize) -- Creusot rejects
    # Vec::len/slice::len in logic context; Seq::len on the mathematical view
    # is valid Pearlite. Only applies to bare receivers, not self/result/
    # ok_result, which are already rerouted to model companions above.
    out = re.sub(
        r"\b([A-Za-z_]\w*)\.(\w+)\(\)@",
        lambda m: f"{m.group(1)}@.{m.group(2)}()" if m.group(1) not in {"self", "result", "ok_result"} else m.group(0),
        out,
    )
    out = out.replace("old(self.staged_mutation_count()@)", "old(self.staged_mutation_count())@")
    out = re.sub(r"\bROOT_PARENT_SENTINEL\b(?!@)", "ROOT_PARENT_SENTINEL@", out)
    out = re.sub(r"\bTAXON_LABEL_MAX_LEN\b(?!@)", "TAXON_LABEL_MAX_LEN@", out)
    out = re.sub(r"\busize::MAX\b(?!@)", "usize::MAX@", out)
    out = re.sub(r"\bindex\s*<", "index.0@ <", out)
    out = re.sub(r"\bnode\s*<", "node@ <", out)
    out = re.sub(r"\bnode\s*==", "node@ ==", out)
    return out


def _replace_creusot_f64_predicates(src: str) -> str:
    atom = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*\([^()]*\))*"
    out = re.sub(rf"({atom})\.is_finite\(\)", rf"{CONTRACTS_CREUSOT}::creusot_f64_is_finite(\1)", src)
    out = re.sub(rf"({atom})\.is_nan\(\)", rf"{CONTRACTS_CREUSOT}::creusot_f64_is_nan(\1)", out)
    return out


def _replace_creusot_f64_comparisons(src: str) -> str:
    funcs = {
        "<": "creusot_f64_lt",
        "<=": "creusot_f64_le",
        ">": "creusot_f64_gt",
        ">=": "creusot_f64_ge",
        "==": "creusot_f64_eq",
    }
    f64_expr = (
        r"(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*\([^()]*\))*\.)?"
        r"(?:branch_length|branch_length_ceiling|weight)\(\)"
        r"|old\(self\.weight\(\)\)"
        r"|length"
        r"|branch_length"
    )
    lit = r"-?\d+\.\d+"

    def repl(match: re.Match[str]) -> str:
        left, op, right = match.group(1), match.group(2), match.group(3)
        if right == "0.0":
            zero_funcs = {
                "<": "creusot_f64_lt_zero",
                "<=": "creusot_f64_le_zero",
                ">": "creusot_f64_gt_zero",
                ">=": "creusot_f64_ge_zero",
                "==": "creusot_f64_eq_zero",
            }
            return f"{CONTRACTS_CREUSOT}::{zero_funcs[op]}({left})"
        return f"{CONTRACTS_CREUSOT}::{funcs[op]}({left}, {right})"

    out = re.sub(rf"(?<!\.)\b({f64_expr})\s*(<=|>=|==|<|>)\s*({lit}|{f64_expr})", repl, src)
    return out

def rewrite_result_methods_for_creusot(logic: str, method: Method) -> str:
    if not method_returns_result(method):
        return logic
    # Rewrite `result.as_ref().unwrap().<expr>` (which Creusot rejects in logic
    # context -- as_ref/unwrap are program functions, not logic functions) to
    # Pearlite's match form: `match result { Ok(ok_result) => <expr with
    # ok_result.>, Err(_) => true }`. Also strips a leading `result.is_ok()
    # ==>` guard since the match already handles the Err case.
    if re.search(r"\bresult\.as_ref\(\)\.unwrap\(\)\.", logic):
        body = re.sub(r"\bresult\.as_ref\(\)\.unwrap\(\)\.", "ok_result.", logic)
        body = re.sub(r"^result\.is_ok\(\)\s*==>\s*", "", body)
        return f"match result {{ Ok(ok_result) => {body}, Err(_) => true }}"
    # Rewrite bare `result.is_ok() ==> <body>` and `result.is_err() ==> <body>`
    # guards to match form, since is_ok/is_err are program functions rejected
    # by Creusot in logic context.
    is_ok_match = re.search(r"^result\.is_ok\(\)\s*==>\s*(.+)$", logic)
    if is_ok_match:
        return f"match result {{ Ok(_) => {is_ok_match.group(1)}, Err(_) => true }}"
    is_err_match = re.search(r"^result\.is_err\(\)\s*==>\s*(.+)$", logic)
    if is_err_match:
        return f"match result {{ Ok(_) => true, Err(_) => {is_err_match.group(1)} }}"
    # Rewrite bare result.is_ok()/is_err() calls appearing inside a larger
    # expression (equality, conjunction, ...) to match-expression form too.
    logic = re.sub(
        r"\bresult\.is_ok\(\)",
        "(match result { Ok(_) => true, Err(_) => false })",
        logic,
    )
    logic = re.sub(
        r"\bresult\.is_err\(\)",
        "(match result { Ok(_) => false, Err(_) => true })",
        logic,
    )
    if not re.search(r"\bresult\.(?!as_ref\(|unwrap\(|expect\()\w+\(", logic):
        return logic
    rewritten = re.sub(r"\bresult\.", "ok_result.", logic)
    return f"match result {{ Ok(ok_result) => {rewritten}, Err(_) => true }}"


def _rewrite_mut_self_prophecy_for_creusot(logic: str) -> str:
    """Rewrite &mut self postcondition to Pearlite prophecy notation (#348).

    For fn method(&mut self) ensures clauses, Creusot represents the pre-call
    receiver as *self and the post-call receiver as ^self.  old(self.X(args))
    maps to (*self).X(args); bare self.X(args) maps to (^self).X(args).

    The old() rewrite matches the full old(self.X(args)) including its closing )
    so no dangling paren is left in the output.  args must have no nested parens
    (sufficient for constraints seen so far).
    """
    # Step 1: old(self.X(args)) -> (*self).X(args)   [pre-state deref]
    logic = re.sub(r"\bold\(self\.(\w+\([^()]*\))\)", r"(*self).\1", logic)
    # Step 2: remaining self.X(args) -> (^self).X(args)   [prophecy / post-state]
    logic = re.sub(r"\bself\.(\w+\([^()]*\))", r"(^self).\1", logic)
    return logic


def rewrite_logic_for_creusot(logic: str, method: Method, methods: list[Method]) -> str:
    logic = rewrite_static_self_for_creusot(logic, method)
    logic = rewrite_string_view_mismatches_for_creusot(logic, method, methods)
    logic = rewrite_result_methods_for_creusot(logic, method)
    return logic


CREUSOT_INT_RETURN_TYPES = {
    "usize", "u64", "u32", "u16", "u8", "i64", "i32", "isize", "i16", "i8",
}


def creusot_logic_type(ret_ty: str) -> str | None:
    """Creusot logic type for a query return type, or None if not yet modelable.

    Integer returns map to the mathematical `Int`; `bool` maps to `bool`.
    Reference/Option/string returns (`&str`, `&T`, `Option<_>` other than
    `Option<f64>`) have no logic model yet (sequence/string models are
    follow-on work), so they return None and their constraints keep a
    targeted sentinel.
    """
    stripped = ret_ty.strip().removeprefix("->").strip()
    if stripped in CREUSOT_INT_RETURN_TYPES:
        return "Int"
    if stripped == "bool":
        return "bool"
    if stripped == "f64":
        return "f64"
    if stripped == "Option<f64>":
        return "Option<f64>"
    return None


def creusot_query_model_companions(methods: list[Method]) -> dict[str, str]:
    """Map modelable queries to a `<name>_model` logic companion."""
    companions: dict[str, str] = {}
    for method in methods:
        if method.kind != "query":
            continue
        name, args, ret_ty = method_signature_parts(method.rust_sig)
        if "&mut" in args:
            continue
        if creusot_logic_type(ret_ty) is None:
            continue
        companions[name] = f"{name}_model"
    return companions


def _creusot_model_args(args: str) -> str:
    """Query args with the receiver taken by value (`self`) for the logic model."""
    stripped = args.strip()
    if stripped.startswith("&self"):
        stripped = "self" + stripped[len("&self"):]
    # Logic-model indices inhabit Pearlite's mathematical integer domain. This
    # lets quantified Int binders call the companion without an impossible
    # conversion back to an executable usize/domain index.
    return re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:usize|[A-Za-z_][A-Za-z0-9_]*Index)\b",
        r"\1: Int",
        stripped,
    )


def emit_creusot_query_model_companion(
    lines: list[str], method: Method, model_name: str, *, pub: bool = True
) -> None:
    """Emit a trusted opaque Pearlite logic model for a query method so contracts
    can reference `self.<query>_model()` instead of the program method, which
    Creusot rejects in logic context.

    The body is a fixed placeholder value keyed only by the query's return
    type (`defaults` below) -- it never reads concrete fields, so it is
    equally valid as a default-bodied trait method (`pub=False`, kind='trait')
    inherited by every implementor, not just an inherent impl method on one
    concrete type."""
    _name, args, ret_ty = method_signature_parts(method.rust_sig)
    logic_ty = creusot_logic_type(ret_ty)
    if logic_ty is None:
        raise SpecError(f"unsupported Creusot logic model return type: {ret_ty}")
    defaults = {"Int": "0", "bool": "true", "f64": "0.0f64", "Option<f64>": "None"}
    default = defaults[logic_ty]
    qualifier = "pub " if pub else ""
    lines.append("    #[cfg(creusot)]")
    lines.append("    #[trusted]")
    lines.append("    #[logic(opaque)]")
    lines.append(
        f"    {qualifier}fn {model_name}({_creusot_model_args(args)}) -> {logic_ty} {{ pearlite! {{ {default} }} }}"
    )


def creusot_logic_unmodeled(rerouted: str, model_names: set[str]) -> bool:
    """Return True when rerouted logic still calls non-logic program methods.

    Modelable queries are replaced with emitted `*_model` logic functions before
    this check. Creusot f64 helper calls are trusted logic functions too, so they
    are allowed here. Any remaining `.<m>(` call is treated as a program method
    and keeps a targeted sentinel. old()/final() on &mut self ensures are
    rewritten to (*self).X()/(^self).X() by _rewrite_mut_self_prophecy_for_creusot
    before this check (#348); remaining old()/final() in other positions still
    sentinel. This replaces the older, coarser creusot_needs_query_model."""
    if re.search(r"\b(old|final)\(", rerouted):
        return True
    helper_names = CREUSOT_IGNORED_CALLS | model_names
    for match in re.finditer(r"(?<!::)\.(\w+)\(", rerouted):
        if match.group(1) not in helper_names:
            return True
    return False


def creusot_chain_model_companions(
    methods: list[Method], spec: dict[str, Any]
) -> dict[tuple[str, str], str]:
    """Map a chained Int-terminal call `self.<q>(args).<term>()` -- where <q> is a
    query and <term> is in CREUSOT_USIZE_MODEL_CALLS (len/size/...) -- to a
    `<q>_<term>_model` logic companion returning Int. Covers reference-returning
    queries and string `.len()` that the per-query integer models alone cannot
    reach. See #349."""
    query_names = {m.name for m in methods if m.kind == "query"}
    out: dict[tuple[str, str], str] = {}
    for constraint in spec.get("constraints", []):
        for mt in re.finditer(r"\bself\.(\w+)\([^()]*\)\.(\w+)\(\)", constraint.get("logic", "")):
            q, term = mt.group(1), mt.group(2)
            if q in query_names and term in CREUSOT_USIZE_MODEL_CALLS:
                out[(q, term)] = f"{q}_{term}_model"
    return out


def emit_creusot_chain_model_companion(
    lines: list[str], method: Method, model_name: str, *, pub: bool = True
) -> None:
    """Emit an Int logic model for a chained `self.<q>(args).<term>()` call (#349).

    Same trusted/opaque, field-independent shape as
    `emit_creusot_query_model_companion` -- see its docstring for why
    `pub=False` (default trait method, kind='trait') is valid."""
    _name, args, _ret = method_signature_parts(method.rust_sig)
    model_args = _creusot_model_args(args)
    # Quantifier binders are Pearlite Int values.  Chained terminal models are
    # logical projections, not calls to the program query, so index arguments
    # must use the same logical domain.  Keeping usize or a domain newtype here
    # makes every `forall<i: Int> ... model(i)` ill-typed.
    model_args = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:usize|[A-Za-z_][A-Za-z0-9_]*Index)\b",
        r"\1: Int",
        model_args,
    )
    qualifier = "pub " if pub else ""
    lines.append("    #[cfg(creusot)]")
    lines.append("    #[trusted]")
    lines.append("    #[logic(opaque)]")
    lines.append(
        f"    {qualifier}fn {model_name}({model_args}) -> Int {{ pearlite! {{ 0 }} }}"
    )


def _replace_chain_calls(expr: str, chain_map: dict[tuple[str, str], str]) -> str:
    """Rewrite `self.<q>(args).<term>()` -> `self.<q>_<term>_model(args)` (#349).

    Also handles `ok_result.<q>(args).<term>()`, the receiver
    `rewrite_static_self_for_creusot`'s match-result form produces for
    self-less Result-returning constructors."""
    out = expr
    for (q, term), model_name in sorted(
        chain_map.items(), key=lambda kv: len(kv[0][0]), reverse=True
    ):
        out = re.sub(
            rf"\b(self|result|old\(self\)|final\(self\)|ok_result)\.{re.escape(q)}\(([^()]*)\)\.{re.escape(term)}\(\)",
            rf"\1.{model_name}(\2)",
            out,
        )
    return out


def _replace_creusot_option_f64_equalities(src: str, names: set[str]) -> str:
    """Route equality between known Option<f64> values through the facade."""
    if not names:
        return src
    atom = r"(?:self|old\(self\)|final\(self\)|result)\.[A-Za-z_]\w*\(\)|[A-Za-z_]\w*"

    def is_option(expr: str) -> bool:
        terminal = re.search(r"([A-Za-z_]\w*)(?:\(\))?$", expr)
        return terminal is not None and terminal.group(1) in names

    def repl(match: re.Match[str]) -> str:
        left, op, right = match.group(1), match.group(2), match.group(3)
        if not (is_option(left) and is_option(right)):
            return match.group(0)
        call = f"{CONTRACTS_CREUSOT}::creusot_option_f64_eq({left}, {right})"
        return f"!{call}" if op == "!=" else call

    return re.sub(rf"(?<![\w.])({atom})\s*(==|!=)\s*({atom})", repl, src)


def _replace_creusot_option_predicates(src: str) -> str:
    """Rewrite `expr.is_some()`/`expr.is_none()` to `expr != None`/`expr == None`.

    Pearlite's `Option::is_some`/`is_none` are program methods, not logic
    functions; calling them in a contract triggers the unmodeled-call
    sentinel. `Option<Int>` (and `Option<f64>` after the opaque-helper path)
    supports direct `==`/`!=` against `None` in Pearlite, so this rewrite
    keeps the constraint semantic without an extra helper.
    """
    atom = r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:\.[A-Za-z_]\w*\([^()]*\))*"
    src = re.sub(rf"\b({atom})\.is_some\(\)", r"(\1 != None)", src)
    src = re.sub(rf"\b({atom})\.is_none\(\)", r"(\1 == None)", src)
    return src


def translate_logic_to_creusot(logic: str) -> str:
    """Translate restricted-English `logic` to Pearlite-compatible syntax.

    This is intentionally conservative. Unsupported aggregates degrade to a
    visible sentinel so the Real gate can proceed while preserving a follow-up
    marker for the missing proof expression.
    """
    src = logic.strip()
    if not src:
        return src
    if re.search(r"\.chars\(\)\.any\s*\(", src):
        return "true /* TODO(concept-to-code): string closure predicate needs Pearlite model */"
    if re.search(r"\bcount\s*\(", src):
        return "true /* TODO(concept-to-code): count aggregate needs Pearlite translation */"
    if re.search(r"\bforall\b", src):
        return _replace_creusot_forall(src)
    src = re.sub(r"\bimplies\b", "==>", src)
    src = _replace_creusot_option_predicates(src)
    src = _replace_creusot_f64_predicates(src)
    src = _replace_creusot_f64_comparisons(src)
    src = _model_creusot_usize_calls(src)
    return src


def rewrite_static_self_for_creusot(logic: str, method: Method) -> str:
    if method_has_self(method) or not re.search(r"\bself\.", logic):
        return logic
    if method_returns_result(method):
        # Creusot rejects `result.as_ref().unwrap().<method>()` in logic
        # context (as_ref/unwrap are program functions). Use Pearlite's match
        # form instead: `match result { Ok(ok_result) => <body>, Err(_) =>
        # true }`. The `ok_result.` receiver is then rerouted to model
        # companions by the standard _replace_query_calls/_replace_chain_calls
        # pipeline.
        rewritten = re.sub(r"\bself\.", "ok_result.", logic)
        return f"match result {{ Ok(ok_result) => {rewritten}, Err(_) => true }}"
    return re.sub(r"\bself\.", "result.", logic)


def translate_logic_to_kani(logic: str) -> str:
    """Translate restricted-English `logic` to plain Rust bool for kani::requires/ensures.

    Kani attribute macros take standard Rust boolean expressions -- no
    implication operator, no quantifiers. Unsupported forms degrade to a
    true-sentinel so contract emission can proceed without a working
    quantifier model.
    """
    src = logic.strip()
    if not src:
        return src
    # forall/exists/sum: no Kani attr-level equivalent; sentinel.
    if re.search(r"\bforall\b|\bexists\b|\bsum\b", src):
        return "true /* TODO(concept-to-code): quantifier unsupported in kani::requires/ensures */"
    # ==>: rewrite material implication a ==> b as !(a) || (b).
    # _find_top_level respects parenthesis nesting so complex lhs/rhs are handled.
    top = _find_top_level(src, "==>")
    if top >= 0:
        lhs = src[:top].strip()
        rhs = src[top + 3:].strip()
        return f"!({lhs}) || ({rhs})"
    return src


def rewrite_static_self_for_kani(logic: str, method: Method) -> str:
    """For self-less constructors, rewrite self -> result inside kani::ensures closures.

    Handles both `self.foo()` (method-call form) and bare `self` (deref form, e.g. `== *self`).
    The dot-terminated sub runs first so `self.` is consumed before the bare-`self` sub fires.
    """
    if method_has_self(method) or not re.search(r"\bself\b", logic):
        return logic
    if method_returns_result(method):
        rewritten = re.sub(r"\bself\.", "result.as_ref().unwrap().", logic)
        # Replace bare `self` (e.g. the `self` in `*self`) -- the `*` is already in the
        # source expression, so just swap the identifier without adding another dereference.
        rewritten = re.sub(r"\bself\b", "result.as_ref().unwrap()", rewritten)
        return f"result.is_ok() && {rewritten}"
    rewritten = re.sub(r"\bself\.", "result.", logic)
    rewritten = re.sub(r"\bself\b", "result", rewritten)
    return rewritten


def parse_arg_types(args: str) -> dict[str, str]:
    """Map parameter name -> declared type for a method's `rust_sig` args."""
    types: dict[str, str] = {}
    if not args:
        return types
    for raw_arg in args.split(","):
        arg = raw_arg.strip()
        if not arg or arg in {"self", "&self", "&mut self"} or ":" not in arg:
            continue
        name, ty = arg.split(":", 1)
        types[name.strip().removeprefix("mut ").strip()] = ty.strip()
    return types


def creusot_query_return_types(methods: list[Method]) -> dict[str, str]:
    """Map query method name -> declared return type."""
    types: dict[str, str] = {}
    for candidate in methods:
        if candidate.kind != "query":
            continue
        _name, _args, ret_ty = method_signature_parts(candidate.rust_sig)
        types[candidate.name] = ret_ty
    return types


def rewrite_string_view_mismatches_for_creusot(logic: str, method: Method, methods: list[Method]) -> str:
    """Fix `result.<getter>() == <param>` where the getter returns a borrowed
    view (`&str`/`Option<&str>`) of a constructor parameter declared as the
    owned type (`String`/`Option<String>`), which Creusot's `equal::<T>`
    rejects as a type mismatch."""
    param_types = parse_arg_types(method_signature_parts(method.rust_sig)[1])
    query_types = creusot_query_return_types(methods)

    def repl(match: re.Match[str]) -> str:
        getter, op, rhs = match["getter"], match["op"], match["rhs"]
        getter_ty = query_types.get(getter)
        rhs_ty = param_types.get(rhs)
        if getter_ty == "&str" and rhs_ty == "String":
            return f"result.{getter}() {op} {rhs}.as_str()"
        if getter_ty == "Option<&str>" and rhs_ty == "Option<String>":
            return f"result.{getter}() {op} {rhs}.as_deref()"
        return match[0]

    pattern = r"result\.(?P<getter>[A-Za-z_]\w*)\(\)\s*(?P<op>==|!=)\s*(?P<rhs>[A-Za-z_]\w*)\b"
    return re.sub(pattern, repl, logic)


def method_signature_parts(signature: str) -> tuple[str, str, str]:
    stripped = signature.strip().rstrip(";")
    match = COMMAND_SIG_RE.match(stripped) or QUERY_SIG_RE.match(stripped)
    if not match:
        raise SpecError(f"invalid method signature: {signature!r}")
    args = match.group("args").strip()
    ret = match.group("ret").strip()
    ret_ty = ret.removeprefix("->").strip() if ret else "()"
    return match.group("name"), args, ret_ty


def verus_spec_default_expr(ret_ty: str) -> str | None:
    if ret_ty == "()":
        return "()"
    if ret_ty == "bool":
        return "false"
    if ret_ty == "f64":
        return "0.0f64"
    if ret_ty in {"usize", "u64", "u32", "u16", "u8"}:
        return f"0{ret_ty}"
    if ret_ty in {"isize", "i64", "i32", "i16", "i8"}:
        return f"0{ret_ty}"
    if ret_ty.startswith("Option<"):
        return "None"
    return None


def verus_query_spec_companions(methods: list[Method]) -> dict[str, str]:
    companions: dict[str, str] = {}
    for method in methods:
        if method.kind != "query":
            continue
        name, args, ret_ty = method_signature_parts(method.rust_sig)
        if "&mut" in args or verus_spec_default_expr(ret_ty) is None:
            continue
        companions[name] = f"{name}_spec"
    return companions


def is_verus_slice_type(ret_ty: str) -> bool:
    stripped = ret_ty.strip()
    return stripped.startswith("&[") and stripped.endswith("]")


def verus_query_len_spec_companions(methods: list[Method]) -> dict[str, str]:
    """Map slice-returning queries (e.g. `&[f64]`) to a `*_len_spec` companion.

    Verus cannot call an exec-mode query like `evec()` from inside a `requires`/
    `ensures` clause, so `X.evec().len()` has no legal spec-mode translation.
    These companions give `.len()` comparisons a callable spec function.
    """
    companions: dict[str, str] = {}
    for method in methods:
        if method.kind != "query":
            continue
        name, args, ret_ty = method_signature_parts(method.rust_sig)
        if "&mut" in args or not is_verus_slice_type(ret_ty):
            continue
        companions[name] = f"{name}_len_spec"
    return companions


def referenced_verus_constants(spec: dict[str, Any]) -> list[str]:
    """Return all-uppercase logic constants that native cargo-verus must see.

    The older wrapper gate synthesized these locally. Native cargo-verus checks
    the generated crate module directly, so the generated module needs the same
    placeholder constants until implementation code provides real model values.
    """
    names: set[str] = set()
    for constraint in spec.get("constraints", []):
        logic = constraint.get("logic", "")
        names.update(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", logic))
    return sorted(names)


def referenced_verus_error_types(methods: list[Method]) -> list[str]:
    names: set[str] = set()
    for method in methods:
        names.update(re.findall(r"\b([A-Z][A-Za-z0-9_]*Error)\b", method.rust_sig))
    return sorted(names)


def emit_verus_query_spec_companion(lines: list[str], method: Method, spec_name: str) -> None:
    _name, args, ret_ty = method_signature_parts(method.rust_sig)
    default = verus_spec_default_expr(ret_ty)
    if default is None:
        raise SpecError(f"unsupported Verus spec companion return type: {ret_ty}")
    lines.append(f"    pub open spec fn {spec_name}({args}) -> {ret_ty} {{ {default} }}")


def emit_verus_query_len_spec_companion(lines: list[str], method: Method, spec_name: str) -> None:
    _name, args, _ret_ty = method_signature_parts(method.rust_sig)
    lines.append(f"    pub open spec fn {spec_name}({args}) -> usize {{ 0 }}")


def _replace_query_calls(expr: str, query_spec_map: dict[str, str]) -> str:
    """Reroute `self|result|old(self)|final(self)|ok_result.<q>(` to its model.

    `ok_result` is the receiver `rewrite_static_self_for_creusot`'s
    match-result form produces for self-less Result-returning constructors."""
    out = expr
    for query_name, spec_name in sorted(query_spec_map.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(rf"\b(self|old\(self\)|final\(self\)|result|ok_result)\.{re.escape(query_name)}\(", rf"\1.{spec_name}(", out)
    return out


def _replace_slice_len_calls(expr: str, len_spec_map: dict[str, str]) -> str:
    out = expr
    for query_name, spec_name in sorted(len_spec_map.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(
            rf"\b(self|old\(self\)|final\(self\)|result)\.{re.escape(query_name)}\(\)\.len\(\)",
            rf"\1.{spec_name}()",
            out,
        )
    return out


def verus_contract_groups(
    spec: dict[str, Any],
    method: Method,
    query_spec_map: dict[str, str] | None = None,
    len_spec_map: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    requires: list[str] = []
    ensures: list[str] = []
    final_mut_refs = mutable_ref_args(method)
    for constraint in applicable_constraints(spec, method):
        logic = constraint.get("logic", "").strip()
        if not logic:
            continue
        kind = constraint.get("kind", "invariant")
        if kind == "postcondition":
            if method_has_mut_self(method):
                wrapped_logic = _wrap_final_self(logic)
            elif not method_has_self(method):
                # Static constructors have no `self` parameter; postconditions
                # describing the constructed value must refer to `result`.
                wrapped_logic = re.sub(r"\bself\.", "result.", logic)
            else:
                wrapped_logic = logic
            if len_spec_map:
                wrapped_logic = _replace_slice_len_calls(wrapped_logic, len_spec_map)
            ensures.append(translate_logic_to_verus(wrapped_logic, final_mut_refs, query_spec_map))
        else:
            logic_for_translation = _replace_slice_len_calls(logic, len_spec_map) if len_spec_map else logic
            requires.append(translate_logic_to_verus(logic_for_translation, query_spec_map=query_spec_map))
    return requires, ensures


def creusot_signature_variant(signature: str) -> str | None:
    if "&mut dyn RngCore" not in signature:
        return None
    return signature.replace("&mut dyn RngCore", "&mut CreusotRngCore")


def emit_method_body(lines: list[str], signature: str, *, pub: bool = True, body: bool = True) -> None:
    """Emit one method. `pub=False` for trait-impl/trait-declaration methods,
    which are never individually `pub`-qualified in Rust regardless of the
    surrounding impl/trait's own visibility. `body=False` emits a bodyless
    trait method declaration (`;`) instead of an `unimplemented!()` body."""
    qualifier = "pub " if pub else ""
    if not body:
        lines.append(f"    {qualifier}{signature};")
        return
    lines.append(f"    {qualifier}{signature} {{")
    lines.append("        unimplemented!()")
    lines.append("    }")


def compute_creusot_maps(
    spec: dict[str, Any], methods: list[Method]
) -> tuple[dict[str, str], dict[tuple[str, str], str], dict[str, Method], set[str]]:
    """Precompute the Creusot logic-model companion maps shared by every
    emission path (inherent impl, trait impl, trait declaration) for a
    verifier=creusot spec. Empty maps for non-creusot specs and for
    kind='trait' (a trait has no concrete state to model a companion over --
    constraints needing one fall back to the sentinel, same as an unmodeled
    query on a concrete concept today)."""
    creusot_model_map = (
        creusot_query_model_companions(methods)
        if spec.get("verifier") == "creusot"
        else {}
    )
    creusot_chain_map = (
        creusot_chain_model_companions(methods, spec)
        if spec.get("verifier") == "creusot"
        else {}
    )
    query_by_name = {m.name: m for m in methods if m.kind == "query"}
    creusot_option_f64_names = {
        creusot_model_map[m.name]
        for m in methods
        if m.name in creusot_model_map
        and method_signature_parts(m.rust_sig)[2].replace(" ", "") == "Option<f64>"
    }
    return creusot_model_map, creusot_chain_map, query_by_name, creusot_option_f64_names


def emit_method_contract_attrs(
    lines: list[str],
    spec: dict[str, Any],
    method: Method,
    methods: list[Method],
    creusot_model_map: dict[str, str],
    creusot_chain_map: dict[tuple[str, str], str],
    creusot_option_f64_names: set[str],
) -> None:
    """Emit `#[cfg_attr(kani|creusot, requires/ensures(...))]` attributes for
    one method's applicable constraints. Shared by inherent-impl emission,
    trait-impl emission (where it is simply not called -- see
    `emit_plain_impl`), and trait-declaration emission (`emit_trait_def`).

    A no-op for verifier=="verus": `emit_verus_impl` calls `emit_plain_impl`
    a second time (gated by `cfg(not(verus))`) as the non-verus fallback
    impl, which must carry no contract attributes at all -- verus is the
    concept's sole verifier and the raw restricted-English `logic` isn't
    valid Rust for kani::requires without translation. For verifier in
    {"kani", "creusot"}, both attribute forms are emitted regardless of
    which one is the spec's declared primary verifier -- gated by
    `cfg_attr` so only the tool actually invoked (`--cfg kani` / `--cfg
    creusot`) sees its own attribute. This lets the same generated stub be
    checked under either verifier, not just the declared one."""
    if spec["verifier"] == "verus":
        return
    for constraint in applicable_constraints(spec, method):
        logic = constraint.get("logic", "").strip()
        if not logic:
            continue

        kani_raw = rewrite_static_self_for_kani(logic, method)
        kani_logic = translate_logic_to_kani(kani_raw)
        # Evaluate is_post AFTER rewrite: invariants on static methods become
        # postconditions once self.* is rewritten to result.* (mirrors the
        # analogous Creusot rewrite below).
        is_post = constraint.get("kind") == "postcondition" or bool(re.search(r"\bresult\b", kani_logic))
        if is_post:
            lines.append(f"    #[cfg_attr(kani, kani::ensures(|result| {kani_logic}))]")
        else:
            lines.append(f"    #[cfg_attr(kani, kani::requires({kani_logic}))]")

        creusot_raw = rewrite_logic_for_creusot(logic, method, methods)
        if creusot_constraint_in_scope(creusot_raw, method):
            creusot_attr = "ensures" if constraint.get("kind") == "postcondition" or re.search(r"\bresult\b", creusot_raw) else "requires"
            # Reroute integer/bool/f64 query calls to their #[logic] model
            # companions so the constraint is real Pearlite, not a `true`
            # sentinel. A constraint still referencing an unmodeled query
            # (string/reference returns) keeps a targeted sentinel.
            # Rewrite chained Int-terminal calls (self.q(args).len()/.size())
            # to their models first (#349), then the per-query int/bool models.
            rerouted = _replace_chain_calls(creusot_raw, creusot_chain_map)
            rerouted = _replace_query_calls(rerouted, creusot_model_map)
            _method_name, method_args, _method_ret = method_signature_parts(method.rust_sig)
            option_names = set(creusot_option_f64_names)
            option_names.update(
                name
                for name, ty in parse_arg_types(method_args).items()
                if ty.replace(" ", "") == "Option<f64>"
            )
            rerouted = _replace_creusot_option_f64_equalities(rerouted, option_names)
            # For &mut self postconditions, rewrite old(self.X()) ->
            # (*self).X() and self.X() -> (^self).X() so Creusot sees
            # Pearlite prophecy notation instead of old() (#348).
            if method_has_mut_self(method) and creusot_attr == "ensures":
                rerouted = _rewrite_mut_self_prophecy_for_creusot(rerouted)
            translated = translate_logic_to_creusot(rerouted)
            # Fix usize parameter references in Pearlite logic context.
            # index.0@ is for newtype wrappers; plain usize params use index@.
            usize_params = _creusot_usize_param_names(method)
            if "index.0@ <" in translated and "index" in usize_params:
                translated = translated.replace("index.0@ <", "index@ <")
            # Executable usize parameters need their mathematical view in
            # Pearlite, including when passed to an Int model companion.
            for _uname in usize_params:
                translated = re.sub(
                    rf"\b{re.escape(_uname)}\b(?![@(])", f"{_uname}@", translated
                )
            # Domain index newtypes expose their inner usize before
            # taking the mathematical view expected by Int models.
            for _iname in _creusot_domain_index_param_names(method):
                translated = re.sub(
                    rf"\b{re.escape(_iname)}\b(?![.@(])", f"{_iname}.0@", translated
                )
            all_models = set(creusot_model_map.values()) | set(creusot_chain_map.values())
            if creusot_logic_unmodeled(translated, all_models):
                creusot_logic = "true /* TODO(concept-to-code): generated query method needs Pearlite model helper */"
            else:
                creusot_logic = translated
            lines.append(
                "    #[cfg_attr(creusot, "
                f"{CONTRACTS_CREUSOT}::{creusot_attr}({creusot_logic}))]"
            )


def _emit_impl_method(
    lines: list[str],
    spec: dict[str, Any],
    method: Method,
    methods: list[Method],
    creusot_model_map: dict[str, str],
    creusot_chain_map: dict[tuple[str, str], str],
    creusot_option_f64_names: set[str],
    *,
    emit_contracts: bool,
    pub: bool,
) -> None:
    """Emit one method inside an impl block (inherent or `impl Trait for X`).

    `emit_contracts=False` is used for trait-satisfying methods (`implements`):
    Creusot checks contract refinement from the trait's own declaration
    automatically, so restating `#[cfg_attr(creusot, ...)]` here would be
    redundant at best and risk drifting from the trait's contract at worst.
    `pub=False` matches Rust's own rule that `impl Trait for X` methods are
    never individually `pub`-qualified."""
    lines.append("")
    lines.extend(f"    {line}" for line in doc_lines(method.english))
    if emit_contracts:
        emit_method_contract_attrs(
            lines, spec, method, methods, creusot_model_map, creusot_chain_map, creusot_option_f64_names
        )
    creusot_sig = creusot_signature_variant(method.rust_sig) if spec["verifier"] == "creusot" else None
    if creusot_sig is not None:
        lines.append("    #[cfg(creusot)]")
        emit_method_body(lines, creusot_sig, pub=pub)
        lines.append("    #[cfg(not(creusot))]")
        emit_method_body(lines, method.rust_sig, pub=pub)
    else:
        emit_method_body(lines, method.rust_sig, pub=pub)


def emit_plain_impl(lines: list[str], spec: dict[str, Any], methods: list[Method], *, cfg: str | None = None) -> None:
    concept = spec["concept"]
    creusot_model_map, creusot_chain_map, query_by_name, creusot_option_f64_names = compute_creusot_maps(spec, methods)
    implements: dict[str, dict[str, Any]] = spec.get("implements", {})
    trait_method_names: set[str] = {
        name for trait_info in implements.values() for name in trait_info["methods"]
    }
    inherent_methods = [m for m in methods if m.name not in trait_method_names]

    if cfg:
        lines.append(cfg)
    lines.extend(doc_lines(spec["english_description"]))
    lines.append(f"pub struct {concept};")
    lines.append("")

    # `implements`: each named trait gets its own `impl Trait for Concept`
    # block, methods routed there instead of the inherent impl, with no
    # contract attributes -- Creusot's refinement checking reads the
    # contract from the trait's own declaration.
    for trait_name, trait_info in implements.items():
        if cfg:
            lines.append(cfg)
        lines.append(f"impl {trait_name} for {concept} {{")
        for method_name in trait_info["methods"]:
            method = next(m for m in methods if m.name == method_name)
            _emit_impl_method(
                lines, spec, method, methods,
                creusot_model_map, creusot_chain_map, creusot_option_f64_names,
                emit_contracts=False, pub=False,
            )
        lines.append("}")
        lines.append("")

    if cfg:
        lines.append(cfg)
    lines.append(f"impl {concept} {{")
    for method in inherent_methods:
        if method.name in creusot_model_map:
            lines.append("")
            emit_creusot_query_model_companion(lines, method, creusot_model_map[method.name])
    for (chain_q, _term), chain_model in creusot_chain_map.items():
        if chain_q in {m.name for m in inherent_methods}:
            lines.append("")
            emit_creusot_chain_model_companion(lines, query_by_name[chain_q], chain_model)
    for method in inherent_methods:
        _emit_impl_method(
            lines, spec, method, methods,
            creusot_model_map, creusot_chain_map, creusot_option_f64_names,
            emit_contracts=True, pub=True,
        )
    lines.append("}")


def emit_trait_def(lines: list[str], spec: dict[str, Any], methods: list[Method]) -> None:
    """Emit `pub trait {concept} { ... }` for kind='trait'.

    Method declarations are bodyless (`;`), never `pub`-qualified (trait
    methods inherit visibility from the trait item itself), and carry
    contracts directly -- concrete `impl {concept} for Concrete` blocks are
    checked for refinement against these by Creusot automatically, not
    restated.

    Creusot logic-model companions ARE emitted here, as default-bodied trait
    methods (`pub=False`, same as the bodyless declarations -- Rust forbids
    individually `pub`-qualifying any trait item regardless of whether it has
    a body). Their bodies are trusted/opaque placeholder values keyed only by
    the query's return type, never concrete fields, so they are exactly as
    valid as a trait default as they are as an inherent impl method, and
    every implementor inherits the same one instead of each restating an
    identical copy. Omitting these silently downgrades working contracts to
    sentinels for no real reason, since the companion never depended on
    concrete state to begin with (see beast-rs's T-476 findings for the
    regression this caught in the original upstream implementation)."""
    concept = spec["concept"]
    creusot_model_map, creusot_chain_map, query_by_name, creusot_option_f64_names = compute_creusot_maps(spec, methods)
    lines.extend(doc_lines(spec["english_description"]))
    lines.append(f"pub trait {concept} {{")
    for method in methods:
        if method.name in creusot_model_map:
            lines.append("")
            emit_creusot_query_model_companion(lines, method, creusot_model_map[method.name], pub=False)
    for (chain_q, _term), chain_model in creusot_chain_map.items():
        lines.append("")
        emit_creusot_chain_model_companion(lines, query_by_name[chain_q], chain_model, pub=False)
    for method in methods:
        lines.append("")
        lines.extend(f"    {line}" for line in doc_lines(method.english))
        emit_method_contract_attrs(
            lines, spec, method, methods, creusot_model_map, creusot_chain_map, creusot_option_f64_names
        )
        # Mirrors _emit_impl_method's dual-signature handling exactly: under a
        # plain (non-creusot) build the trait's single declaration and every
        # implementor's #[cfg(not(creusot))] variant both use `dyn RngCore`,
        # so the mismatch is invisible to `cargo check`. Creusot's own build
        # activates cfg(creusot), where implementors switch to a Creusot-safe
        # signature (cargo-creusot rejects dyn trait object parameters) -- a
        # trait declaration that never splits the same way produces a genuine
        # signature mismatch the moment Creusot compiles it.
        creusot_sig = creusot_signature_variant(method.rust_sig) if spec["verifier"] == "creusot" else None
        if creusot_sig is not None:
            lines.append("    #[cfg(creusot)]")
            emit_method_body(lines, creusot_sig, pub=False, body=False)
            lines.append("    #[cfg(not(creusot))]")
            emit_method_body(lines, method.rust_sig, pub=False, body=False)
        else:
            emit_method_body(lines, method.rust_sig, pub=False, body=False)
    lines.append("}")


def emit_enum_impl(lines: list[str], spec: dict[str, Any]) -> None:
    """Emit `pub enum {concept} { Variant(Concrete), ... }` plus a
    match-dispatched inherent method per trait method, for kind='enum'. No
    `dyn` anywhere -- some deductive verifiers (e.g. Creusot) cannot verify
    trait object reasoning, so a closed enum of concrete variants is the
    composition mechanism for a heterogeneous set of concrete concepts."""
    concept = spec["concept"]
    trait_ref = spec["trait_ref"]
    trait_spec = resolve_cross_crate_spec(trait_ref["crate"], trait_ref["concept"])
    if concept_kind(trait_spec) != "trait":
        raise SpecError(
            f"trait_ref {trait_ref['crate']}::{trait_ref['concept']} is not kind='trait'"
        )
    trait_methods = validate_spec(trait_spec)
    variants = spec["variants"]

    lines.extend(doc_lines(spec["english_description"]))
    lines.append(f"pub enum {concept} {{")
    for variant in variants:
        lines.append(f"    {variant['name']}({variant['wraps']['concept']}),")
    lines.append("}")
    lines.append("")
    lines.append(f"impl {concept} {{")
    for method in trait_methods:
        name, args, ret = method_signature_parts(method.rust_sig)
        # `args` (from method_signature_parts) already includes the receiver
        # (`&self`/`&mut self`) as written in rust_sig -- reuse it verbatim
        # for the signature. `parse_arg_types` explicitly skips the receiver,
        # so its keys are exactly the arguments to forward to the concrete
        # variant's own method call.
        forward_args = ", ".join(parse_arg_types(args).keys())
        ret_part = f" -> {ret}" if ret else ""
        lines.append("")
        lines.extend(f"    {line}" for line in doc_lines(method.english))

        def emit_dispatch_body(sig_args: str) -> None:
            lines.append(f"    pub fn {name}({sig_args}){ret_part} {{")
            lines.append("        match self {")
            for variant in variants:
                lines.append(
                    f"            {concept}::{variant['name']}(op) => op.{name}({forward_args}),"
                )
            lines.append("        }")
            lines.append("    }")

        # Mirrors _emit_impl_method / emit_trait_def's dual-signature
        # handling: the enum's own dispatch method forwards its arguments
        # unchanged, so its parameter types must match whichever cfg branch
        # the variant's own method is in, or the forwarding call itself
        # becomes a type mismatch under cfg(creusot).
        creusot_args = creusot_signature_variant(args) if trait_spec["verifier"] == "creusot" else None
        if creusot_args is not None:
            lines.append("    #[cfg(creusot)]")
            emit_dispatch_body(creusot_args)
            lines.append("    #[cfg(not(creusot))]")
            emit_dispatch_body(args)
        else:
            emit_dispatch_body(args)
    lines.append("}")


def emit_verus_impl(lines: list[str], spec: dict[str, Any], methods: list[Method]) -> None:
    concept = spec["concept"]
    lines.append(VERUS_CFG)
    lines.append("verus! {")
    verus_consts = referenced_verus_constants(spec)
    for name in verus_consts:
        lines.append(f"pub const {name}: f64 = 0.000001f64;")
    verus_error_types = referenced_verus_error_types(methods)
    for name in verus_error_types:
        lines.append(f"pub enum {name} {{ Placeholder }}")
    if verus_consts or verus_error_types:
        lines.append("")
    lines.append("pub open spec fn abs_f64(value: f64) -> f64 {")
    lines.append("    if value < 0.0f64 { -value } else { value }")
    lines.append("}")
    lines.append("")
    lines.append("pub open spec fn verus_f64_is_finite(value: f64) -> bool {")
    lines.append("    value >= -1.7976931348623157e308f64 && value <= 1.7976931348623157e308f64")
    lines.append("}")
    lines.append("")
    lines.append("pub open spec fn verus_sum_f64(start: int, end: int, f: spec_fn(int) -> f64) -> f64")
    lines.append("    decreases if start < end { (end - start) as nat } else { 0nat }")
    lines.append("{")
    lines.append("    if start < end {")
    lines.append("        f(start) + verus_sum_f64(start + 1, end, f)")
    lines.append("    } else {")
    lines.append("        0.0f64")
    lines.append("    }")
    lines.append("}")
    lines.append("")
    lines.append("pub open spec fn verus_sum_f64_where(start: int, end: int, p: spec_fn(int) -> bool, f: spec_fn(int) -> f64) -> f64")
    lines.append("    decreases if start < end { (end - start) as nat } else { 0nat }")
    lines.append("{")
    lines.append("    if start < end {")
    lines.append("        (if p(start) { f(start) } else { 0.0f64 }) + verus_sum_f64_where(start + 1, end, p, f)")
    lines.append("    } else {")
    lines.append("        0.0f64")
    lines.append("    }")
    lines.append("}")
    lines.append("")
    lines.append("pub open spec fn verus_product_f64(start: int, end: int, f: spec_fn(int) -> f64) -> f64 { 1.0f64 }")
    lines.append("pub open spec fn verus_product_f64_where(start: int, end: int, p: spec_fn(int) -> bool, f: spec_fn(int) -> f64) -> f64 { 1.0f64 }")
    lines.append("")
    lines.extend(doc_lines(spec["english_description"]))
    lines.append(f"pub struct {concept};")
    lines.append("")
    lines.append(f"impl {concept} {{")
    query_spec_map = verus_query_spec_companions(methods)
    len_spec_map = verus_query_len_spec_companions(methods)
    for method in methods:
        if method.name in query_spec_map:
            lines.append("")
            emit_verus_query_spec_companion(lines, method, query_spec_map[method.name])
        if method.name in len_spec_map:
            lines.append("")
            emit_verus_query_len_spec_companion(lines, method, len_spec_map[method.name])
    for method in methods:
        lines.append("")
        lines.extend(f"    {line}" for line in doc_lines(method.english))
        requires, ensures = verus_contract_groups(spec, method, query_spec_map, len_spec_map)
        rust_sig = rust_signature_with_named_result(method.rust_sig) if ensures else method.rust_sig
        lines.append(f"    pub {rust_sig}")
        if requires:
            lines.append("        requires")
            for logic in requires:
                lines.append(f"            {logic},")
        if ensures:
            lines.append("        ensures")
            for logic in ensures:
                lines.append(f"            {logic},")
        lines.append("    {")
        lines.extend(verus_stub_body_lines(method))
        lines.append("    }")
    lines.append("}")
    lines.append("}")
    lines.append("")
    emit_plain_impl(lines, spec, methods, cfg=NOT_VERUS_CFG)


def emit_module(spec: dict[str, Any], methods: list[Method]) -> str:
    concept = spec["concept"]
    module_name = snake_case(concept)
    lines: list[str] = []
    lines.append("//! Generated by emit_stubs.py (concept-to-code skill). Do not hand-edit.")
    lines.extend(doc_lines(spec["english_description"], "//!"))
    lines.append("")
    lines.append("#![allow(unexpected_cfgs)]")
    lines.append("")
    effective_supplementary_imports = list(spec.get("supplementary_imports", []) or [])
    if concept_kind(spec) == "enum":
        # Dispatch methods call trait methods on each variant (e.g. `op.name()`
        # where `name` comes from `impl Trait for Concrete`, not an inherent
        # impl) -- Rust requires the trait itself in scope to call its methods
        # via dot-syntax, even on a type that implements it. This is
        # structurally required for every enum-kind concept, not a
        # spec-author choice, so it is computed here rather than left to
        # manual `supplementary_imports`.
        # `as _` (Rust's unnameable-import idiom): brings the trait's methods
        # into scope for dot-syntax dispatch calls without binding the
        # trait's own name -- an enum-kind concept is free to share its
        # identifier with the trait it wraps once they live in different
        # crates, and a plain `use` would collide with the enum's own name.
        trait_ref = spec["trait_ref"]
        trait_crate_ident = trait_ref["crate"].replace("-", "_")
        trait_module = snake_case(trait_ref["concept"])
        trait_use_path = f"{trait_crate_ident}::{trait_module}::{trait_ref['concept']} as _"
        if trait_use_path not in effective_supplementary_imports:
            effective_supplementary_imports.insert(0, trait_use_path)
    for use_path in effective_supplementary_imports:
        if spec["verifier"] == "verus":
            lines.append(NOT_VERUS_CFG)
        lines.append(f"use {use_path};")
    if effective_supplementary_imports:
        lines.append("")
    if spec["verifier"] == "verus":
        lines.append(VERUS_CFG)
        lines.append(f"use {CONTRACTS_VERUS_PRELUDE}::*;")
        lines.append("")
    if spec["verifier"] == "creusot" and (
        creusot_query_model_companions(methods)
        or creusot_chain_model_companions(methods, spec)
    ):
        # Narrow imports for the emitted query-model companions: the
        # `logic`/`trusted` attribute macros, the `pearlite!` body macro, and
        # the `Int` logic type. A broad `creusot::*` glob would drag in
        # creusot-std's own prelude and risk colliding with the concept
        # module's own imports.
        lines.append("#[cfg(creusot)]")
        lines.append(f"use {CONTRACTS_CREUSOT}::{{logic, trusted}};")
        lines.append("#[cfg(creusot)]")
        lines.append(f"use {CONTRACTS_CREUSOT}::prelude::*;")
        lines.append("")
    for constraint in spec.get("constraints", []):
        lines.extend(doc_lines(constraint.get("english", "")))
        lines.append(f"pub const {constraint_const_name(constraint)}: &str = {rust_string(constraint.get('logic', ''))};")
        lines.append("")
    kind = concept_kind(spec)
    if kind == "trait":
        emit_trait_def(lines, spec, methods)
    elif kind == "enum":
        emit_enum_impl(lines, spec)
    elif spec["verifier"] == "verus":
        emit_verus_impl(lines, spec, methods)
    else:
        emit_plain_impl(lines, spec, methods)
    lines.append("")
    lines.append(f"// Module name hint for callers: {module_name}")
    return "\n".join(lines) + "\n"


def constraint_const_name(constraint: dict[str, Any]) -> str:
    english = constraint.get("english", "constraint")
    raw = re.sub(r"[^A-Za-z0-9]+", "_", english).strip("_").upper()
    if not raw or raw[0].isdigit():
        raw = f"CONSTRAINT_{raw}"
    return f"SPEC_{raw[:64]}"


def emit_props(spec: dict[str, Any]) -> str:
    concept = spec["concept"]
    module_name = snake_case(concept)
    lines: list[str] = []
    lines.append("//! Generated by emit_stubs.py (concept-to-code skill). Do not hand-edit.")
    lines.append(f"//! Proptest scaffolding for {concept}.")
    if concept_kind(spec) == "enum":
        # kind='enum' has no constraints/adversary_table of its own -- the
        # real behavior contract lives on the trait_ref and each wrapped
        # variant's own concept. An empty 0usize..0 proptest range panics at
        # test time, so there is nothing meaningful to scaffold here; each
        # variant's own concept already has its own props file.
        lines.append("//! kind='enum': no local constraints/adversary_table to scaffold.")
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append("use proptest::prelude::*;")
    lines.append("")
    lines.append(f"const CONCEPT: &str = {rust_string(concept)};")
    lines.append(f"const MODULE_UNDER_TEST: &str = {rust_string(module_name)};")
    lines.append("const CONSTRAINTS: &[(&str, &str)] = &[")
    for constraint in spec.get("constraints", []):
        lines.append(
            f"    ({rust_string(constraint.get('english', ''))}, {rust_string(constraint.get('logic', ''))}),"
        )
    lines.append("];" )
    lines.append("const ADVERSARY_CASES: &[(&str, &str, &str)] = &[")
    for case in spec.get("adversary_table", []):
        lines.append(
            "    ("
            f"{rust_string(case.get('scenario', ''))}, "
            f"{rust_string(case.get('violates', ''))}, "
            f"{rust_string(case.get('resolution', ''))}),"
        )
    lines.append("];" )
    lines.append("")
    if not spec.get("commands", []):
        lines.append(f"fn {strategy_name(spec)}() -> impl Strategy<Value = ()> {{")
        lines.append(
            "    // TODO(spec-first): wire observational values from owner-managed storage."
        )
        lines.append("    Just(())")
        lines.append("}")
        lines.append("")
    lines.append("proptest! {")
    if not spec.get("commands", []):
        lines.append("    #[test]")
        lines.append(
            f"    fn generated_observational_strategy_is_available(_node in {strategy_name(spec)}()) {{"
        )
        lines.append("        prop_assert!(!CONCEPT.is_empty());")
        lines.append("    }")
        lines.append("")
    lines.append("    #[test]")
    lines.append("    fn generated_constraints_are_traceable(index in 0usize..CONSTRAINTS.len()) {")
    lines.append("        let (english, logic) = CONSTRAINTS[index];")
    lines.append("        prop_assert!(!CONCEPT.is_empty());")
    lines.append("        prop_assert!(!MODULE_UNDER_TEST.is_empty());")
    lines.append("        prop_assert!(!english.trim().is_empty());")
    lines.append("        prop_assert!(!logic.trim().is_empty());")
    lines.append("    }")
    lines.append("")
    lines.append("    #[test]")
    lines.append("    fn generated_adversary_cases_are_actionable(index in 0usize..ADVERSARY_CASES.len()) {")
    lines.append("        let (scenario, violates, resolution) = ADVERSARY_CASES[index];")
    lines.append("        prop_assert!(!scenario.trim().is_empty());")
    lines.append("        prop_assert!(!violates.trim().is_empty());")
    lines.append("        prop_assert!(!resolution.trim().is_empty());")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def emit_kani_f64_harness(spec: dict[str, Any]) -> str:
    """Emit implementation-stage f64 checks supplementing Creusot contracts.

    Generates one `#[kani::proof]` harness per `kani_f64_checks` entry,
    checking exactly the f64 sign/finiteness obligations a Creusot-primary
    concept's own contracts can't express in Pearlite logic (see
    docs/spec-first-workflow.md and verifiers/creusot/step-c-verify.md for
    when to reach for this)."""
    concept = spec["concept"]
    module_name = snake_case(concept)
    checks = spec.get("kani_f64_checks", [])
    imports = sorted(
        {import_path for check in checks for import_path in check.get("imports", [])}
    )
    lines = [
        "//! Generated by emit_stubs.py (concept-to-code skill). Do not hand-edit.",
        f"//! Supplementary Kani f64 checks for Creusot-primary {concept}.",
        "",
        "#![cfg(kani)]",
        "",
    ]
    for import_path in imports:
        lines.append(f"use {import_path};")
    if imports:
        lines.append("")
    for check in checks:
        lines.append("#[kani::proof]")
        if check["expected"] == "panic":
            lines.append("#[kani::should_panic]")
        lines.append(f"fn kani_f64_{module_name}_{check['name']}() {{")
        for symbol in check["symbolic_f64s"]:
            lines.append(f"    let {symbol}: f64 = kani::any();")
        for assumption in check["assumptions"]:
            lines.append(f"    kani::assume({assumption});")
        for statement in check["statements"]:
            lines.append(f"    {statement.rstrip(';')};")
        for assertion in check.get("assertions", []):
            lines.append(f"    assert!({assertion});")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def default_paths(spec: dict[str, Any], crate_dir: Path) -> tuple[Path, Path, Path]:
    module = snake_case(spec["concept"])
    return (
        crate_dir / "src" / f"{module}.rs",
        crate_dir / "tests" / f"props_{module}.rs",
        crate_dir / "tests" / f"kani_f64_{module}.rs",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Emit deterministic concept-to-code Rust stubs")
    parser.add_argument("spec_json", type=Path)
    parser.add_argument("--crate-dir", type=Path, required=True, help="Path to the target crate root (containing src/ and tests/)")
    parser.add_argument("--module-out", type=Path)
    parser.add_argument("--props-out", type=Path)
    parser.add_argument("--kani-f64-out", type=Path)
    parser.add_argument(
        "--contracts-crate",
        default="contracts",
        help="Crate name for the verifier-contracts facade exposing "
        "<crate>::creusot::* and <crate>::verus::prelude::* (default: 'contracts')",
    )
    parser.add_argument(
        "--specs-search-root",
        type=Path,
        help="Root directory searched for another crate's spec JSON when resolving "
        "implements/trait_ref/variants cross-crate references, as "
        "<root>/<crate>/specs/<snake_case(concept)>.json. Defaults to the parent "
        "of --crate-dir, so a single-crate invocation with no cross-crate "
        "references is unaffected.",
    )
    parser.add_argument("--check", action="store_true", help="validate only; do not write files")
    args = parser.parse_args(argv)

    global CONTRACTS_CREUSOT, CONTRACTS_VERUS_PRELUDE, SPECS_SEARCH_ROOT
    CONTRACTS_CREUSOT = f"{args.contracts_crate}::creusot"
    CONTRACTS_VERUS_PRELUDE = f"{args.contracts_crate}::verus::prelude"
    SPECS_SEARCH_ROOT = args.specs_search_root or args.crate_dir.parent

    try:
        spec = json.loads(args.spec_json.read_text())
        methods = validate_spec(spec)
        module_out, props_out, kani_f64_out = default_paths(spec, args.crate_dir)
        if args.module_out:
            module_out = args.module_out
        if args.props_out:
            props_out = args.props_out
        if args.kani_f64_out:
            kani_f64_out = args.kani_f64_out
        module = emit_module(spec, methods)
        props = emit_props(spec)
        kani_f64 = emit_kani_f64_harness(spec) if spec.get("kani_f64_checks") else None
        if args.check:
            outputs = [str(module_out), str(props_out)]
            if kani_f64 is not None:
                outputs.append(str(kani_f64_out))
            print(f"valid: {spec['concept']} -> {' and '.join(outputs)}")
            return 0
        module_out.parent.mkdir(parents=True, exist_ok=True)
        props_out.parent.mkdir(parents=True, exist_ok=True)
        module_out.write_text(module)
        props_out.write_text(props)
        print(f"wrote {module_out}")
        print(f"wrote {props_out}")
        if kani_f64 is not None:
            kani_f64_out.parent.mkdir(parents=True, exist_ok=True)
            kani_f64_out.write_text(kani_f64)
            print(f"wrote {kani_f64_out}")
        return 0
    except (OSError, json.JSONDecodeError, SpecError) as exc:
        print(f"emit_stubs.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
