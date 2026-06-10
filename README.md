# concept-to-code

A Claude Code skill that walks a concept through a concept-to-contract-to-code
pipeline for Rust crates: restricted-English concept analysis, query/command/
constraint co-analysis, deterministic Rust stub generation, and a verifier
(Kani/Creusot/Verus) stub-gate.

## What it does

The pipeline adapts Kiniry and Zimmerman's "Secret Ninja Formal Methods"
BON-to-JML concept-to-contract method to Rust:

1. **Step A — Concept analysis.** Produce a JSON artifact (validated against
   `schemas/spec.schema.json`) that names the concept, its cluster, and its
   restricted-English queries, commands, constraints, and adversary table.
   No Rust vocabulary (`struct`, `fn`, `trait`, ...) is allowed until this
   artifact is stable.
2. **Step B — Stub generation.** `emit_stubs.py` deterministically turns the
   JSON artifact into a Rust module: signatures with copied Rustdoc,
   `unimplemented!()` bodies, proptest scaffolding, and `cfg_attr` verifier
   contract annotations for whichever verifier the JSON selects.
3. **Step C — Verifier stub-gate.** Run the selected verifier (Kani, Creusot,
   or Verus) in its lean/check mode to confirm the generated contracts are
   well-formed and reachable, before any real implementation is written.

See `docs/spec-first-workflow.md` for the full six-step mapping and
`SKILL.md` for the skill's hard rules and file map.

## Installation

Copy or symlink this directory into your project's skills directory:

```bash
cp -r concept-to-code <your-project>/.claude/skills/concept-to-code
# or, to track upstream changes:
ln -s /path/to/concept-to-code <your-project>/.claude/skills/concept-to-code
```

Once installed, invoke the skill with `/concept-to-code <concept>` from
within your project.

## Prerequisites

- Rust toolchain (`cargo`) for the target crate.
- Python 3 (standard library only — `emit_stubs.py` and the test suite have
  no third-party dependencies).
- Optional, for Step C full verification or the gate self-tests: `cargo-kani`,
  `cargo-creusot`, and/or `cargo-verus` on `PATH`, matching whichever
  verifier(s) your crates use.

## Configuration

A consuming project provides or configures the following when using this
skill:

| Setting | Default | Notes |
|---|---|---|
| `--crate-dir <path>` | required | Target crate root (containing `src/` and `tests/`) that `emit_stubs.py` writes generated modules and proptest scaffolds into. |
| `--contracts-crate <name>` | `contracts` | Verifier-contracts facade crate. Must expose `<name>::creusot::*` (Pearlite contract attributes, `creusot_f64_*` model-predicate helpers, and a `creusot::prelude` re-export) and `<name>::verus::prelude::*` (the Verus prelude). See `tests/fixtures/contracts-crate/` for a minimal crate satisfying this shape. |
| Findings-report path | project-defined | Where Step A reports are written. One example convention: `findings/<crate>/<concept-kebab>.md` (or, with ticket numbers, `findings/<crate>/T-<id>-<concept-kebab>.md`). |
| Trace-sidecar path | project-defined, optional | If your project mines invariants from a reference implementation (e.g. via Daikon), point Step A at that sidecar directory. Skip entirely if you have none. |
| `schemas/spec.schema.json` | bundled | Projects may extend or replace this schema as long as the `$defs` shape (`query`/`command`/`constraint`/`adversary_case`/`source_reference`) is preserved, since `emit_stubs.py` depends on it. |

## Quickstart

1. Read `schemas/spec.schema.json` and `docs/spec-first-workflow.md` to
   understand the artifact shape.
2. Follow `prompts/step-a-coanalysis.md` to write `<concept>.json` for a new
   concept — for example, a `Buffer` concept with a `len()` query, a
   `scale(factor: f64)` command, and constraints that every element stays
   finite.
3. Follow `prompts/step-b-skeleton.md` to run:

   ```bash
   python3 <path-to-skill>/emit_stubs.py <concept>.json \
     --crate-dir <path-to-your-crate> \
     --contracts-crate contracts
   ```

   This writes a generated module under `<path-to-your-crate>/src/` with
   `unimplemented!()` bodies and verifier `cfg_attr` annotations, plus
   proptest scaffolding under `tests/`.
4. Read the `verifier` field from `<concept>.json` and follow the matching
   `verifiers/<verifier>/step-c-verify.md` to run the lean stub-gate, e.g. for
   Kani:

   ```bash
   cargo kani -p <your-crate> --tests --only-codegen
   ```

5. Once the stub-gate is clean, implement the bodies. Re-run the full
   verifier (or your test suite) on the implemented methods.

`tests/fixtures/example_concept.json` is a complete worked example (a
`NumericKernel` concept with queries, commands, constraints, and an adversary
table) you can run through `emit_stubs.py` to see the generated output before
trying your own concept.

## File map

- `SKILL.md` — skill manifest: triggers, configuration, hard rules, file map,
  recommended flow.
- `docs/spec-first-workflow.md` — the six-step workflow this skill follows.
- `schemas/spec.schema.json` — JSON schema for concept artifacts.
- `prompts/step-a-coanalysis.md` — Step A prompt: concept analysis and
  co-analysis.
- `prompts/step-b-skeleton.md` — Step B prompt: running `emit_stubs.py`.
- `verifiers/{kani,creusot,verus}/step-c-verify.md` — Step C prompts, one per
  verifier.
- `verifiers/{kani,creusot,verus}/contracts.md` — contract-pattern reference
  for each verifier.
- `emit_stubs.py` — deterministic JSON-to-Rust stub generator.
- `tests/` — generator tests (`emit_stubs_test.py`, `test_emit_stubs.py`) and
  verifier gate self-tests (`gate_selftest_{kani,creusot,verus}.py`, skipped
  unless `SPEC_RUN_{KANI,CREUSOT,VERUS}_SELFTEST=1` and the corresponding
  `cargo-*` tool is on `PATH`).
- `tests/fixtures/` — fixtures for the above, including
  `example_concept.json` and a minimal `contracts-crate/` satisfying the
  `--contracts-crate` facade shape.
