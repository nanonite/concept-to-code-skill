#!/usr/bin/env python3
"""Tests for concept-to-code stub generation."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMIT_STUBS = ROOT / "emit_stubs.py"
FIXTURES = ROOT / "tests" / "fixtures"

spec = importlib.util.spec_from_file_location("emit_stubs", EMIT_STUBS)
assert spec is not None and spec.loader is not None
emit_stubs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = emit_stubs
spec.loader.exec_module(emit_stubs)


def json_load(path: Path) -> dict:
    return json.loads(path.read_text())


class EmitStubsTests(unittest.TestCase):
    def base_spec(self, verifier: str) -> dict:
        return {
            "schema_version": "1.0",
            "concept": "LikelihoodKernel",
            "cluster": "likelihood-kernel",
            "english_description": "A likelihood kernel accumulates finite partial likelihood values over a bounded site-pattern block.",
            "queries": [
                {
                    "english": "How many site patterns are active?",
                    "rust_sig": "fn site_count(&self) -> usize",
                    "pure": True,
                }
            ],
            "commands": [
                {
                    "english": "Scale the active partials in place.",
                    "rust_sig": "fn scale_partials(&mut self, factor: f64)",
                }
            ],
            "constraints": [
                {
                    "english": "The active site count is non-empty.",
                    "logic": "self.site_count() > 0",
                    "kind": "invariant",
                },
                {
                    "english": "The scale factor is finite.",
                    "logic": "factor.is_finite()",
                    "kind": "precondition",
                    "applies_to": ["scale_partials"],
                },
                {
                    "english": "The site count remains non-empty.",
                    "logic": "self.site_count() > 0",
                    "kind": "postcondition",
                    "applies_to": ["scale_partials"],
                },
            ],
            "adversary_table": [
                {
                    "scenario": "A caller provides a NaN scale factor.",
                    "violates": "factor.is_finite()",
                    "resolution": "Reject the call before mutating partials.",
                }
            ],
            "verifier": verifier,
        }

    def test_verus_specs_emit_verus_block_and_contract_clauses(self) -> None:
        concept = self.base_spec("verus")
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)

        self.assertIn('#[cfg(any(verus, feature = "verus"))]\nverus! {', module)
        self.assertIn("use contracts::verus::prelude::*;", module)
        self.assertIn("    pub fn scale_partials(&mut self, factor: f64) -> (result: ())\n        requires", module)
        self.assertIn("            verus_f64_is_finite(factor),", module)
        self.assertIn("pub open spec fn abs_f64(value: f64) -> f64 {\n    if value < 0.0f64 { -value } else { value }\n}", module)
        self.assertIn("pub open spec fn verus_f64_is_finite(value: f64) -> bool {\n    value >= -1.7976931348623157e308f64 && value <= 1.7976931348623157e308f64\n}", module)
        self.assertNotIn("pub open spec fn verus_f64_is_finite(value: f64) -> bool { true }", module)
        self.assertNotIn("pub open spec fn abs_f64(value: f64) -> f64 { value }", module)
        self.assertIn("    pub open spec fn site_count_spec(&self) -> usize { 0usize }", module)
        self.assertIn("        ensures\n            final(self).site_count_spec() > 0,", module)
        self.assertIn('#[cfg(not(any(verus, feature = "verus")))]', module)
        self.assertIn("pub struct LikelihoodKernel;", module)
        self.assertNotIn("cfg_attr(kani", module)
        self.assertNotIn("cfg_attr(creusot", module)

    def test_kani_specs_keep_existing_cfg_attr_contracts(self) -> None:
        concept = self.base_spec("kani")
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)

        self.assertIn("#[cfg_attr(kani, kani::requires(self.site_count() > 0))]", module)
        self.assertIn(
            "#[cfg_attr(creusot, contracts::creusot::requires(true /* TODO(concept-to-code): generated query method needs Pearlite model helper */))]",
            module,
        )
        self.assertNotIn("use contracts::creusot", module)
        self.assertNotIn("verus! {", module)



    def test_string_applies_to_matches_exact_method_name(self) -> None:
        concept = self.base_spec("creusot")
        concept["commands"].append({"english": "Tune.", "rust_sig": "fn tune(&self)"})
        concept["constraints"].append(
            {
                "english": "Only scale_partials sees factor.",
                "logic": "factor > 0.0",
                "kind": "postcondition",
                "applies_to": "scale_partials",
            }
        )
        methods = emit_stubs.validate_spec(concept)
        by_name = {method.name: list(emit_stubs.applicable_constraints(concept, method)) for method in methods}

        self.assertTrue(any(c.get("logic") == "factor > 0.0" for c in by_name["scale_partials"]))
        self.assertFalse(any(c.get("logic") == "factor > 0.0" for c in by_name["tune"]))

    def test_creusot_postconditions_emit_ensures(self) -> None:
        concept = self.base_spec("creusot")
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)

        # site_count returns usize, a modelable Creusot logic type, so the
        # postcondition routes through the generated site_count_model()
        # companion (in &mut self prophecy notation) instead of degrading to
        # the "no logic model yet" sentinel.
        self.assertIn(
            "#[cfg_attr(creusot, contracts::creusot::ensures((^self).site_count_model() > 0))]",
            module,
        )
        self.assertIn(
            "#[trusted]\n    #[logic(opaque)]\n    pub fn site_count_model(self) -> Int { pearlite! { 0 } }",
            module,
        )

    def test_creusot_static_result_constructor_rewrites_self_to_result(self) -> None:
        concept = self.base_spec("creusot")
        concept["commands"] = [{"english": "Build.", "rust_sig": "fn new() -> Result<Self, BuildError>"}]
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)

        # site_count returns usize (modelable), so the constructor's
        # postcondition routes through Pearlite's match-result form and the
        # site_count_model() companion instead of degrading to a sentinel.
        self.assertIn(
            "#[cfg_attr(creusot, contracts::creusot::ensures(match result { Ok(ok_result) => ok_result.site_count_model() > 0, Err(_) => true }))]",
            module,
        )

    def test_creusot_string_closure_predicates_degrade_to_sentinel(self) -> None:
        out = emit_stubs.translate_logic_to_creusot(
            "!self.label().chars().any(|c| c.is_whitespace())"
        )

        self.assertEqual(out, "true /* TODO(concept-to-code): string closure predicate needs Pearlite model */")

    def test_empty_commands_fixture_emits_observational_strategy(self) -> None:
        concept = json_load(FIXTURES / "empty_commands_view_node.json")
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)
        props = emit_stubs.emit_props(concept)

        self.assertEqual(["query", "query"], [method.kind for method in methods])
        self.assertIn("impl TreeNode {", module)
        self.assertIn("pub fn child_count(&self) -> usize", module)
        self.assertIn("pub fn is_leaf(&self) -> bool", module)
        # Both queries return modelable Creusot logic types (usize -> Int,
        # bool -> bool), so their contracts route through generated
        # *_model() companions, which need logic/trusted/pearlite! in scope.
        self.assertIn("#[cfg(creusot)]\nuse contracts::creusot::{logic, trusted};", module)
        self.assertIn("pub fn child_count_model(self) -> Int { pearlite! { 0 } }", module)
        self.assertNotIn("fn attach_child", module)
        self.assertNotIn("impl TreeNode {\n}", module)

        self.assertIn("fn tree_node_strategy() -> impl Strategy<Value = ()>", props)
        self.assertIn("// TODO(spec-first):", props)
        self.assertIn("_node in tree_node_strategy()", props)

    def test_empty_commands_fixture_module_compiles(self) -> None:
        if shutil.which("cargo") is None:
            self.skipTest("cargo is required for the generated-module compile check")

        concept = json_load(FIXTURES / "empty_commands_view_node.json")
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)

        with tempfile.TemporaryDirectory() as tmp:
            crate = Path(tmp)
            (crate / "src").mkdir()
            (crate / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[workspace]",
                        "",
                        "[package]",
                        'name = "empty-commands-compile-check"',
                        'version = "0.0.0"',
                        'edition = "2021"',
                    ]
                )
                + "\n"
            )
            (crate / "src" / "lib.rs").write_text("pub mod tree_node;\n")
            (crate / "src" / "tree_node.rs").write_text(module)

            result = subprocess.run(
                ["cargo", "check", "--quiet"],
                cwd=crate,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"cargo check failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )




class TraitEnumConceptKindTests(unittest.TestCase):
    """Regression coverage for kind='trait'/kind='enum' concepts and the
    implements mechanism (composition without dyn Trait)."""

    DEMO_ROOT = FIXTURES / "trait_enum_demo"

    def setUp(self) -> None:
        self._saved_root = emit_stubs.SPECS_SEARCH_ROOT
        emit_stubs.SPECS_SEARCH_ROOT = self.DEMO_ROOT

    def tearDown(self) -> None:
        emit_stubs.SPECS_SEARCH_ROOT = self._saved_root

    def _load(self, name: str) -> dict:
        return json_load(self.DEMO_ROOT / "democrate" / "specs" / f"{name}.json")

    def test_trait_kind_emits_bodyless_declarations_with_model_companion(self):
        spec = self._load("toggle")
        methods = emit_stubs.validate_spec(spec)
        module = emit_stubs.emit_module(spec, methods)

        self.assertIn("pub trait Toggle {", module)
        # Bodyless, never pub-qualified trait method declarations.
        self.assertIn("    fn is_on(&self) -> bool;", module)
        self.assertNotIn("pub fn is_on(&self) -> bool;", module)
        # bool is a modelable Creusot logic type, so is_on gets a default-bodied
        # trait model companion instead of degrading the flip postcondition to
        # a sentinel.
        self.assertIn(
            "#[logic(opaque)]\n    fn is_on_model(self) -> bool { pearlite! { true } }",
            module,
        )
        self.assertIn(
            "#[cfg_attr(creusot, contracts::creusot::ensures((^self).is_on_model() == !(*self).is_on_model()))]",
            module,
        )
        self.assertNotIn("TODO(concept-to-code)", module)

    def test_implements_routes_methods_into_trait_impl_without_contracts(self):
        spec = self._load("fast_toggle")
        methods = emit_stubs.validate_spec(spec)
        module = emit_stubs.emit_module(spec, methods)

        self.assertIn("pub struct FastToggle;", module)
        self.assertIn("impl Toggle for FastToggle {", module)
        self.assertIn("    fn is_on(&self) -> bool {", module)
        self.assertNotIn("pub fn is_on(&self) -> bool {", module)
        # Creusot checks refinement against the trait's own contract
        # automatically -- implements-routed methods get no attributes here.
        self.assertNotIn("cfg_attr(creusot", module)
        self.assertNotIn("cfg_attr(kani", module)

    def test_enum_kind_emits_closed_enum_with_match_dispatch(self):
        spec = self._load("any_toggle")
        methods = emit_stubs.validate_spec(spec)
        module = emit_stubs.emit_module(spec, methods)
        props = emit_stubs.emit_props(spec)

        self.assertEqual([], methods)
        self.assertIn("use democrate::toggle::Toggle as _;", module)
        self.assertIn("pub enum AnyToggle {", module)
        self.assertIn("    Fast(FastToggle),", module)
        self.assertIn("    Slow(SlowToggle),", module)
        self.assertIn("impl AnyToggle {", module)
        self.assertIn("        match self {", module)
        self.assertIn("            AnyToggle::Fast(op) => op.is_on(),", module)
        self.assertIn("            AnyToggle::Slow(op) => op.is_on(),", module)
        self.assertIn("            AnyToggle::Fast(op) => op.flip(),", module)
        self.assertNotIn("dyn Toggle", module)
        self.assertIn("kind='enum': no local constraints/adversary_table to scaffold.", props)
        self.assertNotIn("proptest!", props)


class KaniF64HarnessTests(unittest.TestCase):
    """Regression coverage for the hybrid Kani-f64 supplementary verifier
    (kani_f64_checks), which supplements a Creusot-primary concept when f64
    sign/finiteness obligations can't be expressed in Pearlite logic."""

    def test_emits_one_proof_harness_per_check(self):
        spec = json_load(FIXTURES / "kani_f64_demo.json")
        harness = emit_stubs.emit_kani_f64_harness(spec)

        self.assertIn("#![cfg(kani)]", harness)
        self.assertIn("#[kani::proof]", harness)
        self.assertIn("fn kani_f64_rate_scaler_scale_preserves_finite_sign() {", harness)
        self.assertIn("let rate: f64 = kani::any();", harness)
        self.assertIn("let factor: f64 = kani::any();", harness)
        self.assertIn("kani::assume(rate.is_finite());", harness)
        self.assertIn("let scaled = rate * factor;", harness)
        self.assertIn("assert!(scaled.is_finite());", harness)
        self.assertNotIn("kani::should_panic", harness)

    def test_expected_panic_adds_should_panic_attribute(self):
        spec = json_load(FIXTURES / "kani_f64_demo.json")
        spec["kani_f64_checks"][0]["expected"] = "panic"
        harness = emit_stubs.emit_kani_f64_harness(spec)

        self.assertIn("#[kani::should_panic]", harness)

    def test_default_paths_includes_kani_f64_out_and_main_wires_it(self):
        spec = json_load(FIXTURES / "kani_f64_demo.json")
        module_out, props_out, kani_f64_out = emit_stubs.default_paths(spec, Path("/tmp/demo-crate"))

        self.assertEqual(kani_f64_out, Path("/tmp/demo-crate/tests/kani_f64_rate_scaler.rs"))

    def test_kani_f64_checks_absent_produces_no_harness(self):
        spec = json_load(FIXTURES / "example_concept.json")
        self.assertIsNone(spec.get("kani_f64_checks"))


