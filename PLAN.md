# Upstream port plan — model companions, hybrid Kani-f64 verifier, trait/enum concept-kinds

**Status:** Phases 0-3 implemented and landed (commit `0bf9366`, 2026-07-06).
A second fork-drift review and follow-up bug-fix pass landed 2026-07-09 (see
addendum at the end of this file). Originally written 2026-07-05 for a
dedicated session to execute against.

**Source of truth for everything below:** `/home/goya/beast-workspace/workspace`,
specifically `skills/beast-rs-spec-first/emit_stubs.py` (2455 lines) and
`docs/spec-schema.json`, which forked from this repo (`concept-to-code`,
`emit_stubs.py` 1170 lines) at some earlier point and has since accumulated
substantial independent work. This plan inventories every function/constant
that exists in the beast-rs fork but not here, categorizes each, and proposes
what to port, what to generalize, and what to leave behind.

## Why this exists

A session working on beast-rs hit two confirmed verifier limitations back to
back:

1. Creusot's `f64` type has no `OrdLogic` in `creusot_std` — sign/finiteness
   predicates on `f64` cannot be expressed in Pearlite logic at all. Worked
   around with a **hybrid verifier** pattern: a Creusot-primary concept can
   carry supplementary `kani_f64_checks`, generating a `tests/kani_f64_<concept>.rs`
   Kani harness that checks exactly the f64 obligations Creusot can't.
2. Creusot has no support for `dyn Trait` (trait-object) reasoning at all —
   confirmed against Creusot's own docs and independently by a second LLM
   query. Composing a closed set of concrete types behind one interface (the
   beast-rs case: an MCMC `Operator` trait with several concrete operator
   implementations feeding one scheduler) needed a **trait + enum
   concept-kind** extension: `kind: "trait"` emits a real `pub trait`,
   `implements` routes a concrete concept's methods into `impl Trait for
   Concrete`, and `kind: "enum"` emits a closed enum wrapping concrete
   variants with generated `match` dispatch — the standard accepted
   workaround for deductive Rust verifiers generally, not a beast-rs-specific
   hack.

Both were built directly in the beast-rs fork under time pressure, using
beast-rs's own conventions (hardcoded `beast_rs_contracts::` paths, a
workspace-wide `crates/<crate>/specs/` convention for cross-crate lookups).
This repo (`concept-to-code`) is the generic, project-agnostic base the fork
came from — this plan is how those two mechanisms (and everything they turned
out to depend on) come back upstream, generalized instead of copy-pasted.

**Explicitly out of scope:** the beast-rs fork also accumulated several
*unrelated* bug fixes (see "Excluded" section below) discovered while working
on beast-rs-specific concepts. Do not port these opportunistically just
because they're sitting in the same file — evaluate each on its own merits in
a separate pass if wanted, but they are not part of what this plan is for.

## Scope and phase ordering

Three phases, in dependency order — phase 2 depends on phase 1's `Int`/`bool`/
`f64` model-companion machinery; phase 3 (hybrid verifier) is independent and
can be done in parallel with phase 1/2 if split across sessions.

1. **Phase 1 — Creusot logic-model companion subsystem** (chainlink #347/#348/
   #349 in beast-rs). Prerequisite for phase 2 to avoid silently reintroducing
   the exact regression beast-rs's own session caught and fixed: without this,
   a `kind: "trait"` concept's constraints referencing `self.<query>()` all
   degrade to `TODO` sentinels, even ones that don't need it.
2. **Phase 2 — Trait and enum concept-kinds** (beast-rs ADR-0004).
3. **Phase 3 — Hybrid Kani-f64 verifier** (`kani_f64_checks`). Independent of
   1/2; can land first, last, or in parallel.

Plus two smaller, clearly-generic **Phase 0** fixes worth doing first since
they're small, self-contained, and improve the existing (already-ported)
Kani/Creusot pipeline regardless of the other three phases:

- **Kani precondition/postcondition emission is currently wrong here.**
  `emit_plain_impl` (this repo, line ~914) always emits
  `#[cfg_attr(kani, kani::requires({logic}))]` regardless of whether the
  constraint is actually a precondition or postcondition. beast-rs's
  `rewrite_static_self_for_kani` + `translate_logic_to_kani` (see beast-rs
  `emit_stubs.py` around the `emit_method_contract_attrs` Kani branch) checks
  `constraint.get("kind") == "postcondition"` and emits
  `kani::ensures(|result| ...)` with `self.*` rewritten to `result.*` for
  static/constructor methods. This is a real correctness bug in the current
  Kani path here, unrelated to any of the three phases — fix it regardless of
  how much of the rest of this plan gets done.
