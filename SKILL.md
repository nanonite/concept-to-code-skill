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
  attributes plus `creusot_f64_*` model-predicate helpers and a
  `creusot::prelude` re-export) and `<name>::verus::prelude::*` (the Verus
  prelude). See `tests/fixtures/contracts-crate/` for a minimal example
  satisfying this shape.
- A findings-report path convention (project-defined). One example
  convention: `findings/<crate>/<concept-kebab>.md`.
- An optional trace-sidecar path for trace-inferred invariants (project-
  defined, e.g. via Daikon). Skip Step A's sidecar-triage section entirely if
  your project has none.
- `schemas/spec.schema.json` (bundled default). Projects may extend or
  replace it as long as the `$defs` shape (query/command/constraint/
  adversary_case/source_reference) is preserved, since `emit_stubs.py`
  depends on that shape.

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
  contract well-formedness.
- Do not hand-edit generated stubs. Regenerate from JSON.

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
- `schemas/spec.schema.json`: bundled generic concept schema.
- `docs/spec-first-workflow.md`: the 6-step workflow this skill follows.

## Recommended Flow

1. Read `schemas/spec.schema.json` and `docs/spec-first-workflow.md`.
2. Follow `prompts/step-a-coanalysis.md` to produce `<concept>.json` or repair
   an existing spec.
3. Follow `prompts/step-b-skeleton.md` to run `emit_stubs.py`.
4. Read `verifier` from the concept JSON and dispatch Step C:
   - `kani` -> follow `verifiers/kani/step-c-verify.md`.
   - `creusot` -> follow `verifiers/creusot/step-c-verify.md`.
   - `verus` -> follow `verifiers/verus/step-c-verify.md`.
5. Report the selected verifier lean-gate signal: contracts are well formed
   and verifier startup/typechecking reaches the unimplemented stubs without
   parser errors.
