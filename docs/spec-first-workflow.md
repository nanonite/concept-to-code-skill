# Spec-First Workflow

This workflow adapts Kiniry and Zimmerman's six-step BON to JML to static-check
path to a Rust workspace. The JSON artifact validated by
`schemas/spec.schema.json` is the handoff between concept analysis and
generated Rust stubs.

## Step 1: Concept Analysis

Paper step: identify concepts in restricted English before writing classes.

Schema fields: `concept`, `cluster`, and `english_description`.

The concept name records the thing being specified, while `cluster` groups it
into a domain such as `parsing`, `data-model`, or `numeric-kernel`. The
description must avoid Rust implementation vocabulary until the query,
command, and constraint vocabulary is stable.

## Step 2: Queries, Commands, and Constraints

Paper step: write BON queries, commands, and constraints in restricted
English.

Schema fields: `queries`, `commands`, `constraints`, and `adversary_table`.

Queries are observations and must be pure. The schema requires `pure: true`;
the later stub generator must reject query signatures that contain `&mut
self` or return `()`. Commands describe state-changing behavior or
constructors. Constraints carry both a human-readable sentence and a `logic`
expression in a Rust boolean subset so later tooling can emit verifier
annotations and property tests.

The `adversary_table` is required. It records counterexamples before
implementation starts, forcing each concept to name the edge cases that should
be rejected, normalized, or preserved for compatibility with any reference
implementation.

## Step 3: Module Skeleton

Paper step: create a module or class skeleton with the English comments
carried forward.

Schema fields: all query, command, and constraint `english` values.

Generated Rust modules must copy these English strings into Rustdoc verbatim.
This keeps the implementation traceable to the concept artifact and gives the
lint layer a direct way to detect drift between the JSON spec and generated
source.

## Step 4: Method Signatures

Paper step: add signatures while bodies remain assert-false stubs.

Schema fields: `queries[].rust_sig` and `commands[].rust_sig`.

The schema constrains signatures enough for deterministic generation without
pretending to be a full Rust parser. Step B tooling is responsible for deeper
checks: queries must be immutable and value-returning, commands may mutate
state, and generated bodies remain explicit missing-body stubs until
implementation starts.

## Step 5: Contracts

Paper step: translate BON constraints into JML preconditions, postconditions,
invariants, and purity annotations.

Schema fields: `constraints[].kind`, `constraints[].logic`,
`constraints[].applies_to`, and `verifier`.

The selected `verifier` chooses the first contract target: `kani`, `creusot`,
or `verus`. Invariants become type-level checks where Rust can express them
directly, and verifier obligations where it cannot. Preconditions and
postconditions attach to the generated query or command named in
`applies_to`.

Your project should document per-crate or per-module verifier defaults
somewhere (e.g. an architecture decision record) and reference that document
here; absent such a decision, default new crates to Kani until one is
recorded.

**Creusot logic-model companions.** For `verifier: "creusot"`, a query
returning an integer type, `bool`, `f64`, or `Option<f64>` gets a generated
`<query>_model()` (or, for a chained `self.<q>(args).<term>()` call ending in
`.len()`/`.size()`/etc., a `<q>_<term>_model()`) trusted/opaque Pearlite logic
companion, and constraints referencing that query are rewritten to call the
companion instead of the program method (which Creusot rejects in logic
context). A constraint still referencing an unmodeled query (string/reference
returns) degrades to a visible `TODO(concept-to-code)` sentinel rather than
silently passing. `&mut self` postconditions are rewritten into Pearlite's
`*self`/`^self` prophecy notation automatically.

**Trait/enum concept kinds.** `kind: "trait"` (a `pub trait` with contracts
on bodyless method declarations) and `kind: "enum"` (a closed `pub enum`
dispatching to named concrete variants) are the composition mechanism for a
shared interface implemented by several concrete concepts, for verifiers that
cannot reason about `dyn Trait` (Creusot). Reach for `kind: "trait"` at Step 1
when the concept under analysis genuinely describes a shared interface rather
than one concrete behavior; most concepts stay `kind: "struct"` (the
default). See `tests/fixtures/trait_enum_demo/` for a worked example and
SKILL.md's "Concept kinds" section for the schema fields involved
(`implements`, `trait_ref`, `variants`).

**Supplementary Kani f64 checks.** If your Creusot toolchain's Pearlite
logic lacks `f64: OrdLogic` (or another f64 sign/finiteness obligation your
contracts need), add `kani_f64_checks` to a Creusot-primary concept at
implementation time (not at Step 4/5) rather than degrading the whole
concept's verifier choice. This generates a supplementary
`tests/kani_f64_<concept>.rs` Kani harness checking exactly the f64
obligations Creusot logic can't express yet, alongside the concept's
Creusot-checked contracts. See `tests/fixtures/kani_f64_demo.json` and
`verifiers/creusot/step-c-verify.md`'s "Supplementary Kani f64 gate" section.

## Step 6: Static Checking

Paper step: run the checker before real method bodies are written.

Schema fields: `verifier`, plus the complete query, command, and constraint
set.

The expected early signal is that signatures and contracts are well formed
even though generated bodies are intentionally incomplete. This static pass
complements any reference-implementation or oracle validation your project
runs separately.

## Artifact Lifecycle

1. Write or regenerate a JSON concept file that validates against
   `schemas/spec.schema.json`.
2. Generate Rust stubs from the JSON without hand-editing signatures.
3. Run the selected verifier in stub-check mode. For Kani stub checks, use
   `cargo kani -p <crate> --tests --only-codegen`; this confirms contracts
   compile and harnesses link without invoking CBMC or the SAT solver.
4. Implement bodies only after the concept, adversary table, signatures, and
   contracts are reviewed.
5. Run full Kani verification only after implementation on selected critical
   methods, or in release/nightly CI. For constrained local full runs,
   consider resource-capping the process, e.g.:
   `systemd-run --user --scope -p MemoryMax=4G -p CPUQuota=200% cargo kani -p <crate> --tests --harness-timeout 300`
   (optional, environment-specific — adapt or drop for your setup).
6. Keep the JSON spec, generated Rustdoc, contracts, and any
   reference-implementation validation report together in the PR.
