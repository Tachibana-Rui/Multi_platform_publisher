from __future__ import annotations

import os
import subprocess

from fastapi import HTTPException


POWERSHELL_FOLDER_DIALOG = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '__DESCRIPTION__'
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$result = $dialog.ShowDialog($owner)
$owner.Close()
$owner.Dispose()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
}
$dialog.Dispose()
"""


def pick_windows_folder(description: str = "选择原始素材库目录") -> str | None:
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="当前系统暂不支持原生目录选择器")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        safe_description = description.replace("'", "''")
        script = POWERSHELL_FOLDER_DIALOG.replace("__DESCRIPTION__", safe_description)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=500, detail="无法打开 Windows 文件夹选择器") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail="Windows 文件夹选择器运行失败")
    selected = result.stdout.strip()
    return selected or None
