#!/usr/bin/env python3
"""Supplementary Kani-f64 harness lean-gate self-test (kani_f64_checks)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
EMIT_STUBS = ROOT / "emit_stubs.py"


class KaniF64GateSelfTest(unittest.TestCase):
    def setUp(self) -> None:
        if os.environ.get("SPEC_RUN_KANI_F64_SELFTEST") != "1":
            self.skipTest("set SPEC_RUN_KANI_F64_SELFTEST=1 to run the verifier-backed Kani f64 self-test")
        if shutil.which("cargo-kani") is None:
            self.skipTest("cargo-kani is required on PATH for this self-test")

    def test_generated_harness_passes_codegen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            crate = Path(tmp)
            (crate / "src").mkdir()
            (crate / "src" / "lib.rs").write_text("")
            (crate / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[workspace]",
                        "",
                        "[package]",
                        'name = "kani-f64-gate-selftest"',
                        'version = "0.0.0"',
                        'edition = "2021"',
                    ]
                )
                + "\n"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(EMIT_STUBS),
                    str(FIXTURES / "kani_f64_demo.json"),
                    "--crate-dir",
                    str(crate),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            kani = subprocess.run(
                [
                    "cargo",
                    "kani",
                    "--manifest-path",
                    str(crate / "Cargo.toml"),
                    "--tests",
                    "--only-codegen",
                ],
                cwd=crate,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            self.assertEqual(
                kani.returncode,
                0,
                msg=f"generated kani_f64 harness failed codegen\nstdout:\n{kani.stdout}\nstderr:\n{kani.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
