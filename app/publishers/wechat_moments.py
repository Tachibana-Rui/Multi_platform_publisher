from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading

from .base import ContentCallback, PublicationCancelled, PublishSnapshot, StatusCallback
from ..config import settings


class WechatMomentsPublisher:
    platform = "wechat_moments"

    def validate(self, snapshot: PublishSnapshot) -> list[dict]:
        issues: list[dict] = []
        images = [asset for asset in snapshot.assets if asset.media_type == "image"]
        videos = [asset for asset in snapshot.assets if asset.media_type == "video"]
        if not snapshot.assets:
            issues.append(self._issue("error", "media_required", "朋友圈至少需要一个图片或视频素材"))
        if images and videos:
            issues.append(self._issue("error", "mixed_media", "朋友圈单次发布不能混合图片和视频"))
        if len(images) > 9:
            issues.append(self._issue("error", "image_count", "朋友圈单次最多选择 9 张图片"))
        if len(videos) > 1:
            issues.append(self._issue("error", "video_count", "朋友圈单次只能选择 1 个视频"))
        for asset in snapshot.assets:
            if not asset.path.is_file():
                issues.append(self._issue("error", "file_missing", f"素材文件不存在：{asset.path.name}"))
        return issues

    @staticmethod
    def _issue(level: str, code: str, message: str) -> dict:
        return {"level": level, "code": code, "message": message}

    def execute(
        self,
        snapshot: PublishSnapshot,
        confirm_event: threading.Event,
        cancel_event: threading.Event,
        on_status: StatusCallback,
        on_content: ContentCallback,
    ) -> dict:
        stage_dir = settings.data_dir / "wechat_moments" / snapshot.id
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        for index, asset in enumerate(snapshot.assets, start=1):
            destination = stage_dir / f"{index:02d}_{asset.path.name}"
            shutil.copy2(asset.path, destination)

        caption = "\n\n".join(
            part for part in (snapshot.title.strip(), snapshot.body.strip()) if part
        )
        (stage_dir / "朋友圈文案.txt").write_text(caption, encoding="utf-8")
        self._copy_to_clipboard(caption)
        self._open_wechat()
        subprocess.Popen(
            ["explorer.exe", str(stage_dir)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        on_status(
            "review_pending",
            "朋友圈文案已复制，素材目录和微信已打开；请在微信中选择素材、设置可见范围并发布，然后回到中台确认",
        )
        while not confirm_event.wait(0.5):
            if cancel_event.is_set():
                raise PublicationCancelled("朋友圈发布任务已取消")
        return {
            "status": "published",
            "platform_url": None,
            "platform_item_id": None,
            "manual": True,
        }

    @staticmethod
    def _copy_to_clipboard(caption: str) -> None:
        try:
            subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-Command",
                    "$value = [Console]::In.ReadToEnd(); Set-Clipboard -Value $value",
                ],
                input=caption,
                text=True,
                encoding="utf-8",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("无法将朋友圈文案复制到剪贴板") from exc

    @staticmethod
    def _open_wechat() -> None:
        candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "Tencent" / "Weixin" / "Weixin.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Tencent" / "WeChat" / "WeChat.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Tencent" / "WeChat" / "WeChat.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Tencent" / "Weixin" / "Weixin.exe",
        ]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable:
            subprocess.Popen([str(executable)])
            return
        try:
            os.startfile("weixin://")
        except OSError as exc:
            raise RuntimeError("未找到 Windows 微信，请先安装并登录微信") from exc
