# Step B: Deterministic Module Skeleton And Signatures

Goal: invoke `emit_stubs.py` to turn a validated Step A JSON artifact into
Rust stubs and proptest scaffolding.

## Preconditions

- Step A JSON exists and conforms to `schemas/spec.schema.json`.
- `adversary_table` is populated.
- No query uses `&mut self`.
- No query returns `()`.
- Every query has `pure: true`.

## Command

From your project's repository root:

```bash
python3 <path-to-skill>/emit_stubs.py \
  specs/<concept>.json \
  --crate-dir <path-to-your-crate>
```

For example, if this skill is installed at
`.claude/skills/concept-to-code/` and the target crate lives at
`crates/my-crate/`:

```bash
python3 .claude/skills/concept-to-code/emit_stubs.py \
  specs/<concept>.json \
  --crate-dir crates/my-crate
```

For a single-crate repository where the crate root is the repository root,
pass `--crate-dir .`.

By default, generated stubs reference a verifier-contracts facade crate named
`contracts` (`contracts::creusot::*`, `contracts::verus::prelude::*`). If your
project's facade crate has a different name, pass `--contracts-crate
<your-contracts-crate>`.

Override output paths when needed:

```bash
python3 <path-to-skill>/emit_stubs.py \
  specs/<concept>.json \
  --crate-dir <path-to-your-crate> \
  --module-out <path-to-your-crate>/src/<module>.rs \
  --props-out <path-to-your-crate>/tests/props_<module>.rs
```

## Expected Output

- Rust module with Rustdoc copied verbatim from `english_description`,
  `queries[].english`, `commands[].english`, and `constraints[].english`.
- One generated stub per query/command with body `unimplemented!()`.
- `#[cfg_attr(kani, kani::requires(...))]` attributes generated from
  applicable `constraints[].logic`.
- `tests/props_<concept>.rs` with proptest scaffolding derived
  deterministically from constraints and adversary cases.

## Hard Gate

If the script rejects a query or missing adversary table, return to Step A.
Do not hand-edit generated output to bypass the gate.