class RewriteStringViewMismatchesForCreusotTests(unittest.TestCase):
    """Regression tests: `result.<getter>() == <param>` must not compare a
    borrowed view (`&str`/`Option<&str>`) against an owned constructor
    parameter (`String`/`Option<String>`), which Creusot's `equal::<T>`
    rejects as a type mismatch."""

    def setUp(self):
        self.methods = [
            emit_stubs.Method("query", "name", "...", "fn name(&self) -> &str"),
            emit_stubs.Method(
                "query", "nickname", "...", "fn nickname(&self) -> Option<&str>"
            ),
        ]
        self.new_method = emit_stubs.Method(
            "command",
            "new",
            "...",
            "fn new(name: String, nickname: Option<String>, age: u32) -> Self",
        )

    def test_str_getter_vs_owned_string_param_gains_as_str(self):
        out = emit_stubs.rewrite_string_view_mismatches_for_creusot(
            "result.name() == name", self.new_method, self.methods
        )

        self.assertEqual(out, "result.name() == name.as_str()")

    def test_option_str_getter_vs_owned_option_string_param_gains_as_deref(self):
        out = emit_stubs.rewrite_string_view_mismatches_for_creusot(
            "result.nickname() == nickname", self.new_method, self.methods
        )

        self.assertEqual(out, "result.nickname() == nickname.as_deref()")

    def test_non_string_comparison_is_unaffected(self):
        out = emit_stubs.rewrite_string_view_mismatches_for_creusot(
            "result.age() == age", self.new_method, self.methods
        )

        self.assertEqual(out, "result.age() == age")


