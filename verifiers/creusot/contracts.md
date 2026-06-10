# Creusot Contract Patterns

Use Creusot for pure Rust with mathematical structure: structural invariants,
algebraic operator properties, and reversibility/conservation laws. Generated
stubs use `contracts::creusot::*` through your project's verifier-contracts
facade crate (default name `contracts`, configurable via
`--contracts-crate`).

## Pearlite Basics

Creusot contracts are Pearlite terms. The `@` operator views executable
values as mathematical values, so prefer it when talking about lengths,
integers, and sequences.

```rust
#[cfg_attr(creusot, contracts::creusot::requires(children@.len() <= 2))]
#[cfg_attr(creusot, contracts::creusot::ensures(result@ >= 0))]
pub fn child_count(&self) -> usize { unimplemented!() }
```

Machine integers can overflow; mathematical integers cannot. If a contract is
about an unbounded count, use the model value with `@`. If the implementation
must preserve machine bounds, state both facts.

## Structural Invariants

Before adding a contract, identify the proof obligation category: model-value
bounds, structural shape, mutation preservation, recursive descent, loop
progress, or symbolic reversibility. Use the smallest Creusot annotation that
states that obligation clearly, and put recursive shape facts near the query
or command they constrain.

Example, for a tree-like structure:

- shape: child count is at most some fixed bound;
- root law: the root has no parent and a zero-valued accumulator;
- leaf law: leaf iff child count is zero;
- parameters: finite, ordered, non-negative, or normalized as needed.

Prefer small named predicates when several methods share an invariant. Keep
mutation specs focused on what changes and what is preserved — state the
changed field or relation directly and preserve only the relevant surrounding
invariant, rather than restating every global property.

For recursive structures or recursive predicates, always provide an explicit
well-founded measure such as subtree size, list length, or remaining fuel,
and add a `decreases` clause whenever recursion or structural descent is part
of the proof obligation. Do not rely on function names, data shape, or
"obvious" recursion as termination evidence.

```rust
#[cfg_attr(creusot, contracts::creusot::predicate)]
#[cfg_attr(creusot, contracts::creusot::decreases(self@.len()))]
fn well_formed(&self) -> bool {
    pearlite! { true }
}
```

Prefer `@` model views for mathematical lengths, counts, integer quantities,
and sequence facts. Only combine machine-level bounds with model-level facts
when the task explicitly requires both executable safety and mathematical
meaning.

## Reversibility And Operators

For state-transition operators, specify symbolic obligations first. Example,
for an operator with a conservation law:

```rust
#[cfg_attr(creusot, contracts::creusot::requires(old_state.well_formed()))]
#[cfg_attr(creusot, contracts::creusot::ensures(result.forward_log_weight == -result.reverse_log_weight))]
pub fn propose(&mut self) -> Proposal { unimplemented!() }
```

Use postconditions for conservation laws, symmetry/reversibility components,
and preserved domain constraints. Keep these obligations symbolic and
algebraic — do not encode probabilistic or distributional claims as Creusot
contracts; validate distributional behavior separately (e.g. statistical
tests against a reference implementation).

## Loops And Limits

Loops need explicit `#[invariant(...)]` clauses that restate the preserved
fact at every iteration, plus a `decreases` measure when the verifier must
see termination or monotone progress.

```rust
let mut i = 0usize;
while i < xs.len() {
    #[invariant(i@ <= xs@.len())]
    #[invariant(acc@ >= 0)]
    #[decreases(xs@.len() - i@)]
    {
        acc += xs[i];
        i += 1;
    }
}
```

Choose invariants that connect the loop index, the modeled collection length,
and the accumulated postcondition; include a `decreases` term whenever the
loop has monotone progress or termination is expected.

Creusot is not the right tool for unsafe Rust, concurrency, FFI, or tight
numerical kernels over large arrays — use Kani for unsafe/FFI and Verus for
loop-heavy numerical-array proofs. If a task fits the existing Pearlite,
structural invariant, loop invariant, recursion, mutation, or reversibility
patterns above, apply those patterns directly rather than inventing a new
contract style.
