# Step A: Concept And Co-Analysis

Goal: produce one JSON concept artifact that conforms to
`schemas/spec.schema.json` and can drive deterministic Rust stub generation.

## Inputs

- User concept name or problem statement.
- Relevant papers, oracle classes, reference-implementation code paths, or
  issue references.
- Repository schema: `schemas/spec.schema.json`.

## Protocol

1. Stay in restricted English. Before the concept, queries, commands,
   constraints, and adversary table are complete, do not use these Rust
   declaration words: `struct`, `enum`, `trait`, `fn`, `impl`, `generic`.
2. Identify the concept and cluster.
3. Write the concept's `english_description` without implementation
   vocabulary.
4. Write queries as pure observations. Each query needs `english`,
   `rust_sig`, and `pure: true`.
5. Write commands as state-changing behaviors or constructors. Each command
   needs `english` and `rust_sig`.
6. Write constraints as invariants, preconditions, or postconditions. Each
   constraint needs `english`, `logic`, optional `kind`, optional `source`
   (defaults to `hand`), and optional `applies_to`.
7. **Optional: trace-derived invariant sidecars.** If your project maintains
   trace-inferred invariant sidecars (e.g. produced by Daikon) for one or more
   reference implementations, under a path your project documents, triage them
   before writing constraints — do not blind-copy:
   - Sidecar entries typically carry `english` and a trace-derived invariant
     expression but usually **no `logic` field** (the schema requires
     `logic`). To promote one you must **translate** it: map the traced field
     reference to a concept query, and the trace-derived invariant to a
     Rust-boolean `logic` expression. Keep `source: "daikon"` on the result.
   - **Reject trace artifacts.** These are typically short-chain, low-N
     traces; entries like `return == 0`, `this has only one value`, or
     `this.field == orig(this.field)` (a getter that merely does not mutate)
     are sampling noise, not invariants. Drop them.
   - **Cross-reference reconciliation.** Where sidecars from multiple
     reference implementations agree, promote once and cite all of them in
     `source_references` (strongest evidence). Where a candidate appears in
     only one, treat it as weak — absence from another is *not* evidence of
     absence there. Where reference implementations **contradict** each other,
     file an adversary-table row recording the divergence and your project's
     chosen resolution (intersect to the strictest, or carry a discriminant
     for the differing behavior) before advancing.
   - Record how many candidates you promoted vs rejected so the review is
     auditable.

   If your project has no such sidecars, skip this step entirely.
8. Write an adversary table. This is mandatory. Each row needs `scenario`,
   `violates`, and `resolution`.
9. Select exactly one verifier: `kani`, `creusot`, or `verus`.
10. Include source references when available.
11. Write a findings report at `<findings-dir>/<concept-kebab>.md`, where
    `<findings-dir>` is a path your project defines (e.g. `findings/<crate>/`
    or a flat `findings/`). The report should include: (a) any trace-sidecar
    triage counts, if used (promoted / rejected / "no logic available, kept as
    english"), (b) cross-reference reconciliation notes for any divergent
    reference implementations and how each was resolved, (c) sub-tickets split
    out during analysis, (d) any tolerance entries that needed loosening, and
    (e) the rationale for any user-facing mode the spec introduces.

## Hard Gate

Refuse to advance to Step B if `adversary_table` is absent or empty. Ask for
concrete adversarial cases instead.

## Output

Emit JSON only, conforming to `schemas/spec.schema.json`. Use
`schema_version: "1.0"`.