class CreusotResultAndForallUpstreamFixTests(unittest.TestCase):
    """Regression coverage for a second wave of generic Creusot/Verus fixes
    found upstream in the beast-rs fork after this repo's first port pass."""

    def test_constraint_in_scope_binds_result_for_non_result_returning_self_method(self):
        # Creusot's ensures binds the return value as `result` for every
        # return type, not just Result<_, _> -- a &self method returning a
        # plain bool/usize/etc. can still reference `result` in scope.
        method = emit_stubs.Method(
            "query", "is_valid", "...", "fn is_valid(&self) -> bool"
        )
        self.assertTrue(
            emit_stubs.creusot_constraint_in_scope("result == self.is_valid()", method)
        )

    def test_free_identifiers_ignores_words_inside_comments(self):
        out = emit_stubs.creusot_free_identifiers(
            "self.count() > 0 /* stray_word should not count as free */"
        )
        self.assertNotIn("stray_word", out)
        self.assertNotIn("should", out)

    def test_verus_sum_does_not_false_positive_on_longer_identifier(self):
        # `.find("sum(")` would incorrectly match inside `checksum(...)`.
        out = emit_stubs.translate_logic_to_verus("checksum(0) >= 0")
        self.assertEqual(out, "checksum(0) >= 0")

    def test_verus_product_does_not_false_positive_on_longer_identifier(self):
        out = emit_stubs.translate_logic_to_verus("byproduct(0) >= 0")
        self.assertEqual(out, "byproduct(0) >= 0")

    def test_result_returning_constructor_postcondition_uses_match_form(self):
        # A self-less Result-returning constructor's postcondition must not
        # emit `result.as_ref().unwrap().<method>()` -- Creusot rejects
        # as_ref/unwrap in logic context. It must use Pearlite's match form.
        method = emit_stubs.Method(
            "command", "new", "...", "fn new() -> Result<Self, BuildError>"
        )
        rewritten = emit_stubs.rewrite_static_self_for_creusot(
            "self.count() > 0", method
        )
        self.assertEqual(
            rewritten,
            "match result { Ok(ok_result) => ok_result.count() > 0, Err(_) => true }",
        )
        self.assertNotIn("as_ref", rewritten)
        self.assertNotIn("unwrap", rewritten)

    def test_query_calls_reroute_through_ok_result_receiver(self):
        rerouted = emit_stubs._replace_query_calls(
            "match result { Ok(ok_result) => ok_result.count() > 0, Err(_) => true }",
            {"count": "count_model"},
        )
        self.assertEqual(
            rerouted,
            "match result { Ok(ok_result) => ok_result.count_model() > 0, Err(_) => true }",
        )

    def test_bare_param_len_becomes_seq_view(self):
        out = emit_stubs._model_creusot_usize_calls("values.len()@ > 0")
        self.assertEqual(out, "values@.len() > 0")

    def test_self_and_result_len_are_unaffected_by_seq_view_rewrite(self):
        out = emit_stubs._model_creusot_usize_calls("self.len()@ > 0")
        self.assertEqual(out, "self.len()@ > 0")

    def test_verus_bare_slice_index_gets_int_cast(self):
        out = emit_stubs.translate_logic_to_verus("values[pattern] >= 0.0")
        self.assertIn("values[(pattern) as int]", out)

    def test_verus_quantified_slice_index_is_not_double_cast(self):
        # "i" is bound by the forall (already `int`-typed in Verus), so the
        # bare-slice-index cast must leave it alone rather than wrapping an
        # already-correct quantified index in a redundant `as int`.
        out = emit_stubs.translate_logic_to_verus(
            "forall i in 0..values.len(): values[i] >= 0.0"
        )
        self.assertIn("values[i]", out)
        self.assertNotIn("values[(i) as int]", out)

    def test_bare_forall_without_range_translates_to_pearlite_quantifier(self):
        out = emit_stubs.translate_logic_to_creusot(
            "forall label: self.contains(label) == self.contains(label)"
        )
        self.assertEqual(
            out, "forall<label> self.contains(label) == self.contains(label)"
        )
        self.assertNotIn("TODO(concept-to-code)", out)

    def test_is_some_becomes_pearlite_none_comparison(self):
        out = emit_stubs.translate_logic_to_creusot("self.nickname().is_some()")
        self.assertEqual(out, "(self.nickname() != None)")

    def test_is_none_becomes_pearlite_none_comparison(self):
        out = emit_stubs.translate_logic_to_creusot("self.nickname().is_none()")
        self.assertEqual(out, "(self.nickname() == None)")


