---
name: concept-to-code
description: Walk a concept through concept analysis, query/command/constraint co-analysis, deterministic Rust stub generation, and verifier stub checking.
triggers:
  - /concept-to-code <concept>
  - concept-to-code
  - concept analysis
---

# concept-to-code Skill

Use this skill when the user invokes `/concept-to-code <concept>` or asks for
spec-first/concept-analysis work on a Rust crate.

This skill implements a concept-to-contract-to-code pipeline adapted from
Kiniry and Zimmerman's "Secret Ninja Formal Methods":

1. Step A: concept and co-analysis, producing JSON that conforms to
   `schemas/spec.schema.json`.
2. Step B: deterministic Rust stub generation with `emit_stubs.py`.
3. Step C: verifier round-trip with Kani, Creusot, or Verus, selected by the
   JSON `verifier` field.

## Configuration

A consuming project provides or configures:

- `--crate-dir <path>` (required): the target crate root (containing `src/`
  and `tests/`) that `emit_stubs.py` writes generated modules and proptest
  scaffolds into.
- `--contracts-crate <name>` (default `contracts`): the verifier-contracts
  facade crate. It must expose `<name>::creusot::*` (Pearlite contract
  attributes plus `creusot_f64_*` model-predicate helpers, `logic`/`trusted`
  attribute macros and a `pearlite!` macro for query logic-model companions,
  and a `creusot::prelude` re-export) and `<name>::verus::prelude::*` (the
  Verus prelude). See `tests/fixtures/contracts-crate/` for a minimal example
  satisfying this shape.
- `--specs-search-root <path>` (default: the parent of `--crate-dir`): root
  directory searched for another crate's spec JSON when resolving a
  `kind: "struct"` concept's `implements` or a `kind: "enum"` concept's
  `trait_ref`/`variants`, as `<root>/<crate>/specs/<snake_case(concept)>.json`.
  Only matters for concepts using those fields; a single-crate invocation
  with none is unaffected.
- A findings-report path convention (project-defined). One example
  convention: `findings/<crate>/<concept-kebab>.md`.
- An optional trace-sidecar path for trace-inferred invariants (project-
  defined, e.g. via Daikon). Skip Step A's sidecar-triage section entirely if
  your project has none.
- `schemas/spec.schema.json` (bundled default). Projects may extend or
  replace it as long as the `$defs` shape (query/command/constraint/
  adversary_case/source_reference) is preserved, since `emit_stubs.py`
  depends on that shape.

## Concept kinds

Most concepts are `kind: "struct"` (the default): a concrete `pub struct`
with an inherent impl. Two more kinds exist for composing concepts without
`dyn Trait`, which some deductive verifiers (e.g. Creusot) cannot reason
about:

