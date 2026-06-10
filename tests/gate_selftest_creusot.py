#!/usr/bin/env python3
"""Creusot lean-gate self-test for deliberately valid/broken specs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CONTRACTS_CRATE = FIXTURES / "contracts-crate"


class CreusotGateSelfTest(unittest.TestCase):
    def setUp(self) -> None:
        if os.environ.get("SPEC_RUN_CREUSOT_SELFTEST") != "1":
            self.skipTest("set SPEC_RUN_CREUSOT_SELFTEST=1 to run the verifier-backed Creusot self-test")
        if shutil.which("cargo-creusot") is None:
            self.skipTest("cargo-creusot is required on PATH for this self-test")

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
                        'name = "creusot-gate-selftest"',
                        'version = "0.0.0"',
                        'edition = "2021"',
                        "",
                        "[features]",
                        'creusot = ["contracts/creusot", "creusot-std/creusot", "creusot-std/nightly"]',
                        "",
                        "[dependencies]",
                        f'contracts = {{ path = "{CONTRACTS_CRATE}" }}',
                        'creusot-std = { git = "https://github.com/creusot-rs/creusot", rev = "9cf662ce6dfe16810b871dbde4c72a3f37567ae0", package = "creusot-std", optional = true }',
                        "",
                        "[lints.rust]",
                        'unexpected_cfgs = { level = "warn", check-cfg = ["cfg(creusot)"] }',
                    ]
                )
                + "\n"
            )
            shutil.copyfile(FIXTURES / fixture_name, crate / "src" / "lib.rs")
            return subprocess.run(
                [
                    "cargo",
                    "creusot",
                    "--no-check-version",
                    "--only",
                    "coma",
                    "--check",
                    "--",
                    "--features",
                    "creusot",
                ],
                cwd=crate,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )

    def test_valid_fixture_passes_and_broken_fixture_fails(self) -> None:
        valid = self.run_fixture("valid_creusot.rs")
        self.assertEqual(
            valid.returncode,
            0,
            msg=f"valid Creusot fixture should pass\nstdout:\n{valid.stdout}\nstderr:\n{valid.stderr}",
        )

        broken = self.run_fixture("broken_creusot.rs")
        self.assertNotEqual(
            broken.returncode,
            0,
            msg="broken Creusot fixture unexpectedly passed; lean gate may be a false positive",
        )
        combined = broken.stdout + broken.stderr
        self.assertIn("plain_runtime_predicate", combined)


if __name__ == "__main__":
    unittest.main()
