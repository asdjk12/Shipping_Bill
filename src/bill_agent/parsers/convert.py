"""用 LibreOffice（soffice）做旧格式转换：.doc -> .docx、.xls -> .xlsx。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def soffice_path() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_with_soffice(data: bytes, src_ext: str, dst_ext: str, timeout: int = 120) -> bytes | None:
    """转换失败或没有 soffice 时返回 None。"""
    exe = soffice_path()
    if exe is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input.{src_ext}"
        src.write_bytes(data)
        try:
            subprocess.run(
                [exe, "--headless", "--norestore", f"-env:UserInstallation=file://{tmp}/profile",
                 "--convert-to", dst_ext, "--outdir", tmp, str(src)],
                check=True, capture_output=True, timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        out = Path(tmp) / f"input.{dst_ext}"
        return out.read_bytes() if out.exists() else None
