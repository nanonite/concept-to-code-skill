#!/usr/bin/env python3
"""Tests for spec_workspace.py (discovery, drift-check, impact, verify dispatch)."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_WORKSPACE = ROOT / "spec_workspace.py"
EMIT_STUBS = ROOT / "emit_stubs.py"

spec = importlib.util.spec_from_file_location("spec_workspace", SPEC_WORKSPACE)
assert spec is not None and spec.loader is not None
spec_workspace = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = spec_workspace
spec.loader.exec_module(spec_workspace)


def _spec(concept: str, *, depends_on: list[dict] | None = None) -> dict:
    body: dict = {
        "schema_version": "1.0",
        "concept": concept,
        "cluster": "workspace-test",
        "english_description": f"A {concept} concept used to exercise spec_workspace.py's discovery, drift-check, and impact analysis.",
        "queries": [
            {
                "english": "How many units are recorded?",
                "rust_sig": "fn count(&self) -> usize",
                "pure": True,
            }
        ],
        "commands": [],
        "constraints": [
            {
                "english": "The count is never negative by construction.",
                "logic": "self.count() >= 0",
                "kind": "invariant",
            }
        ],
        "adversary_table": [
            {
                "scenario": "A caller expects a negative count.",
                "violates": "the count is never negative",
                "resolution": "usize cannot represent a negative value; the type system rejects it.",
            }
        ],
        "verifier": "kani",
    }
    if depends_on is not None:
        body["depends_on"] = depends_on
    return body


class SpecWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._write_spec("alpha", "Counter", _spec("Counter"))
        self._write_spec(
            "beta",
            "Reporter",
            _spec(
                "Reporter",
                depends_on=[
                    {
                        "crate": "alpha",
                        "concept": "Counter",
                        "reason": "Reporter's summary assumes Counter's count is monotonic across a session.",
                    }
                ],
            ),
        )

    def _write_spec(self, crate: str, concept: str, body: dict) -> Path:
        module = spec_workspace.snake_case(concept)
        path = self.root / crate / "specs" / f"{module}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
        return path

    def _generate(self, crate: str, concept: str) -> None:
        module = spec_workspace.snake_case(concept)
        spec_path = self.root / crate / "specs" / f"{module}.json"
        crate_dir = self.root / crate
        result = subprocess.run(
            [
                sys.executable, str(EMIT_STUBS), str(spec_path),
                "--crate-dir", str(crate_dir),
                "--specs-search-root", str(self.root),
            ],
            check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_discover_reports_pending_before_generation(self) -> None:
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        by_key = {c.key: c for c in concepts}
        self.assertTrue(by_key["alpha::Counter"].pending)
        self.assertTrue(by_key["beta::Reporter"].pending)

    def test_discover_reports_stub_after_generation(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        counter = next(c for c in concepts if c.key == "alpha::Counter")
        self.assertFalse(counter.pending)
        self.assertFalse(counter.implemented)

    def test_discover_reports_implemented_once_unimplemented_marker_is_gone(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        counter = next(c for c in concepts if c.key == "alpha::Counter")
        counter.source.write_text(
            counter.source.read_text().replace("unimplemented!()", "self.count")
        )
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        counter = next(c for c in concepts if c.key == "alpha::Counter")
        self.assertTrue(counter.implemented)

    def test_partial_generation_is_rejected(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        counter = next(c for c in concepts if c.key == "alpha::Counter")
        counter.props.unlink()
        with self.assertRaises(spec_workspace.WorkspaceError):
            spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)

    def test_dependencies_include_depends_on_field(self) -> None:
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        reporter = next(c for c in concepts if c.key == "beta::Reporter")
        self.assertEqual(len(reporter.dependencies), 1)
        dep = reporter.dependencies[0]
        self.assertEqual(dep.key, "alpha::Counter")
        self.assertEqual(dep.link, "depends_on")

    def test_impact_finds_transitive_dependent(self) -> None:
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        out = io.StringIO()
        with redirect_stdout(out):
            spec_workspace.cmd_impact(concepts, "alpha::Counter")
        self.assertIn("beta::Reporter", out.getvalue())

    def test_impact_on_leaf_concept_reports_nothing(self) -> None:
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        out = io.StringIO()
        with redirect_stdout(out):
            spec_workspace.cmd_impact(concepts, "beta::Reporter")
        self.assertIn("no other concept depends on it", out.getvalue())

    def test_impact_on_unknown_concept_raises(self) -> None:
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        with self.assertRaises(spec_workspace.WorkspaceError):
            spec_workspace.cmd_impact(concepts, "alpha::DoesNotExist")

    def test_check_passes_for_freshly_generated_output(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        spec_workspace.cmd_check(concepts, self.root, "contracts")  # should not raise

    def test_check_fails_on_hand_edited_drift(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        counter = next(c for c in concepts if c.key == "alpha::Counter")
        counter.source.write_text(counter.source.read_text() + "\n// hand-edited\n")
        with self.assertRaises(spec_workspace.WorkspaceError) as ctx:
            spec_workspace.cmd_check(concepts, self.root, "contracts")
        self.assertIn("alpha::Counter", str(ctx.exception))

    def test_verify_full_skips_unimplemented_stub(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        out = io.StringIO()
        with redirect_stdout(out):
            spec_workspace.cmd_verify(concepts, "full", dry_run=True)
        self.assertIn("SKIP alpha::Counter: unimplemented body", out.getvalue())
        self.assertIn("PENDING beta::Reporter", out.getvalue())

    def test_verify_full_runs_once_implemented(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        counter = next(c for c in concepts if c.key == "alpha::Counter")
        counter.source.write_text(
            counter.source.read_text().replace("unimplemented!()", "self.count")
        )
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        out = io.StringIO()
        with redirect_stdout(out):
            spec_workspace.cmd_verify(concepts, "full", dry_run=True)
        self.assertIn("FULL alpha::Counter [kani]", out.getvalue())
        self.assertNotIn("SKIP alpha::Counter", out.getvalue())

    def test_verify_lean_does_not_skip_unimplemented_stub(self) -> None:
        self._generate("alpha", "Counter")
        concepts = spec_workspace.discover(self.root, spec_workspace.DEFAULT_SPECS_GLOB)
        out = io.StringIO()
        with redirect_stdout(out):
            spec_workspace.cmd_verify(concepts, "lean", dry_run=True)
        self.assertIn("LEAN alpha::Counter [kani]", out.getvalue())
        self.assertNotIn("SKIP alpha::Counter", out.getvalue())


if __name__ == "__main__":
    unittest.main()
