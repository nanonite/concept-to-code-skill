# Step C (Creusot): Contract And Translation Round-Trip

Goal: run Creusot against generated stubs when the concept JSON has
`"verifier": "creusot"`.

## Preconditions

- Step B generated Rust stubs from JSON.
- Generated stubs remain unimplemented.
- Stubs contain `#[cfg_attr(creusot, contracts::creusot::requires(...))]`
  attributes (or your project's `--contracts-crate` equivalent).
- `cargo check --workspace` passes.
- Your verifier-contracts facade crate (default name `contracts`) is
  available as a dependency.

## Lean Gate

Use this PR-time command for stub-only concepts:

```bash
creusot-rustc <generated-module-or-rustc-args> -- --check
```

(Replace the bare `creusot-rustc` invocation with however your project
invokes the pinned Creusot toolchain — e.g. via `nix develop`, a devshell, or
a container — if it is not on `PATH` directly.)

Expected signal: Creusot performs Rust-to-Coma translation and exits without
contract parse/type errors. `--check` disables output writing and never
invokes Why3 or SMT solvers.

## Full Verification

Full Creusot verification is reserved for selected critical methods after
Step 6 implementation and release/nightly CI. Omit `--check` only when the
proof obligation is ready for solver-backed verification.

## Report

Summarize:

- Concept name and crate.
- Command run and exit code.
- Whether Rust-to-Coma translation reached the generated stubs.
- Any contract parse/type errors.
- Any drift found by your project's spec-lint tooling or generated test
  failures.
