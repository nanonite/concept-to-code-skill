# Verus Contract Patterns

Use Verus for loop-heavy numerical kernels over arrays and slices: bounds
safety, finite-value invariants, and quantified properties over indexed
collections. Generated stubs use `verus! { ... }` blocks gated by `#[cfg(any(verus,
feature = "verus"))]`, with a plain Rust fallback for normal builds, and
`use contracts::verus::prelude::*` (or your project's `--contracts-crate`
equivalent) for shared spec helpers.

## Module Shape

A generated Verus stub has three parts:

```rust
#[cfg(any(verus, feature = "verus"))]
mod verus_impl {
    use contracts::verus::prelude::*;
    use vstd::prelude::*;

    verus! {
        pub struct Buffer {
            pub values: Vec<f64>,
        }

        impl Buffer {
            pub closed spec fn wf(&self) -> bool {
                forall|i: int| 0 <= i < self.values.len() ==> verus_f64_is_finite(self.values[i])
            }

            pub fn scale_values(&mut self, factor: f64)
                requires
                    old(self).wf(),
                    verus_f64_is_finite(factor),
                ensures
                    self.wf(),
                    self.values.len() == old(self).values.len(),
                    forall|i: int| 0 <= i < self.values.len() ==>
                        self.values[i] == old(self).values[i] * factor,
            {
                unimplemented!()
            }
        }
    }
}

#[cfg(not(any(verus, feature = "verus")))]
mod plain_impl {
    pub struct Buffer {
        pub values: Vec<f64>,
    }

    impl Buffer {
        pub fn scale_values(&mut self, factor: f64) {
            unimplemented!()
        }
    }
}
```

The `verus!` block is the only place spec/proof syntax is legal. Outside it,
write ordinary Rust; the plain fallback module keeps non-Verus builds
compiling and is what runtime code links against.

## Modes

Verus has three modes: `spec` (ghost, erased, used in `requires`/`ensures`/
`invariant`), `proof` (ghost, erased, used for proof steps and lemmas), and
`exec` (real, compiled code). A `spec fn` may call other `spec fn`s only.
`exec` functions may call `spec fn`s only inside `requires`/`ensures`/
`assert`, never as part of computation. Keep this separation explicit:
write small `spec fn` predicates for "what must hold", and keep the
computation itself in `exec` code.

## Emitted Float And Aggregate Helpers

`emit_stubs.py` emits or imports a small set of spec helpers used by
translated `logic` expressions:

- `verus_f64_is_finite(x: f64) -> bool` — true iff `x` is not NaN and not
  infinite.
- `abs_f64(x: f64) -> f64` — absolute value as a spec function.
- `verus_sum_f64(s: Seq<f64>, n: int) -> f64` — recursive spec-mode sum over
  the first `n` elements of a sequence, used to translate `sum(i in 0..N:
  EXPR)` constraints.

These come from `contracts::verus::prelude` (or your `--contracts-crate`
equivalent) so generated stubs do not redefine them per module. If a
constraint needs a helper outside this set, add it to your contracts facade
crate rather than inlining ad hoc spec functions per stub.

## Arrays, Loops, And Quantifiers

For loops over arrays/`Vec`/slices, state the loop invariant in terms of
`old(self)` for the pre-state and `self`/local variables for the in-progress
state, and connect the loop index to both the modeled length and the
postcondition shape:

```rust
let mut i: usize = 0;
while i < self.values.len()
    invariant
        i <= self.values.len(),
        self.values.len() == old(self).values.len(),
        forall|j: int| 0 <= j < i ==> self.values[j] == old(self).values[j] * factor,
        forall|j: int| i <= j < self.values.len() ==> self.values[j] == old(self).values[j],
{
    self.values.set(i, self.values[i] * factor);
    i += 1;
}
```

Quantifiers (`forall`, `exists`) are spec-mode only. Bound every quantified
variable over a concrete range or sequence length — unbounded quantifiers
over `int` without a bounding hypothesis are rarely provable. For `exists`
obligations, prefer `choose` to extract a witness in proof mode rather than
asserting existence directly.

## Recursive Spec Functions And Proofs

For recursive structures or recursive aggregates (sums, folds, counts),
separate the computation from the proof: define a recursive `spec fn` with
an explicit `decreases` measure (e.g. remaining length, subtree size), and
prove properties about it with a separate `proof fn` that also carries a
`decreases` clause matching the recursion's structure.

```rust
pub closed spec fn sum_upto(s: Seq<f64>, n: int) -> f64
    decreases n
{
    if n <= 0 { 0.0 } else { sum_upto(s, n - 1) + s[n - 1] }
}

pub proof fn sum_upto_nonneg(s: Seq<f64>, n: int)
    requires
        0 <= n <= s.len(),
        forall|i: int| 0 <= i < s.len() ==> s[i] >= 0.0,
    ensures
        sum_upto(s, n) >= 0.0,
    decreases n
{
    if n > 0 {
        sum_upto_nonneg(s, n - 1);
    }
}
```

Induction-shaped proofs follow the same pattern: prove the base case
directly, then recurse on a strictly smaller measure while reusing the
inductive hypothesis from the recursive call. For sequence relations such as
"appending preserves a property" or "the result is a prefix/suffix of the
input", state the relation as a postcondition on the operation and prove it
by induction on sequence length, calling out the base case (empty sequence)
and the inductive step (one element appended/removed) separately.

## Limits

Verus is not the right tool for structural invariants over generic recursive
types with rich Pearlite-style algebra (use Creusot), or for unsafe/FFI
boundary checks (use Kani). If a generated stub lacks a `verus!` block
entirely, regenerate it with the current `emit_stubs.py` — current versions
always emit one for `"verifier": "verus"` concepts.

If a translated `logic` expression falls outside what `emit_stubs.py`'s
restricted-English translator supports, it emits `true /* TODO(concept-to-code):
... */` as a placeholder. Treat such constraints as unverified: either
rewrite the `logic` field into a supported form (see
`verifiers/verus/step-c-verify.md` for the supported subset) or add the
needed translation case to `emit_stubs.py` before relying on the contract.

When a Verus proof passes under `--no-verify`, that only confirms parsing,
mode-checking, and erasure succeeded — not that any `requires`/`ensures`
actually holds. Report lean-gate results as "syntactically valid, not yet
proof-checked" and keep that distinction visible until a full
solver-backed run (without `--no-verify`) succeeds.

Prefer the smallest local change that preserves an existing proof's
structure and invariants; do not restructure a passing proof for style or
generality alone.