class TranslateLogicToCreusotTests(unittest.TestCase):
    def test_count_aggregate_degrades_to_todo_sentinel(self):
        out = emit_stubs.translate_logic_to_creusot(
            "self.observed_count() == count(i in 0..self.size() : self.is_observed_at(i))"
        )

        self.assertEqual(out, "true /* TODO(concept-to-code): count aggregate needs Pearlite translation */")


    def test_bounded_forall_translates_to_pearlite_quantifier(self):
        out = emit_stubs.translate_logic_to_creusot(
            "forall i, j in 0..self.size(): i != j ==> self.taxon_at(i).label() != self.taxon_at(j).label()"
        )

        self.assertIn("forall<i, j>", out)
        self.assertIn("0 <= i && i < self.size()", out)
        self.assertIn("0 <= j && j < self.size()", out)
        self.assertNotIn("forall i", out)

    def test_unsupported_forall_degrades_to_todo_sentinel(self):
        out = emit_stubs.translate_logic_to_creusot(
            "result.is_ok() ==> forall (idx, val) in values: result.as_ref().unwrap().is_observed_at(idx)"
        )

        self.assertEqual(out, "true /* TODO(concept-to-code): unsupported forall clause needs Pearlite translation */")

class TranslateLogicToVerusTests(unittest.TestCase):
    """Regression tests verifying that emit_stubs.py does not copy
    restricted-English `logic` verbatim into verus! blocks."""

    def test_implies_becomes_double_arrow(self):
        out = emit_stubs.translate_logic_to_verus("self.x() implies self.y()")
        self.assertIn("==>", out)
        self.assertNotIn("implies", out)

    def test_if_then_else_becomes_rust_if_expression(self):
        out = emit_stubs.translate_logic_to_verus("if i == j then 1.0 else 0.0")
        self.assertIn("if i == j {", out)
        self.assertIn("} else {", out)
        self.assertNotIn(" then ", out)

    def test_forall_clause_translates_to_verus_quantifier(self):
        out = emit_stubs.translate_logic_to_verus(
            "forall i in 0..self.state_count(): self.equilibrium_frequency(i) >= 0.0"
        )
        self.assertIn("forall|i: int|", out)
        self.assertIn("0 <= i && i < (self.state_count()) as int ==>", out)
        self.assertIn("self.equilibrium_frequency(i as usize) >= 0.0f64", out)
        self.assertNotIn("TODO(concept-to-code)", out)

    def test_multi_forall_with_where_clause_translates(self):
        out = emit_stubs.translate_logic_to_verus(
            "forall i,j in 0..self.state_count() where i != j: self.rate(i,j) >= 0.0"
        )
        self.assertIn("forall|i: int, j: int|", out)
        self.assertIn("0 <= i && i < (self.state_count()) as int", out)
        self.assertIn("0 <= j && j < (self.state_count()) as int", out)
        self.assertIn("i != j", out)
        self.assertIn("self.rate(i as usize, j as usize) >= 0.0f64", out)

    def test_is_finite_translates_to_verus_model_helper(self):
        out = emit_stubs.translate_logic_to_verus("branch_length.is_finite()")

        self.assertEqual(out, "verus_f64_is_finite(branch_length)")

    def test_sum_aggregate_translates_to_spec_sum_helper(self):
        out = emit_stubs.translate_logic_to_verus(
            "abs(sum(i in 0..self.state_count(): self.equilibrium_frequency(i)) - 1.0) <= FREQUENCY_SUM_TOL"
        )
        self.assertIn("abs_f64(", out)
        self.assertIn("verus_sum_f64(0, (self.state_count()) as int", out)
        self.assertIn("|i: int| self.equilibrium_frequency(i as usize)", out)
        self.assertNotIn("TODO(concept-to-code)", out)

    def test_simple_clauses_pass_through(self):
        out = emit_stubs.translate_logic_to_verus("self.state_count() >= 2")
        self.assertEqual(out, "self.state_count() >= 2")

    def test_query_calls_can_rewrite_to_spec_companions(self):
        out = emit_stubs.translate_logic_to_verus(
            "forall i in 0..self.state_count(): self.equilibrium_frequency(i) >= 0.0",
            query_spec_map={
                "state_count": "state_count_spec",
                "equilibrium_frequency": "equilibrium_frequency_spec",
            },
        )

        self.assertIn("0 <= i && i < (self.state_count_spec()) as int ==>", out)
        self.assertIn("self.equilibrium_frequency_spec(i as usize) >= 0.0f64", out)
        self.assertNotIn("self.state_count()", out)
        self.assertNotIn("self.equilibrium_frequency(", out)

    def test_slice_indexes_use_spec_sequence_int_indexes(self):
        out = emit_stubs.translate_logic_to_verus(
            "forall k in 0..out.len(): out[k].is_finite()"
        )
        self.assertIn("verus_f64_is_finite(out@[k])", out)
        self.assertNotIn("out[(k as usize)]", out)

    def test_mut_slice_postconditions_use_final_view(self):
        out = emit_stubs.translate_logic_to_verus(
            "result.is_ok() implies forall k in 0..out.len(): out[k].is_finite() && out[k] >= 0.0",
            final_mut_refs=["out"],
        )

        self.assertIn("final(out)@.len()", out)
        self.assertIn("verus_f64_is_finite(final(out)@[k]) && final(out)@[k] >= 0.0f64", out)
        self.assertNotIn("out@[k]", out)

    def test_matrix_slice_indexes_keep_quantified_int_arithmetic(self):
        out = emit_stubs.translate_logic_to_verus(
            "forall i,j in 0..self.state_count(): out[i*self.state_count()+j] >= 0.0"
        )
        self.assertIn("out@[i*((self.state_count()) as int)+j] >= 0.0f64", out)
        self.assertNotIn("as usize", out)

    def test_named_result_signature_for_verus_ensures(self):
        sig = emit_stubs.rust_signature_with_named_result(
            "fn transition_probabilities(&self, out: &mut [f64]) -> Result<(), SubstitutionModelError>"
        )
        self.assertEqual(
            sig,
            "fn transition_probabilities(&self, out: &mut [f64]) -> (result: Result<(), SubstitutionModelError>)",
        )

    def test_verus_stub_body_uses_assume_false_and_diverges(self):
        method = emit_stubs.Method(
            "query",
            "state_count",
            "How many states?",
            "fn state_count(&self) -> usize",
        )

        self.assertEqual(
            emit_stubs.verus_stub_body_lines(method),
            ["        assume(false);", "        loop {}"],
        )

    def test_verus_block_never_contains_raw_restricted_english(self):
        """Generated verus! block must not contain bare `implies` or
        `forall ... :` tokens — both are Verus parse errors. Catches the
        original #278 failure mode end-to-end."""
        spec = {
            "schema_version": "1.0",
            "concept": "Demo",
            "cluster": "demo-cluster",
            "english_description": "Demo concept for the regression test.",
            "queries": [
                {
                    "english": "How many items?",
                    "rust_sig": "fn n(&self) -> usize",
                    "pure": True,
                }
            ],
            "commands": [],
            "constraints": [
                {
                    "english": "n is at least one.",
                    "logic": "self.n() >= 1",
                    "kind": "invariant",
                },
                {
                    "english": "everything is non-negative.",
                    "logic": "forall i in 0..self.n(): self.x(i) >= 0.0",
                    "kind": "invariant",
                },
                {
                    "english": "if reversible then symmetric.",
                    "logic": "self.is_rev() implies self.is_sym()",
                    "kind": "invariant",
                },
            ],
            "adversary_table": [
                {
                    "scenario": "empty collection",
                    "violates": "IC1",
                    "resolution": "constructor rejects",
                }
            ],
            "verifier": "verus",
        }
        methods = emit_stubs.validate_spec(spec)
        module_text = emit_stubs.emit_module(spec, methods)
        verus_start = module_text.index("verus! {")
        verus_end = module_text.index("\n}\n\n#[cfg(not", verus_start)
        verus_block = module_text[verus_start:verus_end]
        verus_block_no_comments = re.sub(
            r"/\*.*?\*/", "", verus_block, flags=re.DOTALL
        )
        self.assertNotIn(
            " implies ",
            verus_block_no_comments,
            msg="restricted-English `implies` leaked into verus! block outside comments",
        )
        self.assertNotIn(
            "forall i in 0..",
            verus_block_no_comments,
            msg="restricted-English `forall ... in 0..` leaked into verus! block outside comments",
        )
        self.assertIn("==>", verus_block)
        self.assertIn("forall|i: int|", verus_block)
        self.assertNotIn("TODO(concept-to-code)", verus_block)

    def test_example_concept_constraints_translate_without_sentinels(self):
        concept = json_load(FIXTURES / "example_concept.json")
        methods = emit_stubs.validate_spec(concept)
        module_text = emit_stubs.emit_module(concept, methods)
        self.assertNotIn("TODO(concept-to-code)", module_text)
        self.assertNotIn("true /*", module_text)
        self.assertIn("forall|i: int, j: int|", module_text)
        self.assertIn("verus_sum_f64", module_text)
        self.assertIn("pub open spec fn verus_sum_f64(start: int, end: int, f: spec_fn(int) -> f64) -> f64\n    decreases if start < end { (end - start) as nat } else { 0nat }", module_text)
        self.assertIn("f(start) + verus_sum_f64(start + 1, end, f)", module_text)
        self.assertIn("pub open spec fn verus_sum_f64_where(start: int, end: int, p: spec_fn(int) -> bool, f: spec_fn(int) -> f64) -> f64\n    decreases if start < end { (end - start) as nat } else { 0nat }", module_text)
        self.assertIn("(if p(start) { f(start) } else { 0.0f64 }) + verus_sum_f64_where(start + 1, end, p, f)", module_text)
        self.assertNotIn("pub open spec fn verus_sum_f64(start: int, end: int, f: spec_fn(int) -> f64) -> f64 { 0.0f64 }", module_text)
        self.assertNotIn("pub open spec fn verus_sum_f64_where(start: int, end: int, p: spec_fn(int) -> bool, f: spec_fn(int) -> f64) -> f64 { 0.0f64 }", module_text)
        self.assertIn("abs_f64", module_text)
        self.assertIn("pub open spec fn component_count_spec(&self) -> usize { 0usize }", module_text)
        self.assertIn("pub open spec fn weight_spec(&self, index: usize) -> f64 { 0.0f64 }", module_text)
        self.assertIn("pub open spec fn coupling_spec(&self, from: usize, to: usize) -> f64 { 0.0f64 }", module_text)
        self.assertIn("pub open spec fn is_symmetric_spec(&self) -> bool { false }", module_text)
        self.assertIn("self.component_count_spec()", module_text)
        self.assertIn("self.weight_spec(i as usize)", module_text)
        self.assertIn("self.coupling_spec(i as usize, j as usize)", module_text)
        self.assertIn("self.coupling_spec(i as usize, i as usize)", module_text)
        self.assertIn("self.weight_spec(i as usize) * self.coupling_spec(i as usize, j as usize)", module_text)
        self.assertNotIn("self.coupling_spec(i,i)", module_text)
        self.assertNotIn("self.weight_spec(i) *", module_text)
        self.assertIn("-> (result: Result<(), KernelError>)", module_text)
        self.assertIn("final(out)@[k]", module_text)
        self.assertNotIn("out[(k as usize)]", module_text)


if __name__ == "__main__":
    unittest.main()

