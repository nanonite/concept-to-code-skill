# Step C (Kani): Contract And Harness Round-Trip

Goal: run Kani against generated stubs when the concept JSON has
`"verifier": "kani"`.

## Preconditions

- Step B generated Rust stubs from JSON.
- Generated stubs remain unimplemented.
- Stubs contain `#[cfg_attr(kani, kani::requires(...))]` attributes.
- `cargo check --workspace` passes.

## Lean Gate

Use this PR-time command for stub-only concepts:

```bash
cargo kani -p <your-crate> --tests --only-codegen
```

(Replace the bare `cargo kani` invocation with however your project invokes
the pinned Kani toolchain — e.g. via `nix develop`, a devshell, or a
container — if it is not on `PATH` directly.)

Expected signal: Kani's rustc compiles the crate and links proof harnesses
without invoking CBMC or the SAT solver. `--only-codegen` succeeding is the
Step C green signal for unimplemented stubs.

## Gate Self-Test

The Kani lean gate has a falsifiability test under `tests/gate_selftest_kani.py`.
It builds two temporary crates from `fixtures/valid_kani.rs` and
`fixtures/broken_kani.rs` and asserts that `cargo kani --tests --only-codegen`
passes the valid fixture and rejects the broken one. This test is skipped by
default; run it explicitly with:

```bash
SPEC_RUN_KANI_SELFTEST=1 python3 -m unittest tests.gate_selftest_kani
```

A passing run accepts the valid fixture and rejects the broken
`nonexistent_precondition` fixture.

## Full Verification

Full Kani verification is reserved for selected critical methods after Step 6
implementation and release/nightly CI:

```bash
cargo kani -p <your-crate> --tests --harness-timeout 300
```

## Report

Summarize:

- Concept name and crate.
- Command run and exit code.
- Harness count, if Kani reports one.
- Whether contract parsing/typechecking reached the generated stubs.
- Any drift found by your project's spec-lint tooling or generated test
  failures.