- `kind: "trait"` emits a `pub trait` with contracts on its bodyless method
  declarations. A `kind: "struct"` concept's `implements` field routes named
  methods into `impl Trait for Concept` instead of the inherent impl, with no
  restated contract attributes (the verifier checks refinement against the
  trait's own declaration).
- `kind: "enum"` emits a closed `pub enum` wrapping named concrete concepts
  (`variants`), with one match-dispatched method per method declared on the
  trait named by `trait_ref`.

Both `implements` and `trait_ref`/`variants` reference another concept's spec
JSON by crate name, resolved under `--specs-search-root`. See
`tests/fixtures/trait_enum_demo/` for a worked example (a `Toggle` trait, two
concrete implementors, and an `AnyToggle` enum composing them).

## Supplementary Kani f64 checks

A Creusot-primary concept (`verifier: "creusot"`) may include
`kani_f64_checks`: implementation-stage Kani harnesses supplementing Pearlite
contracts when an f64 sign or finiteness obligation can't be expressed in
Creusot logic (check whether the Creusot toolchain in use has closed this gap
before reaching for it). Presence generates `tests/kani_f64_<concept>.rs` via
`--kani-f64-out` (default path from `default_paths`). See
`tests/fixtures/kani_f64_demo.json` for a worked example.

## Concept dependencies and workspace tooling

Cross-concept relationships are otherwise invisible to tooling — `implements`/
`trait_ref`/`variants` are resolved once, at generation time, for the one
concept being built, with no reverse index and no way to ask "what else
needs review if I change this concept's shape." An optional `depends_on:
[{crate, concept, reason}]` field records a *semantic* dependency with no
other structural home (e.g. a constraint implicitly assumes an invariant
another concept establishes) — purely declarative, not consumed by
`emit_stubs.py` itself.

`spec_workspace.py` is the workspace-level companion to `emit_stubs.py`: it
walks every spec JSON under `--specs-search-root` and can:

- `discover` — list every concept, its generation state (`PENDING`/`STUB`/
  `IMPLEMENTED`), and its dependency edges (`implements`/`trait_ref`/
  `variant`/`depends_on`).
- `impact <crate>::<Concept>` — print every concept that depends on the
  given one, directly or transitively, so a change's blast radius is a
  command instead of a memory exercise.
- `graph` — dump the full dependency graph.
- `check` — regenerate every concept to a temp dir and byte-diff against the
  committed output; fails on drift (hand-edited or stale generated files).
- `verify-lean` / `verify-full` — dispatch the selected verifier per
  concept, **skipping any concept whose generated body still contains
  `unimplemented!()` under `verify-full`** (see Hard Rules below).

Run `spec_workspace.py discover` and `spec_workspace.py impact <key>` before
adding a concept that composes with existing ones, and add a `depends_on`
entry (with a real `reason`, not just a citation) whenever a constraint's
correctness silently leans on another concept's behavior.

## Hard Rules

- During Step A, do not propose Rust declaration vocabulary until the
  concept, restricted-English queries, commands, constraints, and adversary
  table exist. Forbidden vocabulary before that point: `struct`, `enum`,
  `trait`, `fn`, `impl`, `generic`.
- Refuse to advance past Step A unless `adversary_table` is present and
  non-empty.
- Refuse to run Step B if any query uses `&mut self`, if any query returns
  `()`, or if any query has `pure: false`.
- Generated Rustdoc must copy English text from the JSON spec verbatim.
- Generated stubs must keep bodies as `unimplemented!()` until Step C reports
  contract well-formedness. This is a prose rule for you to follow during the
  conversation, **and** a mechanically checked one: `spec_workspace.py
  verify-full` refuses to run full verification against any concept whose
  generated source still contains `unimplemented!()`, printing `SKIP
  <concept>: unimplemented body` instead of silently attempting it (or
  worse, silently passing because there was nothing left to disprove).
- Do not hand-edit generated stubs. Regenerate from JSON.
  `spec_workspace.py check` mechanically enforces this too: it regenerates
  every concept to a temp dir and fails the moment a byte differs from the
  committed file.

## Files

- `prompts/step-a-coanalysis.md`: use first to create or repair the JSON spec.
- `prompts/step-b-skeleton.md`: use after Step A validates; it invokes
  `emit_stubs.py`.
- `verifiers/kani/step-c-verify.md`: use after Step B when `verifier` is
  `kani`.
- `verifiers/creusot/step-c-verify.md`: use after Step B when `verifier` is
  `creusot`.
- `verifiers/verus/step-c-verify.md`: use after Step B when `verifier` is
  `verus`.
- `verifiers/{kani,creusot,verus}/contracts.md`: contract-pattern reference
  for each verifier.
- `emit_stubs.py`: deterministic JSON-to-Rust stub generator.
- `spec_workspace.py`: workspace-wide discovery, dependency-impact analysis,
  drift-check, and gated verifier dispatch across every spec under
  `--specs-search-root`.
- `schemas/spec.schema.json`: bundled generic concept schema.
- `docs/spec-first-workflow.md`: the 6-step workflow this skill follows.
- `tests/fixtures/trait_enum_demo/`: worked `kind: "trait"`/`kind: "enum"`/
  `implements` example.
- `tests/fixtures/kani_f64_demo.json`: worked `kani_f64_checks` example.

## Recommended Flow

1. Read `schemas/spec.schema.json` and `docs/spec-first-workflow.md`. If this
   concept composes with existing ones (via `implements`/`trait_ref` or an
   assumed `depends_on` relationship), run `spec_workspace.py discover` and
   `spec_workspace.py impact <crate>::<Concept>` on anything it will depend
   on first, to see what's already there and what else might need review.
2. Follow `prompts/step-a-coanalysis.md` to produce `<concept>.json` or repair
   an existing spec. Add a `depends_on` entry for any semantic dependency not
   already captured by `implements`/`trait_ref`/`variants`.
3. Follow `prompts/step-b-skeleton.md` to run `emit_stubs.py`.
4. Read `verifier` from the concept JSON and dispatch Step C:
   - `kani` -> follow `verifiers/kani/step-c-verify.md`.
   - `creusot` -> follow `verifiers/creusot/step-c-verify.md`.
   - `verus` -> follow `verifiers/verus/step-c-verify.md`.
5. Report the selected verifier lean-gate signal: contracts are well formed
   and verifier startup/typechecking reaches the unimplemented stubs without
   parser errors.
6. Periodically (e.g. before a PR), run `spec_workspace.py check` to catch
   drift between committed generated files and what the spec would produce
   today, and `spec_workspace.py verify-full` to confirm nothing implemented
   is being silently skipped and nothing still-a-stub is being silently
   full-verified.