- **Creusot quantifier trigger handling (`#458`)**: `creusot_quantifier_bound_vars`,
  `_extract_trigger_candidates`, `_select_trigger_terms`,
  `_TRIGGER_HELPER_CALL_RE`/`_TRIGGER_IDENT_RE`/`_TRIGGER_RECEIVER_CALL_RE`.
  General `forall`/`exists` correctness fix for Creusot, not beast-rs-domain
  specific. Worth a look in the same pass as phase 1 since it touches the same
  Creusot logic-rewriting code paths.

## Required generalization work (not just copy-paste)

Two beast-rs conventions are hardcoded and need a real design decision here,
not a blind port:

1. **Contracts-crate path.** beast-rs hardcodes
   `beast_rs_contracts::contracts::creusot::...` as a literal string in every
   new function this session wrote (`emit_trait_def`, `_emit_impl_method`,
   etc.). This repo already solved this generically via the module-level
   `CONTRACTS_CREUSOT`/`CONTRACTS_VERUS_PRELUDE` globals, reassigned in
   `main()` from `--contracts-crate`. Every ported function must use those
   globals, not a hardcoded crate name — check every ported function for
   accidental hardcoding before considering it done.
2. **Cross-crate spec resolution.** beast-rs's `resolve_cross_crate_spec`
   (needed for `implements`'s `crate` field and `trait_ref`/`variants`)
   assumes a fixed, workspace-wide `crates/<crate>/specs/<snake_case(concept)>.json`
   layout, rooted at a hardcoded `REPO_ROOT = Path(__file__).resolve().parents[2]`.
   This repo has no such built-in multi-crate assumption — it's invoked once
   per `--crate-dir` with no workspace-root concept at all. Needs a new,
   generic mechanism: likely a `--specs-search-root <path>` CLI flag (default:
   parent of `--crate-dir`, so single-crate-per-invocation still works
   unchanged) plus a *configurable* path template rather than beast-rs's
   hardcoded `crates/<crate>/specs/`. Decide the exact convention as part of
   phase 2 — don't just copy beast-rs's assumption that every consuming
   project is itself a multi-crate Cargo workspace with a `crates/` directory.

## Phase 1 — model-companion subsystem, detailed inventory

All from beast-rs `emit_stubs.py`. None of these reference beast-rs domain
concepts; all are generic Creusot-correctness machinery.

| Symbol | Purpose |
|---|---|
| `CREUSOT_INT_RETURN_TYPES` | Set of Rust return types (`usize`, `u64`, `i64`, ...) treated as `Int`-modelable. |
| `creusot_logic_type(ret_ty)` | Maps a Rust return type to its Pearlite logic type (`Int`/`bool`/`f64`/`Option<f64>`) or `None` if unmodelable. |
| `creusot_query_model_companions(methods)` | For each `Int`/`bool`/`f64`/`Option<f64>`-returning query, derives a `<name>_model` companion function name. |
| `creusot_chain_model_companions(methods, spec)` | Same, for chained `self.<q>(args).<term>()` calls (`.len()`, `.size()` on a query's result) — see `#349`. |
| `_creusot_model_args(args)` | Rewrites a method's argument list for use inside a `#[logic]` companion signature. |
| `_creusot_usize_param_names(method)` / `_creusot_domain_index_param_names(method)` | Identify which parameters need a `@` mathematical-view suffix or a domain-newtype `.0@` unwrap in Pearlite logic context. |
| `creusot_query_return_types(methods)` | Name -> declared return type map, used by the `#319` string-view-mismatch fix. |
| `rewrite_string_view_mismatches_for_creusot(logic, method, methods)` | Fixes `result.<getter>() == <param>` where the getter returns `&str`/`Option<&str>` but the constructor parameter is the owned `String`/`Option<String>` (`#319`). |
| `_replace_query_calls(expr, model_map)` / `_replace_chain_calls(expr, chain_map)` | Reroutes `self.<query>(...)`/chained-terminal calls to their `_model` companions inside constraint logic before translation. |
| `_replace_creusot_option_f64_equalities(expr, option_names)` | Fixes `Option<f64>` equality comparisons in Pearlite (can't use plain `==` on `Option<f64>` the way you can on `Option<Int>`). |
| `_rewrite_mut_self_prophecy_for_creusot(logic)` | For `&mut self` postconditions: rewrites `old(self.X())` -> `(*self).X()` and bare `self.X()` -> `(^self).X()`, Creusot's prophecy notation (`#348`). |
| `creusot_logic_unmodeled(rerouted, model_names)` | After rerouting, detects any remaining `.method(` call not covered by a model/helper -> triggers the `TODO(#347)` sentinel. This repo's `creusot_needs_query_model` is the direct predecessor; **replace it**, don't run both. |
| `emit_creusot_query_model_companion(lines, method, model_name, *, pub=True)` | Emits the `#[trusted] #[logic(opaque)] fn <name>(...) -> <ty> { pearlite! { <default> } }` companion. **Port with the `pub` parameter already added in beast-rs** (needed by phase 2, not optional). |
| `emit_creusot_chain_model_companion(lines, method, model_name, *, pub=True)` | Same, for chain-call companions. Same `pub` parameter note. |
| `parse_arg_types(args)` | Name -> declared type map for a method's argument list. Also needed standalone by phase 2's enum dispatch (forwarding argument names). |
| `CREUSOT_SCOPE_RESERVED` / `CREUSOT_IGNORED_CALLS` | This repo already has equivalents (`CREUSOT_USIZE_MODEL_CALLS` etc. exist here — diff carefully against beast-rs's version rather than assuming a clean 1:1 replace; beast-rs likely extended these sets, didn't replace them). |

**Also refactor while porting** (this was done in beast-rs specifically to
support phase 2, worth doing here too regardless): beast-rs's
`compute_creusot_maps(spec, methods)` and `emit_method_contract_attrs(...)`
extract the per-method contract-attribute-emission logic (previously
duplicated inline inside `emit_plain_impl` alone) into standalone functions so
both the inherent-impl path and the future trait-declaration path share one
implementation instead of drifting apart. Port this refactor as part of phase
1, not phase 2 — it's a pure extraction with no new behavior.

## Phase 2 — trait and enum concept-kinds, detailed inventory

Full design already written up for the beast-rs case:
`/home/goya/beast-workspace/workspace/docs/decisions/0004-trait-and-enum-concept-kinds.md`
and `/home/goya/beast-workspace/workspace/findings/T-476-trait-and-enum-concept-kinds.md`
(the second file also documents the model-companion regression that was
caught and fixed in the same pass — read it before implementing here, so the
same mistake isn't repeated in this port).

Schema additions (from beast-rs `docs/spec-schema.json`, generalize the
wording — beast-rs's descriptions cite `Operator`/`UpDownOperator` as the
motivating example; replace with a generic placeholder, e.g. this repo's own
`tests/fixtures/example_concept.json`-style naming):

- `kind`: `"struct" | "trait" | "enum"`, default `"struct"`.
- `implements`: object, struct-kind concepts only, maps a trait-kind concept
  name to `{crate/root-reference, methods: [...]}` (adapt the `crate` field
  name once the cross-crate generalization above is decided — it may not be
  called `crate` here if this repo's resolution mechanism ends up being
  path-based rather than crate-name-based).
- `trait_ref` + `variants`: enum-kind concepts only, cross-crate/cross-file
  reference to the trait plus the list of wrapped concrete variants.
- Conditional `required`: `queries`/`commands`/`constraints`/`adversary_table`
  required unless `kind == "enum"` (via `allOf`/`if`/`then`, see beast-rs
  schema for the exact JSON Schema shape — it's a clean, reusable pattern).

Generator additions:

- `concept_kind(spec)` — reads `kind`, defaults to `"struct"`, validates the
  3-value enum.
- `validate_enum_spec(spec)` — lighter validation path for `kind == "enum"`
  (no queries/commands of its own).
- `emit_trait_def(lines, spec, methods)` — emits `pub trait X { ... }`,
  bodyless contract-bearing method declarations, **plus the model-companion
  emission** (this is the piece that was nearly omitted in beast-rs — see the
  findings file's "regression found and fixed" section for exactly why
  omitting it is wrong, not just incomplete).
- `_emit_impl_method(...)` — shared per-method emission for both inherent-impl
  and trait-impl methods (`emit_contracts`/`pub` flags).
- Modified `emit_plain_impl` — `implements` routes named methods into
  `impl Trait for Concept` with contract-attribute emission suppressed for
  those specific methods (Creusot checks refinement from the trait's own
  declaration automatically).
- `emit_enum_impl(lines, spec)` — resolves the trait cross-reference, emits
  `pub enum X { Variant(Concrete), ... }` plus one match-dispatched inherent
  method per trait method. No `dyn` anywhere.
- `emit_method_body` gains `pub: bool = True, body: bool = True` parameters —
  trait declarations and trait-impl methods are never individually
  `pub`-qualified in Rust; trait declarations have no body.
- `emit_props` must special-case `kind == "enum"`: an empty
  `constraints`/`adversary_table` would generate a `0usize..0` proptest range,
  which panics at test time, not just produce zero cases. Emit a doc-only
  placeholder file instead.
- `resolve_cross_crate_spec` — see the generalization note above; do not port
  the hardcoded `crates/<crate>/specs/` convention verbatim.

**Verification approach that worked well in beast-rs, repeat here:** write
throwaway smoke-test specs (a toy trait + a struct implementing it + an enum
wrapping it) using this repo's own `tests/fixtures/` convention, run them
through the generator, inspect the generated Rust by hand (or better, add
them as real fixtures under `tests/fixtures/` and assert on the generated
output in `test_emit_stubs.py` — this repo already has a real pytest-style
test suite beast-rs's ad-hoc validation script doesn't, so formalize the
smoke test as permanent regression coverage here rather than a throwaway).

## Phase 3 — hybrid Kani-f64 verifier, detailed inventory

Schema: the `kani_f64_checks` field and its `$defs/kani_f64_check` sub-schema
in beast-rs's `docs/spec-schema.json` are **already fully generic** — no
beast-rs vocabulary in the field description or property names. Copy
verbatim:

```json
"kani_f64_checks": {
  "type": "array",
  "minItems": 1,
  "items": { "$ref": "#/$defs/kani_f64_check" },
  "description": "Optional implementation-stage Kani harnesses supplementing a Creusot-primary concept when f64 sign or finiteness obligations cannot be expressed in Creusot logic. Presence generates tests/kani_f64_<concept>.rs."
}
```

(plus the `$defs/kani_f64_check` object itself — `name`, `symbolic_f64s`,
`assumptions`, `statements`, `assertions`, `expected` — see beast-rs's schema
for the full shape.)

Generator: `emit_kani_f64_harness(spec)` in beast-rs `emit_stubs.py`. Also
requires:

- `default_paths` extended from a 2-tuple `(module, props)` to a 3-tuple
  `(module, props, kani_f64)`.
- `main()` wiring: `--kani-f64-out`, conditional emission only when
  `spec.get("kani_f64_checks")` is present, matching the existing
  `--module-out`/`--props-out` pattern already in this repo.

Docs: beast-rs's ADR-0001 documents *why* this exists (Creusot's f64
`OrdLogic` gap) and the exact process (`kani_f64_checks` is added at
body-implementation time, not at Step A/B) — that reasoning belongs in this
repo's `docs/spec-first-workflow.md` or a new doc, generalized away from
beast-rs's specific chainlink ticket numbers (`#421`/`#422`), stated as "if
your Creusot toolchain version lacks `f64: OrdLogic`" rather than asserting
it as a permanent fact (Creusot is actively developed; this gap may close in
a future `creusot_std` release — phrase the docs so they age well).

## Documentation to update once phases land

- `SKILL.md` / `README.md` — file map additions (new schema fields, new
  generator functions), Hard Rules section stays unchanged (the restricted-
  English vocabulary ban already correctly excludes `struct`/`enum`/`trait`
  from *prose*, which was never in tension with these `kind` values being
  legal in `rust_sig`/generated code).
- `docs/spec-first-workflow.md` — Step 5 (Contracts) should mention the
  model-companion mechanism and the trait/enum kind's effect on where
  contracts land.
- `prompts/step-a-coanalysis.md` — needs a note that `kind: "trait"` is a
  legitimate Step A decision when a concept describes a shared interface
  multiple concrete concepts satisfy, and `kind: "enum"` is the composition
  mechanism for a closed set of them — without implying every project needs
  this (most concepts stay plain structs).
- `verifiers/creusot/contracts.md` / `verifiers/creusot/step-c-verify.md` —
  document the model-companion pattern and the hybrid Kani-f64 supplementary
  gate (mirrors beast-rs's own `verifiers/creusot/step-c-verify.md` "Supplementary
  Kani f64 gate" section — generalize away the beast-rs-specific
  `benchmark_task_gate_creusot.py`/chainlink references, keep the mechanism).
- `tests/fixtures/example_concept.json` — consider adding a second fixture
  demonstrating `kind: "trait"` + `implements` + `kind: "enum"`, matching the
  existing single-fixture convention.

## Testing strategy

This repo already has a real test suite beast-rs's own ad-hoc `spec_first_workspace.py check` doesn't:
`tests/test_emit_stubs.py`, `tests/emit_stubs_test.py`,
`tests/gate_selftest_{kani,creusot,verus}.py`. Use it properly, don't
reinvent beast-rs's dry-run-and-diff approach:

1. Run the full existing suite before touching anything, to get a clean
   baseline.
2. After each phase, re-run the full suite — zero regressions is the bar,
   same principle as beast-rs's own `spec_first_workspace.py check`
   zero-drift gate.
3. Add new unit tests (not just throwaway smoke-test files) for: a
   trait-kind concept's generated output, an `implements` concept's contract
   suppression, an enum-kind concept's match dispatch, and a
   `kani_f64_checks` concept's generated harness. Use `tests/fixtures/` for
   the input specs, assert on generated output shape in
   `tests/test_emit_stubs.py`.
4. `gate_selftest_kani.py`/`gate_selftest_creusot.py` are real
   verifier-backed tests (opt-in via `SPEC_RUN_{KANI,CREUSOT}_SELFTEST=1`) —
   run these too if `cargo-kani`/`cargo-creusot` are available in the
   dedicated session's environment, since they're the closest thing this repo
   has to beast-rs's own `nix develop .#verifier -c cargo creusot ...` lean-gate
   checks.

## Addendum (2026-07-06): fork advanced further after this plan was written

The beast-rs fork kept moving after this plan's 2455-line snapshot
(`36c4a65`, "feat(spec-first): implement trait and enum concept-kinds
(#476)"). Four more commits landed on `emit_stubs.py` before this session
started implementing; triaged each against the phase boundaries above rather
than blindly re-diffing against a moving target. Reference reading for this
implementation pass is pinned to `a9ce760` (`git show a9ce760:skills/beast-rs-spec-first/emit_stubs.py`),
not the live working tree, specifically to exclude the three commits below.

- **`a9ce760` "relocate ChainState to beast-rs-core, dedupe generator
  dual-cfg gaps (#477)"** — **included**, folded into Phase 2 as originally
  scoped rather than called out separately. The "ChainState relocation" half
  is beast-rs-domain-specific and out of scope, but the "dedupe generator
  dual-cfg gaps" half is three same-day bug fixes to the *exact* trait/enum
  concept-kind feature Phase 2 ports (`emit_trait_def`'s method declarations,
  `emit_enum_impl`'s dispatch methods, and the enum's required trait-import
  in `emit_module`, all missing the `#[cfg(creusot)]`/`#[cfg(not(creusot))]`
  dual-signature split that `_emit_impl_method` already had). Porting Phase 2
  from `36c4a65` alone would silently reintroduce these three bugs into a
  brand-new feature before it ever shipped here — same category of mistake
  the plan already warns about for the Phase 1 model-companion regression
  (see the T-476 findings file reference above). Treated as one unit with
  Phase 2, not a separate phase.
- **`7e55310` "depth-aware signature parser for nested parens (#276)"** —
  **excluded**. General `method_signature_parts` robustness fix (handles
  `Vec<(A, B)>`-shaped nested-paren argument types) with no dependency from
  any Phase 0/1/2/3 item; none of this repo's fixtures need it. Same
  "unrelated bug fix found in the same file" category as the rest of this
  section.
- **`e2dc3eb` "referenced_verus_constants skips path members and string
  literals (#317)"** — **excluded**. General Verus placeholder-constant
  correctness fix, orthogonal to model companions and trait/enum kinds.
- **`f126ab1` "auto-suffix bare integer literals vs raw integer params in
  Creusot (#486)"** — **excluded**. Adds `_suffix_integer_literals_for_creusot`
  / `_creusot_non_usize_integer_param_types` to fix a distinct Creusot
  integer-literal-typing gap (`log_every > 0` where `log_every: u64`).
  Unrelated to the model-companion subsystem Phase 1 ports; the sentinel it
  fixes only bites non-usize integer params, none of which appear in this
  repo's existing fixtures.

If a future session wants these three excluded fixes, treat them the same
way as the rest of the "Excluded" section below: evaluate and port
individually, not opportunistically.

**Also excluded, uncommitted in the fork's working tree as of 2026-07-06**:
an in-progress fix to `_replace_creusot_f64_comparisons`/`translate_logic_to_creusot`/
`emit_method_contract_attrs`, threading a `non_f64_names` set through so the
f64-comparison name-substring heuristic (`branch_length`, `weight`, `density`,
... — see the next paragraph) stops false-positiving on names that merely
*contain* one of those keywords as a substring (their own example:
`chain_length`, a `u64` state count, matching the `length` keyword meant for
genuine f64 fields like `branch_length`). Moot for this port regardless of
whether it lands upstream: this repo's `_replace_creusot_f64_predicates`/
`_replace_creusot_f64_comparisons` were never replaced with beast-rs's
domain-specific, hardcoded-field-name heuristic versions in the first place
(see "Phase 1 — model-companion subsystem, detailed inventory" above — those
two functions are deliberately absent from that inventory table; this repo
keeps its own narrower, name-heuristic-free versions). A fix to a heuristic
this port never imported has nothing to port.

## Excluded from this plan (found during the diff, deliberately not included)

These exist in beast-rs's fork but are unrelated to the three phases above —
general Verus-pipeline correctness fixes discovered while working on
beast-rs-specific concepts. Worth a look in a separate, later pass, not
bundled into this one:

- `_extract_trigger_candidates`, `_select_trigger_terms`,
  `_TRIGGER_HELPER_CALL_RE`/`_TRIGGER_IDENT_RE`/`_TRIGGER_RECEIVER_CALL_RE` —
  actually needed alongside `creusot_quantifier_bound_vars` per Phase 0 above
  (same `#458` fix, spans both Creusot and Verus trigger handling — re-check
  when implementing Phase 0 whether these are separable or one unit).
- `format_verus_f64_literal`, `load_verus_tolerance_registry`,
  `_VERUS_FLOAT_CAST_INTERMEDIATE`, `verus_query_int_cast_map`,
  `_fix_int_to_float_casts` — Verus float-literal formatting and int-to-float
  cast fixes, tied to beast-rs's `validation/tolerance.toml` file convention
  (`TOLERANCE_TOML_PATH`). Generalizable (a `--tolerance-toml <path>` flag
  instead of a hardcoded project-relative path) but not attempted here.
- `localize_verus_error_types`, `_replace_external_companion_calls`,
  `verus_constraint_needs_external_param_sentinel`,
  `verus_domain_object_param_names`, `verus_external_spec_companion_calls` —
  the `verus_external_spec_companions` cross-crate trusted-shim mechanism
  (beast-rs `#467`). Same category of "hand-authored trusted boundary" as the
  Verus prelude already documents; likely worth porting eventually since it's
  general Verus cross-type-boundary handling, but it's a fourth substantial
  subsystem on top of the three this plan already covers — scope it
  separately rather than growing this plan further.
- `REPO_ROOT`/`TOLERANCE_TOML_PATH` as beast-rs defines them are workspace-
  root-relative to *that specific repo's* file layout — the generalized
  cross-crate resolution mechanism (see "Required generalization work" above)
  should not reuse this exact constant, even though phase 2 needs something
  that plays a similar role.

## Addendum (2026-07-09): second fork-drift review and follow-up fix pass

The beast-rs fork kept moving after the first port pass landed (`0bf9366`).
Six more commits touched `emit_stubs.py` on the fork between the first
pass's `a9ce760` reference point and this review (`d3a17ba`, current HEAD at
review time). Triaged each the same way as the first pass — porting generic
correctness fixes to the exact model-companion/forall machinery already
ported, leaving out anything beast-rs-domain-specific or not yet
generalizable.

**Ported this pass** (all generic bugs in code this repo already carries,
none beast-rs-domain-specific):

1. `creusot_constraint_in_scope` now adds `"result"` to scope
   unconditionally, not just for `Result`-returning or self-less methods —
   Creusot's `ensures` binds the return value as `result` for every return
   type (fork `#531`).
2. `creusot_free_identifiers` strips `/* ... */` and `//` comments before
   scanning for free identifiers (same fork `#531` commit).
3. Verus `_replace_sums`/`_replace_products` now use a word-boundary-aware
   regex instead of `str.find("sum("` / `.find("product("`, which
   false-matched inside longer identifiers like `checksum(...)`.
4. **The consequential one**: `rewrite_static_self_for_creusot` and
   `rewrite_result_methods_for_creusot` no longer rewrite a self-less
   `Result`-returning constructor's postcondition to
   `result.as_ref().unwrap().foo()` — Creusot rejects `as_ref`/`unwrap` in
   logic context. Both now emit Pearlite's `match result { Ok(ok_result) =>
   ..., Err(_) => true }` form instead. This was silently forcing every
   Result-returning constructor's query-based constraints to the
   `TODO(#347)`-style sentinel in the first port pass instead of real
   Pearlite — exactly the case Phase 1's model companions were supposed to
   unlock. Required also extending `_replace_query_calls`/
   `_replace_chain_calls`'s receiver alternation to recognize `ok_result`,
   or the match-form rewrite alone would still leave the query call
   unrerouted and hit the sentinel anyway (caught by tracing the fix
   through by hand before applying it, not by the upstream diff alone).
5. `_model_creusot_usize_calls`: a bare-identifier `Vec`/slice parameter's
   `.len()` now becomes `param@.len()` (Seq view), not `param.len()@`
   (rejected program call) — `self`/`result`/`ok_result` receivers are
   excluded since those are already rerouted to model companions earlier in
   the pipeline.
6. Verus: a bare (non-quantified) identifier used alone as a whole slice
   index now gets an explicit `as int` cast (`_cast_bare_slice_index_to_int`)
   — `Seq::spec_index` requires it; only a *compound* index expression
   already containing a quantified `int` operand type-checks via arithmetic
   promotion without one.
7. `_replace_creusot_forall` now supports a bare `forall <vars>: BODY` clause
   with no `in 0..` range (e.g. quantifying over a domain value rather than
   an index), falling back to the existing range-clause parser first and
   only using the bare form when that fails. Ported as a minimal addition to
   this repo's own pre-existing (already-diverged-from-beast-rs) untyped
   `forall<vars>` convention, not a wholesale copy of beast-rs's own
   `Int`-typed/AND-OR-substituting version of this function.
8. `.is_some()`/`.is_none()` now rewrite to `!= None`/`== None` in Pearlite
   (`_replace_creusot_option_predicates`) instead of hitting the
   unmodeled-call sentinel — `Option::is_some`/`is_none` are program
   methods, not logic functions, but direct `None` comparison is valid
   Pearlite.

All 8 are covered by `CreusotResultAndForallUpstreamFixTests` in
`tests/emit_stubs_test.py`, plus the pre-existing
`test_creusot_static_result_constructor_rewrites_self_to_result` updated to
assert the new match-form output instead of the old sentinel.

**New feature found, not ported — needs generalization first**:
`structural_opt_out` (fork `#513`), a whole-concept "this is definitionally
outside what any verifier can prove" escape hatch (for runtime-wiring/
dispatch/orchestration/live-UI-state concepts). Genuinely useful shape, but
its schema is deep beast-rs plumbing as committed: a fixed `category` enum
tied to beast-rs's own crate layout, a `tracking_issue` field requiring the
literal pattern `^chainlink:[0-9]+$`, and an `architectural_alternative_
considered` field whose own description references beast-rs's
`dyn-trait-creusot-ice-plan.md` by name. Would need a project-agnostic
category taxonomy and tracking-reference pattern before it's portable —
scope as a separate pass if wanted.

**Correctly left out (beast-rs-domain-specific, not generalizable as
committed)**: `creusot_param_chain_model_companions` /
`emit_creusot_param_chain_model_companion` / `_replace_param_indexed_chain_calls`
(param-indexed chain models for `<param>[i].<term>()`, e.g. `taxa[i].label()`
— even upstream's own version has a hardcoded `Vec<crate::taxon::Taxon>`
fallback type baked in, a code smell in the fork itself); the `"label"`
chain-terminal vocabulary added to `CREUSOT_USIZE_MODEL_CALLS`; the
RootLikelihoodIntegrator spec JSONs (pure beast-rs domain content, not
generator code); `verifier_override_justification`'s updated description
(this repo has no such field — beast-rs's own ADR-0001 crate-to-verifier
defaults don't apply here).

**Also noted, pre-existing, out of scope for this pass**: `_model_creusot_
usize_calls` in this repo already carried several hardcoded beast-rs-domain
literals (`ROOT_PARENT_SENTINEL`, `TAXON_LABEL_MAX_LEN`, and the
special-cased `index`/`node` parameter names) from concept-to-code's
*original* extraction from beast-rs, predating this port entirely. Not
introduced by either port pass — flagged here as latent generalization debt
worth a dedicated cleanup pass, not fixed opportunistically alongside
unrelated work.
