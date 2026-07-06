"""Default unittest discovery entrypoint for emit_stubs tests."""

from __future__ import annotations

import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

try:
    from tests.emit_stubs_test import (
        EmitStubsTests,
        KaniF64HarnessTests,
        RewriteStringViewMismatchesForCreusotTests,
        TraitEnumConceptKindTests,
        TranslateLogicToCreusotTests,
        TranslateLogicToVerusTests,
    )
except ModuleNotFoundError:
    from emit_stubs_test import (
        EmitStubsTests,
        KaniF64HarnessTests,
        RewriteStringViewMismatchesForCreusotTests,
        TraitEnumConceptKindTests,
        TranslateLogicToCreusotTests,
        TranslateLogicToVerusTests,
    )

__all__ = [
    "EmitStubsTests",
    "KaniF64HarnessTests",
    "RewriteStringViewMismatchesForCreusotTests",
    "TraitEnumConceptKindTests",
    "TranslateLogicToCreusotTests",
    "TranslateLogicToVerusTests",
]
