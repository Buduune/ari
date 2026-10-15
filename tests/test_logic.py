"""Runs the JavaScript logic tests (tests/logic.test.mjs) with Node, so one `pytest` covers everything."""

import shutil
import subprocess

import pytest

from conftest import ROOT


def test_page_logic_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed, skipping the page logic tests")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "logic.test.mjs")],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
