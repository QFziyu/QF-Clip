from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.launch import selected_python
from core.releases import source_files

class ClientStartupTests(unittest.TestCase):
    def test_portable_python_is_selected_without_a_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); py=root/'.runtime/python.exe';py.parent.mkdir();py.touch()
            self.assertEqual(selected_python(root,root),py)
    def test_bundled_runtime_never_enters_source_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'main.py').write_text('pass')
            p=root/'.runtime/site-packages/private.py';p.parent.mkdir(parents=True);p.write_text('pass')
            self.assertEqual([x.relative_to(root).as_posix() for x in source_files(root)],['main.py'])
