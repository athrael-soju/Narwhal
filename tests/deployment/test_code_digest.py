import tempfile
import unittest
from pathlib import Path

from narwhal.deployment.launch_engine.runtime import code_digest

SOURCE = '''"""Launch one engine."""
import os


def start(port):
    """Start the engine."""
    return os.execv("engine", ["engine", str(port)])
'''


class CodeDigestTests(unittest.TestCase):
    def digest(self, source: str) -> str:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch_engine.py"
            path.write_text(source)
            return code_digest(path)

    def test_comments_docstrings_and_formatting_keep_the_digest(self):
        cosmetic = (
            "import os\n\n# The engine replaces this process.\n"
            "def start( port ):\n    return os.execv('engine', ['engine', str(port)])\n"
        )
        self.assertEqual(self.digest(SOURCE), self.digest(cosmetic))

    def test_code_changes_change_the_digest(self):
        for edit in (
            SOURCE.replace("str(port)", "str(port + 1)"),
            SOURCE.replace('"""Start the engine."""', "pass"),
            SOURCE.replace("import os", "import os\nimport sys"),
        ):
            with self.subTest(edit=edit):
                self.assertNotEqual(self.digest(SOURCE), self.digest(edit))
