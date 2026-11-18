"""Explicit-file ACL restoration, including a real Windows protected-DACL move."""
import ctypes
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core import file_access


class FileAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-access-")
        self.root = Path(self.temporary.name)
        self.target = self.root / "中文 文件 & (视频).mp4"
        self.target.write_bytes(b"rendered-video-content")

    def tearDown(self):
        self.temporary.cleanup()

    def test_windows_passes_exact_file_as_one_argument_without_shell(self):
        with patch.object(file_access.sys, "platform", "win32"), \
                patch.object(file_access.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = file_access.restore_inherited_access(self.target)
        self.assertEqual(result, self.target.resolve())
        command = run.call_args.args[0]
        self.assertEqual(Path(command[0]).name.lower(), "icacls.exe")
        self.assertEqual(command[1:], [str(self.target.resolve()), "/reset", "/q"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertNotIn("/T", command)
        self.assertEqual(self.target.read_bytes(), b"rendered-video-content")

    def test_non_windows_leaves_mode_and_file_content_unchanged(self):
        original_mode = self.target.stat().st_mode
        with patch.object(file_access.sys, "platform", "linux"), \
                patch.object(file_access.subprocess, "run") as run:
            self.assertEqual(file_access.restore_inherited_access(self.target), self.target.resolve())
        run.assert_not_called()
        self.assertEqual(self.target.stat().st_mode, original_mode)
        self.assertEqual(self.target.read_bytes(), b"rendered-video-content")

    def test_directory_and_missing_file_are_rejected_without_running_utility(self):
        with patch.object(file_access.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "单个输出文件"):
                file_access.restore_inherited_access(self.root)
            with self.assertRaises(FileNotFoundError):
                file_access.restore_inherited_access(self.root / "missing.mp4")
        run.assert_not_called()

    def test_failed_reset_is_an_explicit_error(self):
        result = subprocess.CompletedProcess([], 5, "", "Access is denied")
        with patch.object(file_access.sys, "platform", "win32"), \
                patch.object(file_access.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(file_access.FileAccessError, "Access is denied"):
                file_access.restore_inherited_access(self.target)

    def test_missing_utility_and_timeout_are_explicit_errors(self):
        for failure in (FileNotFoundError("missing icacls"), subprocess.TimeoutExpired(["icacls.exe"], 30)):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(file_access.sys, "platform", "win32"), \
                    patch.object(file_access.subprocess, "run", side_effect=failure):
                with self.assertRaises(file_access.FileAccessError):
                    file_access.restore_inherited_access(self.target)

    @unittest.skipUnless(sys.platform == "win32", "Windows DACL integration")
    def test_actual_moved_file_loses_private_protected_dacl(self):
        # All changes stay inside this test's temporary directory. Copy inherited
        # ACEs before protecting the DACL so this account retains normal access.
        private = self.root / "private"
        destination = self.root / "export"
        private.mkdir()
        destination.mkdir()
        original = private / "private.mp4"
        original.write_bytes(b"video-hash-stays-identical")
        utility = str(Path(file_access.os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe")
        subprocess.run([utility, str(original), "/inheritance:d", "/q"], shell=False,
                       capture_output=True, check=True, timeout=30)
        self.assertTrue(_has_protected_dacl(original))
        moved = destination / self.target.name
        original.replace(moved)
        self.assertTrue(_has_protected_dacl(moved))
        self.assertEqual(file_access.restore_inherited_access(moved), moved.resolve())
        self.assertFalse(_has_protected_dacl(moved))
        self.assertEqual(moved.read_bytes(), b"video-hash-stays-identical")


def _has_protected_dacl(path: Path) -> bool:
    """Read only the security descriptor control bits, without PowerShell."""
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_descriptor = security.GetNamedSecurityInfoW
    get_descriptor.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_descriptor.restype = wintypes.DWORD
    get_control = security.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    get_control.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    error = get_descriptor(str(path.resolve()), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(control.value & 0x1000)  # SE_DACL_PROTECTED
    finally:
        kernel.LocalFree(descriptor)


if __name__ == "__main__":
    unittest.main()
