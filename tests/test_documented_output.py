# SPDX-License-Identifier: Apache-2.0

"""The `compare` output shown in the README and the overview is what the CLI prints."""

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

from ocura_oss import cli

REPO = Path(__file__).resolve().parents[1]
DOCUMENTS = ("README.md", "docs/index.md")
BLOCK = re.compile(r"```text\n(comparison: .*?)```", re.DOTALL)


def shape(text):
    """Replace the parts that differ between runs: identifiers and durations."""
    text = re.sub(r"[0-9a-f]{32}", "<id>", text)
    return re.sub(r"\d+\.\d{6}s", "<seconds>", text)


class DocumentedCompareOutputTests(unittest.TestCase):
    def invoke(self, root, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([arguments[0], "--root", str(root), *arguments[1:]])
        self.assertEqual(code, 0, stderr.getvalue())
        return stdout.getvalue()

    def field(self, text, label):
        return next(
            line.split(": ", 1)[1] for line in text.splitlines() if line.startswith(f"{label}: ")
        )

    def documented_workflow(self, root):
        """Run the commands the documents list above the output block."""
        self.invoke(root, "init", "--name", "example")
        baseline = self.invoke(
            root, "run", "--quiet", "--param", "count=1", "--", sys.executable, "-c", "print(1)"
        )
        branch = self.invoke(
            root,
            "branch",
            "--from",
            self.field(baseline, "chokepoint"),
            "--reason",
            "try count 2",
            "--param",
            "count=2",
        )
        self.invoke(
            root,
            "run",
            "--quiet",
            "--pathway",
            self.field(branch, "pathway"),
            "--",
            sys.executable,
            "-c",
            "print(2)",
        )
        return self.invoke(root, "compare")

    def test_documents_show_what_compare_prints(self):
        with tempfile.TemporaryDirectory() as temporary:
            printed = self.documented_workflow(Path(temporary) / "proj")
        for name in DOCUMENTS:
            with self.subTest(document=name):
                blocks = BLOCK.findall((REPO / name).read_text(encoding="utf-8"))
                self.assertEqual(len(blocks), 1, f"{name} should show one compare result")
                self.assertEqual(shape(blocks[0]), shape(printed))


if __name__ == "__main__":
    unittest.main()
