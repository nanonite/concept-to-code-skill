# Step C (Verus): Contract And Mode-Check Round-Trip

Goal: run Verus against generated stubs when the concept JSON has
`"verifier": "verus"`.

## Preconditions

- Step B generated Rust stubs from JSON.
- Generated stubs remain unimplemented.
- Stubs contain a `verus! { ... }` block with `requires` and `ensures`
  clauses, plus a `#[cfg(not(any(verus, feature = "verus")))]` plain Rust
  fallback.
- `cargo check --workspace` passes for the non-Verus fallback module.

## Lean Gate

Use this PR-time command for generated stubs:

```bash
cargo verus focus -p <your-crate> --features verus -- --no-verify
```

(Replace the bare `cargo verus` invocation with however your project invokes
the pinned Verus toolchain — e.g. via `nix develop`, a devshell, or a
container — if it is not on `PATH` directly. Note that `cargo verus`
typically requires a subcommand, e.g. `focus`, before package/feature flags.)

Expected signal: `cargo-verus` drives Cargo package resolution, compiles the
selected crate with its `verus` feature enabled, reaches generated `verus! {
... }` blocks in-place, and runs Verus parsing, mode checking, lifetime
checking, and erasure checking. `--no-verify` disables solver proof search
only.

For a single generated module during local debugging, temporarily reduce the
fixture into a small standalone package and run the same command with
`--manifest-path`.

### Notes on cargo-verus integration

- Native `cargo-verus` requires the root crate to import `vstd`. If your
  crate's normal builds should not depend on `vstd`, gate it behind an
  optional `verus` feature so default runtime builds stay on the
  verifier-facade path while `cargo verus` still gets direct root-crate
  visibility.
- `emit_stubs.py` emits `#[cfg(any(verus, feature = "verus"))]`, local
  placeholder constants, and Verus-local `*Error` placeholder enums so native
  `cargo-verus` checks the real generated module rather than a synthesized
  out-of-crate wrapper.

### Restricted-English logic translation

`emit_stubs.py` runs `translate_logic_to_verus` over each
`constraints[].logic` field before emitting it into a `requires` or `ensures`
clause. The translator handles:

- `implies -> ==>`
- `if X then A else B -> (if X { A } else { B })`
- `forall i in 0..N: BODY -> forall|i: int| ... ==> BODY`
- `forall i,j in 0..N where GUARD: BODY` with bounded nested variables
- `sum(i in 0..N: EXPR)` through generated `verus_sum_f64` spec helpers
- named `result` returns for `ensures`
- `final(out)@` post-state slice access with `int` indexes
- f64 literal suffixes and pure-query `_spec` companions

If a translated expression falls outside this set, `emit_stubs.py` emits a
`true /* TODO(concept-to-code): ... */` placeholder — treat that as
unverified and revisit the constraint's `logic` before relying on the
generated contract.

## Full Verification

Full Verus verification is reserved for selected critical methods after
Step 6 implementation and release/nightly CI. Omit `--no-verify` only when
loop, ghost, and proof obligations are ready for solver-backed verification.

## Gate Self-Test

The Verus lean gate has a falsifiability test under
`tests/gate_selftest_verus.py`. It builds two temporary crates from
`fixtures/valid_verus.rs` and `fixtures/broken_verus.rs` and asserts that the
lean gate passes the valid fixture and rejects the broken one. This test is
skipped by default; run it explicitly with:

```bash
SPEC_RUN_VERUS_SELFTEST=1 python3 -m unittest tests.gate_selftest_verus
```

## Report

Summarize:

- Concept name and crate.
- Command run and exit code.
- Whether Verus mode/erasure checks reached the generated stubs.
- Any missing `verus!` block or contract syntax errors.
- Any drift found by your project's spec-lint tooling or generated test
  failures.
