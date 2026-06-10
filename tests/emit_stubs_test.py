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

        self.assertIn(
            "#[cfg_attr(creusot, contracts::creusot::ensures(true /* TODO(concept-to-code): generated query method needs Pearlite model helper */))]",
            module,
        )

    def test_creusot_static_result_constructor_rewrites_self_to_result(self) -> None:
        concept = self.base_spec("creusot")
        concept["commands"] = [{"english": "Build.", "rust_sig": "fn new() -> Result<Self, BuildError>"}]
        methods = emit_stubs.validate_spec(concept)
        module = emit_stubs.emit_module(concept, methods)

        self.assertIn(
            "#[cfg_attr(creusot, contracts::creusot::ensures(true /* TODO(concept-to-code): generated query method needs Pearlite model helper */))]",
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
        self.assertNotIn("use contracts::creusot", module)
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

