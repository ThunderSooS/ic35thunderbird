#!/usr/bin/env python3
"""
Radicale 3.7.8 Windows compatibility launcher for IC35 Thunderbird Sync.

Radicale 3.7.8's path_supports_symlink() only catches PermissionError.
On some Windows 11 systems, os.symlink() raises OSError/WinError 1314
(ERROR_PRIVILEGE_NOT_HELD) instead. Current upstream Radicale catches
OSError for this probe and simply reports that symlinks are unsupported.

This launcher applies that narrow behavior without requiring Administrator
rights or Windows Developer Mode, then starts the normal Radicale server.
"""
import os
import sys

from radicale import pathutils

_original_path_supports_symlink = pathutils.path_supports_symlink


def _windows_safe_path_supports_symlink(path):
    try:
        return _original_path_supports_symlink(path)
    except OSError as exc:
        # Windows error 1314 = ERROR_PRIVILEGE_NOT_HELD.  For the Radicale
        # storage capability probe this means only: symlinks unavailable.
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            print(
                "[IC35-Compat] Windows erlaubt hier keine symbolischen Links; "
                "Radicale laeuft ohne Symlink-Unterstuetzung weiter.",
                file=sys.stderr,
                flush=True,
            )
            return False
        raise


pathutils.path_supports_symlink = _windows_safe_path_supports_symlink

from radicale.__main__ import run  # noqa: E402

if __name__ == "__main__":
    run()
