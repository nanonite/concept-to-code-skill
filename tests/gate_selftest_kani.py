#!/usr/bin/env python3
"""Kani lean-gate self-test for deliberately valid/broken specs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class KaniGateSelfTest(unittest.TestCase):
    def setUp(self) -> None:
        if os.environ.get("SPEC_RUN_KANI_SELFTEST") != "1":
            self.skipTest("set SPEC_RUN_KANI_SELFTEST=1 to run the verifier-backed Kani self-test")
        if shutil.which("cargo-kani") is None:
            self.skipTest("cargo-kani is required on PATH for this self-test")

    def run_fixture(self, fixture_name: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            crate = Path(tmp)
            (crate / "src").mkdir()
            (crate / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[workspace]",
                        "",
                        "[package]",
                        'name = "kani-gate-selftest"',
                        'version = "0.0.0"',
                        'edition = "2021"',
                    ]
                )
                + "\n"
            )
            shutil.copyfile(FIXTURES / fixture_name, crate / "src" / "lib.rs")
            return subprocess.run(
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

    def test_valid_fixture_passes_and_broken_fixture_fails(self) -> None:
        valid = self.run_fixture("valid_kani.rs")
        self.assertEqual(
            valid.returncode,
            0,
            msg=f"valid Kani fixture should pass\nstdout:\n{valid.stdout}\nstderr:\n{valid.stderr}",
        )

        broken = self.run_fixture("broken_kani.rs")
        self.assertNotEqual(
            broken.returncode,
            0,
            msg="broken Kani fixture unexpectedly passed; lean gate may be a false positive",
        )
        combined = broken.stdout + broken.stderr
        self.assertIn("nonexistent_precondition", combined)


if __name__ == "__main__":
    unittest.main()
