#!/usr/bin/env python3
"""Workspace-wide spec discovery, drift-checking, dependency-impact analysis,
and gated verifier dispatch for concept-to-code specs.

Complements `emit_stubs.py` (which generates one concept's Rust stub at a
time) with the workspace-level view: every spec JSON under
`--specs-search-root`, its generation state (pending / stub / implemented),
its dependency graph (`implements`/`trait_ref`/`variants`/`depends_on`), and
a verifier dispatcher that refuses to run full verification against a
concept whose generated body still reads `unimplemented!()` -- a mechanical
backstop for the "keep it a stub until Step C reports contract
well-formedness" rule that would otherwise depend entirely on prose
discipline (SKILL.md's Hard Rules).

Stdlib-only, matching `emit_stubs.py`'s own dependency policy. Discovery
uses the same `<specs-search-root>/<crate>/specs/<snake_case(concept)>.json`
convention `emit_stubs.py --specs-search-root` already establishes for
cross-crate `implements`/`trait_ref` resolution -- no separate layout to
learn.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
EMIT_STUBS = SCRIPT_DIR / "emit_stubs.py"
VERIFIERS = {"kani", "creusot", "verus"}
DEFAULT_SPECS_GLOB = "*/specs/*.json"


class WorkspaceError(RuntimeError):
    pass


def snake_case(name: str) -> str:
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index and not name[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out).replace("-", "_")


@dataclass(frozen=True)
class Dependency:
    crate: str
    concept: str
    reason: str
    link: str  # "implements" | "trait_ref" | "variant" | "depends_on"

    @property
    def key(self) -> str:
        return f"{self.crate}::{self.concept}"


@dataclass(frozen=True)
class Concept:
    spec: Path
    crate: str
    crate_dir: Path
    name: str
    kind: str
    module: str
    verifier: str
    source: Path
    props: Path
    kani_f64: Path
    has_hybrid: bool
    dependencies: tuple[Dependency, ...]

    @property
    def key(self) -> str:
        return f"{self.crate}::{self.name}"

    @property
    def pending(self) -> bool:
        return not self.source.exists() and not self.props.exists()

    @property
    def partial(self) -> bool:
        return self.source.exists() != self.props.exists()

    @property
    def implemented(self) -> bool:
        """Whether this concept is past the stub phase.

        Only `kind='struct'` (the default) has an independent stub/implemented
        distinction: its inherent-impl methods start as `unimplemented!()`
        bodies and get real bodies written in during Step 6. `kind='trait'`
        declarations are bodyless by construction (no `unimplemented!()` ever
        appears), and `kind='enum'` dispatch methods are real match-dispatch
        code the moment they're generated -- neither carries a stub state of
        its own to check.
        """
        if not self.source.exists():
            return False
        if self.kind != "struct":
            return True
        return "unimplemented!()" not in self.source.read_text()


def _dependencies_from_spec(raw: dict) -> tuple[Dependency, ...]:
    deps: list[Dependency] = []
    for trait_name, trait_info in (raw.get("implements") or {}).items():
        deps.append(
            Dependency(
                crate=trait_info["crate"],
                concept=trait_name,
                reason=f"implements {trait_name}",
                link="implements",
            )
        )
    trait_ref = raw.get("trait_ref")
    if trait_ref:
        deps.append(
            Dependency(
                crate=trait_ref["crate"],
                concept=trait_ref["concept"],
                reason="trait_ref",
                link="trait_ref",
            )
        )
    for variant in raw.get("variants") or []:
        wraps = variant["wraps"]
        deps.append(
            Dependency(
                crate=wraps["crate"],
                concept=wraps["concept"],
                reason=f"variant {variant['name']}",
                link="variant",
            )
        )
    for dep in raw.get("depends_on") or []:
        deps.append(
            Dependency(
                crate=dep["crate"],
                concept=dep["concept"],
                reason=dep.get("reason", ""),
                link="depends_on",
            )
        )
    return tuple(deps)


def discover(root: Path, specs_glob: str) -> list[Concept]:
    concepts: list[Concept] = []
    for spec_path in sorted(root.glob(specs_glob)):
        try:
            raw = json.loads(spec_path.read_text())
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"{spec_path}: invalid JSON: {exc}") from exc
        verifier = raw.get("verifier")
        if verifier not in VERIFIERS:
            raise WorkspaceError(f"{spec_path}: unknown verifier {verifier!r}")
        name = raw.get("concept")
        if not isinstance(name, str) or not name:
            raise WorkspaceError(f"{spec_path}: missing concept")
        crate_dir = spec_path.parent.parent
        crate = crate_dir.name
        module = snake_case(name)
        concept = Concept(
            spec=spec_path,
            crate=crate,
            crate_dir=crate_dir,
            name=name,
            kind=raw.get("kind", "struct"),
            module=module,
            verifier=verifier,
            source=crate_dir / "src" / f"{module}.rs",
            props=crate_dir / "tests" / f"props_{module}.rs",
            kani_f64=crate_dir / "tests" / f"kani_f64_{module}.rs",
            has_hybrid=bool(raw.get("kani_f64_checks")),
            dependencies=_dependencies_from_spec(raw),
        )
        if concept.partial:
            raise WorkspaceError(
                f"{spec_path}: partial Step B output; "
                f"source={concept.source.exists()} props={concept.props.exists()}"
            )
        if concept.has_hybrid != concept.kani_f64.exists():
            raise WorkspaceError(f"{spec_path}: hybrid spec/generated harness drift")
        concepts.append(concept)
    return concepts


def cmd_discover(concepts: list[Concept]) -> None:
    if not concepts:
        print("no specs found")
        return
    for concept in concepts:
        if concept.pending:
            state = "PENDING"
        elif concept.implemented:
            state = "IMPLEMENTED"
        else:
            state = "STUB"
        print(f"{state:<11} {concept.key} [{concept.kind}/{concept.verifier}]")
        for dep in concept.dependencies:
            print(f"             -> {dep.key} ({dep.link}: {dep.reason})")


def assert_current(pairs: list[tuple[Path, Path]]) -> None:
    for generated, tracked in pairs:
        if generated.read_bytes() != tracked.read_bytes():
            raise WorkspaceError(f"stale generated output (regenerate): {tracked}")


def cmd_check(concepts: list[Concept], root: Path, contracts_crate: str) -> None:
    failures: list[str] = []
    for concept in concepts:
        if concept.pending:
            print(f"PENDING {concept.key}: Step A only")
            continue
        with tempfile.TemporaryDirectory(prefix="spec-workspace-check-") as tmp:
            temp = Path(tmp)
            module_out = temp / concept.source.name
            props_out = temp / concept.props.name
            kani_out = temp / concept.kani_f64.name
            args = [
                sys.executable,
                str(EMIT_STUBS),
                str(concept.spec),
                "--crate-dir", str(concept.crate_dir),
                "--module-out", str(module_out),
                "--props-out", str(props_out),
                "--kani-f64-out", str(kani_out),
                "--specs-search-root", str(root),
                "--contracts-crate", contracts_crate,
            ]
            result = subprocess.run(
                args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if result.returncode != 0:
                failures.append(concept.key)
                print(f"FAIL {concept.key}: emit_stubs.py error\n{result.stderr}", file=sys.stderr)
                continue
            pairs = [(module_out, concept.source), (props_out, concept.props)]
            if concept.has_hybrid:
                pairs.append((kani_out, concept.kani_f64))
            try:
                assert_current(pairs)
            except WorkspaceError as exc:
                failures.append(concept.key)
                print(f"FAIL {concept.key}: {exc}", file=sys.stderr)
                continue
            print(f"CURRENT {concept.key}")
    if failures:
        raise WorkspaceError(
            f"{len(failures)} concept(s) have stale generated output:\n- " + "\n- ".join(failures)
        )


def _build_reverse_graph(concepts: list[Concept]) -> dict[str, list[tuple[str, Dependency]]]:
    """Map a concept's key to the (dependent's key, edge) pairs that depend on it."""
    reverse: dict[str, list[tuple[str, Dependency]]] = {}
    for concept in concepts:
        for dep in concept.dependencies:
            reverse.setdefault(dep.key, []).append((concept.key, dep))
    return reverse


def cmd_impact(concepts: list[Concept], target: str) -> None:
    known = {c.key for c in concepts}
    if target not in known:
        raise WorkspaceError(f"unknown concept {target!r}; known: {', '.join(sorted(known)) or '(none)'}")
    reverse = _build_reverse_graph(concepts)
    visited: dict[str, tuple[str, Dependency]] = {}
    frontier = [target]
    while frontier:
        current = frontier.pop()
        for dependent_key, dep in reverse.get(current, []):
            if dependent_key in visited or dependent_key == target:
                continue
            visited[dependent_key] = (current, dep)
            frontier.append(dependent_key)
    if not visited:
        print(f"{target}: no other concept depends on it, directly or transitively.")
        return
    print(f"Changing {target} may affect {len(visited)} concept(s):")
    for key, (via, dep) in sorted(visited.items()):
        hop = "" if via == target else f" via {via}"
        print(f"  {key}{hop}  ({dep.link}: {dep.reason})")


def cmd_graph(concepts: list[Concept]) -> None:
    had_edges = False
    for concept in sorted(concepts, key=lambda c: c.key):
        if not concept.dependencies:
            continue
        had_edges = True
        print(f"{concept.key}:")
        for dep in concept.dependencies:
            print(f"  -> {dep.key}  ({dep.link}: {dep.reason})")
    if not had_edges:
        print("no dependency edges declared")


def verifier_command(concept: Concept, level: str) -> list[str]:
    manifest = str(concept.crate_dir / "Cargo.toml")
    if concept.verifier == "kani":
        command = ["cargo", "kani", "--manifest-path", manifest, "--tests"]
        return command + (["--only-codegen"] if level == "lean" else [])
    if concept.verifier == "creusot":
        command = ["cargo", "creusot", "--no-check-version"]
        if level == "lean":
            command += ["--only", "coma", "--check"]
        return command + ["--", "--features", "creusot"]
    command = ["cargo", "verus", "focus", "--manifest-path", manifest, "--features", "verus"]
    return command + (["--", "--no-verify"] if level == "lean" else [])


def cmd_verify(concepts: list[Concept], level: str, dry_run: bool) -> None:
    failures: list[str] = []
    for concept in concepts:
        if concept.pending:
            print(f"PENDING {concept.key}: Step A only")
            continue
        if level == "full" and not concept.implemented:
            print(f"SKIP {concept.key}: unimplemented body")
            continue
        print(f"{level.upper()} {concept.key} [{concept.verifier}]")
        command = verifier_command(concept, level)
        print("+ " + " ".join(command))
        if not dry_run:
            result = subprocess.run(command, cwd=concept.crate_dir)
            if result.returncode != 0:
                failures.append(concept.key)
                print(f"FAIL {concept.key} [{concept.verifier}]", file=sys.stderr)
        if concept.has_hybrid:
            manifest = str(concept.crate_dir / "Cargo.toml")
            kani_command = ["cargo", "kani", "--manifest-path", manifest, "--tests"]
            kani_command += (
                ["--only-codegen"] if level == "lean" else ["--harness", f"kani_f64_{concept.module}_"]
            )
            print("+ " + " ".join(kani_command))
            if not dry_run:
                kani_result = subprocess.run(kani_command, cwd=concept.crate_dir)
                if kani_result.returncode != 0:
                    failures.append(f"{concept.key} (kani f64)")
                    print(f"FAIL {concept.key} [supplementary kani f64]", file=sys.stderr)
    if failures:
        raise WorkspaceError(f"{len(failures)} verifier command(s) failed:\n- " + "\n- ".join(failures))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Workspace-wide spec discovery, drift-checking, dependency-impact analysis, and verifier dispatch"
    )
    parser.add_argument(
        "--specs-search-root",
        type=Path,
        default=Path("."),
        help="Root directory to search for specs, matching emit_stubs.py's --specs-search-root convention (default: '.')",
    )
    parser.add_argument(
        "--specs-glob",
        default=DEFAULT_SPECS_GLOB,
        help="Glob, relative to --specs-search-root, matching spec JSON files (default: %(default)r)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="List every discovered concept, its state, and its declared dependencies")
    check_parser = sub.add_parser("check", help="Regenerate each concept and diff against committed output (drift gate)")
    check_parser.add_argument("--contracts-crate", default="contracts")
    impact_parser = sub.add_parser("impact", help="Print every concept that depends on the given one, directly or transitively")
    impact_parser.add_argument("target", help="crate::Concept")
    sub.add_parser("graph", help="Print the full dependency graph")
    for level in ("lean", "full"):
        verify_parser = sub.add_parser(f"verify-{level}", help=f"Dispatch the {level} verifier gate per concept")
        verify_parser.add_argument("--dry-run", action="store_true", help="Print commands without running them")
    args = parser.parse_args(argv)

    root = args.specs_search_root.resolve()
    try:
        concepts = discover(root, args.specs_glob)
        if args.command == "discover":
            cmd_discover(concepts)
        elif args.command == "check":
            cmd_check(concepts, root, args.contracts_crate)
        elif args.command == "impact":
            cmd_impact(concepts, args.target)
        elif args.command == "graph":
            cmd_graph(concepts)
        else:
            cmd_verify(concepts, args.command.removeprefix("verify-"), args.dry_run)
        return 0
    except (OSError, WorkspaceError) as exc:
        print(f"spec_workspace.py: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
