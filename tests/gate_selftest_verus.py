#!/usr/bin/env python3
"""Verus lean-gate self-test for deliberately valid/broken specs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class VerusGateSelfTest(unittest.TestCase):
    def setUp(self) -> None:
        if os.environ.get("SPEC_RUN_VERUS_SELFTEST") != "1":
            self.skipTest("set SPEC_RUN_VERUS_SELFTEST=1 to run the verifier-backed Verus self-test")
        if shutil.which("cargo-verus") is None:
            self.skipTest("cargo-verus is required on PATH for this self-test")

    def run_fixture(self, fixture_name: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            crate = Path(tmp) / "verus-fixture"
            src = crate / "src"
            src.mkdir(parents=True)
            shutil.copyfile(FIXTURES / fixture_name, src / "lib.rs")
            (crate / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[package]",
                        "name = \"verus-fixture\"",
                        "version = \"0.0.0\"",
                        "edition = \"2021\"",
                        "",
                        "[features]",
                        "default = []",
                        "verus = [\"dep:vstd\"]",
                        "",
                        "[dependencies]",
                        "vstd = { version = \"0.0.0-2026-05-24-0157\", optional = true }",
                        "",
                        "[package.metadata.verus]",
                        "verify = true",
                        "",
                    ]
                )
            )
            return subprocess.run(
                [
                    "cargo",
                    "verus",
                    "focus",
                    "--manifest-path",
                    str(crate / "Cargo.toml"),
                    "--features",
                    "verus",
                    "--",
                    "--no-verify",
                ],
                cwd=crate,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )

    def test_valid_fixture_passes_and_broken_fixture_fails(self) -> None:
        valid = self.run_fixture("valid_verus.rs")
        self.assertEqual(
            valid.returncode,
            0,
            msg=f"valid Verus fixture should pass\nstdout:\n{valid.stdout}\nstderr:\n{valid.stderr}",
        )

        broken = self.run_fixture("broken_verus.rs")
        self.assertNotEqual(
            broken.returncode,
            0,
            msg="broken Verus fixture unexpectedly passed; lean gate may be a false positive",
        )
        combined = broken.stdout + broken.stderr
        self.assertIn("true", combined)


if __name__ == "__main__":
    unittest.main()
