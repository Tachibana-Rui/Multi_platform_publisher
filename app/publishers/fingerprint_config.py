# -*- coding: utf-8 -*-
"""硬件指纹配置生成器。

给 browser.py / account_manager.py 的 context.add_init_script(...) 传入
一份"硬件指纹参数"，使 JS init script 能够根据本文件的配置动态扰动
屏幕、CPU、内存、时区、语言、Canvas/WebGL/Audio 等指纹。

用法示例（在 Python 侧）：

    cfg = FingerprintConfig.random()      # 随机选一个合理的桌面/移动配置
    context.add_init_script(
        'Object.defineProperty(window, "__fp_cfg__", { configurable: true, writable: true, value: '
        + cfg.as_json() + ' });'
    )
    context.add_init_script(open("anti_detection.js").read())

或用 build_init_script() 一步到位：

    script = build_init_script()  # 读取 anti_detection.js 并在其前面注入配置
    context.add_init_script(script)
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, asdict
from typing import List, Optional


# ----------------------------------------------------------------------
# 设备预设：覆盖典型桌面 / 中端安卓 / iPhone 的真实硬件组合
# ----------------------------------------------------------------------
# 说明：screen.width/height 是"屏幕物理分辨率"，availLeft/availTop/availWidth/
# availHeight 是"去掉任务栏后的工作区"。innerWidth/innerHeight 是浏览器窗口，
# outerWidth/outerHeight 是浏览器整体尺寸。移动端通常 screen.width < 500，
# 桌面端通常 >= 1280。
# ----------------------------------------------------------------------

DEVICE_PRESETS = {
    # ---- 桌面（Windows/macOS/Linux）----
    "desktop_1080p": {
        "profile": "desktop_1080p",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "screen_width": 1920,
        "screen_height": 1080,
        "avail_width": 1920,
        "avail_height": 1040,
        "outer_width": 1536,
        "outer_height": 824,
        "inner_width": 1519,
        "inner_height": 746,
        "screen_x": 0,
        "screen_y": 0,
        "device_pixel_ratio": 1.0,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "max_touch_points": 0,
        "ontouchstart_present": False,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-23.21.13.8792)",
    },
    "desktop_2k": {
        "profile": "desktop_2k",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "screen_width": 2560,
        "screen_height": 1440,
        "avail_width": 2560,
        "avail_height": 1400,
        "outer_width": 1920,
        "outer_height": 1040,
        "inner_width": 1903,
        "inner_height": 963,
        "screen_x": 0,
        "screen_y": 0,
        "device_pixel_ratio": 1.25,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 12,
        "device_memory": 16,
        "max_touch_points": 0,
        "ontouchstart_present": False,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.1694)",
    },
    "desktop_4k": {
        "profile": "desktop_4k",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "screen_width": 3840,
        "screen_height": 2160,
        "avail_width": 3840,
        "avail_height": 2120,
        "outer_width": 2560,
        "outer_height": 1380,
        "inner_width": 2543,
        "inner_height": 1295,
        "screen_x": 0,
        "screen_y": 0,
        "device_pixel_ratio": 1.5,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 16,
        "device_memory": 32,
        "max_touch_points": 0,
        "ontouchstart_present": False,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3623)",
    },
    "desktop_macbookpro": {
        "profile": "desktop_macbookpro",
        "platform": "MacIntel",
        "vendor": "Apple Computer, Inc.",
        "screen_width": 1440,
        "screen_height": 900,
        "avail_width": 1440,
        "avail_height": 823,
        "outer_width": 1440,
        "outer_height": 823,
        "inner_width": 1440,
        "inner_height": 755,
        "screen_x": 0,
        "screen_y": 23,
        "device_pixel_ratio": 2.0,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "max_touch_points": 0,
        "ontouchstart_present": False,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh-Hans", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "Apple Inc.",
        "webgl_renderer": "Apple M1 Pro",
    },
    # ---- 安卓中端手机 ----
    "mobile_xiaomi_13": {
        "profile": "mobile_xiaomi_13",
        "platform": "Linux aarch64",
        "vendor": "Xiaomi",
        "screen_width": 393,
        "screen_height": 873,
        "avail_width": 393,
        "avail_height": 803,
        "outer_width": 393,
        "outer_height": 873,
        "inner_width": 393,
        "inner_height": 755,
        "screen_x": 0,
        "screen_y": 0,
        "device_pixel_ratio": 2.75,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "max_touch_points": 5,
        "ontouchstart_present": True,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "Qualcomm",
        "webgl_renderer": "Adreno (TM) 740",
    },
    "mobile_huawei_p40": {
        "profile": "mobile_huawei_p40",
        "platform": "Linux aarch64",
        "vendor": "HUAWEI",
        "screen_width": 360,
        "screen_height": 780,
        "avail_width": 360,
        "avail_height": 722,
        "outer_width": 360,
        "outer_height": 780,
        "inner_width": 360,
        "inner_height": 675,
        "screen_x": 0,
        "screen_y": 0,
        "device_pixel_ratio": 3.0,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "max_touch_points": 10,
        "ontouchstart_present": True,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "ARM",
        "webgl_renderer": "Mali-G78",
    },
    # ---- iPhone ----
    "mobile_iphone_14": {
        "profile": "mobile_iphone_14",
        "platform": "iPhone",
        "vendor": "Apple Computer, Inc.",
        "screen_width": 390,
        "screen_height": 844,
        "avail_width": 390,
        "avail_height": 766,
        "outer_width": 390,
        "outer_height": 844,
        "inner_width": 390,
        "inner_height": 664,
        "screen_x": 0,
        "screen_y": 0,
        "device_pixel_ratio": 3.0,
        "color_depth": 24,
        "pixel_depth": 24,
        "hardware_concurrency": 6,
        "device_memory": 4,
        "max_touch_points": 5,
        "ontouchstart_present": True,
        "timezone": "Asia/Shanghai",
        "languages": ["zh-CN", "zh-Hans", "en-US", "en"],
        "language": "zh-CN",
        "accept_languages": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "locale_country_code": "CN",
        "webgl_vendor": "Apple Inc.",
        "webgl_renderer": "Apple GPU",
    },
}


# ----------------------------------------------------------------------
# 指纹扰动随机种子：同一页面内所有扰动共享一个 seed，
# 确保同一页面生命周期内指纹"自洽"，但不同 session 不同。
# ----------------------------------------------------------------------


def _mulberry32(seed: int):
    """轻量可重复 PRNG（纯 Python 实现）。"""
    t = seed & 0xFFFFFFFF
    while True:
        t = (t + 0x6D2B79F5) & 0xFFFFFFFF
        x = t
        x = ((x ^ (x >> 15)) * (1 | x)) & 0xFFFFFFFF
        x = (x + ((x ^ (x >> 7)) * (61 | x))) & 0xFFFFFFFF
        yield (x ^ (x >> 14)) / 0x100000000


def _new_seed() -> int:
    return random.randint(1, 0xFFFFFFFF)


# ----------------------------------------------------------------------
# 公共字体池：模拟不同设备安装的常见中文字体 + 英文字体
# ----------------------------------------------------------------------

COMMON_FONTS = [
    "Arial", "Arial Black", "Arial Narrow", "Calibri", "Cambria",
    "Cambria Math", "Comic Sans MS", "Consolas", "Courier", "Courier New",
    "Georgia", "Helvetica", "Impact", "Palatino Linotype", "Segoe UI",
    "Tahoma", "Times", "Times New Roman", "Trebuchet MS", "Verdana",
    "Microsoft YaHei", "微软雅黑", "Microsoft JhengHei", "微軟正黑體",
    "SimSun", "宋体", "NSimSun", "新宋体", "FangSong", "仿宋",
    "KaiTi", "楷体", "STXihei", "华文细黑", "STHeiti", "黑体",
    "STFangsong", "华文仿宋", "STKaiti", "华文楷体", "STSong", "华文宋体",
    "MingLiU", "細明體", "DFKai-SB", "標楷體",
    "SimHei", "黑体", "Source Han Sans CN", "Noto Sans CJK SC",
    "Hiragino Sans GB", "PingFang SC", "苹方-简",
    "Yu Gothic", "Yu Mincho", "Meiryo", "MS Gothic", "MS Mincho",
    "Malgun Gothic", "Dotum", "Batang", "Gulim",
    "Open Sans", "Roboto", "Liberation Sans", "DejaVu Sans",
    "Ubuntu", "Cantarell", "FreeSerif",
]


def _sample_fonts() -> List[str]:
    """从 COMMON_FONTS 中随机抽取 40~60 个，模拟不同设备的字体差异。"""
    n = random.randint(40, 60)  # noqa: S311
    pool = list(COMMON_FONTS)
    random.shuffle(pool)  # noqa: S311
    return pool[:n]


# ----------------------------------------------------------------------
# FingerprintConfig 数据类
# ----------------------------------------------------------------------


@dataclass
class FingerprintConfig:
    profile: str
    platform: str
    vendor: str
    screen_width: int
    screen_height: int
    avail_width: int
    avail_height: int
    outer_width: int
    outer_height: int
    inner_width: int
    inner_height: int
    screen_x: int
    screen_y: int
    device_pixel_ratio: float
    color_depth: int
    pixel_depth: int
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    ontouchstart_present: bool
    timezone: str
    languages: List[str]
    language: str
    accept_languages: str
    locale_country_code: str
    webgl_vendor: str
    webgl_renderer: str
    canvas_noise_seed: int = field(default_factory=_new_seed)
    webgl_noise_seed: int = field(default_factory=_new_seed)
    audio_noise_seed: int = field(default_factory=_new_seed)
    fonts: List[str] = field(default_factory=list)

    @classmethod
    def random(cls, profile: Optional[str] = None) -> "FingerprintConfig":
        """随机选一个预设，做轻微扰动。

        注意：扰动只改"可微"的数值（例如 avail_height ± 2px、
        inner_width ± 10px、device_memory 向上偶数值等），绝不破坏
        设备预设的一致性（例如手机 profile 不会突然拥有 32GB 内存）。
        """
        if profile and profile in DEVICE_PRESETS:
            base = dict(DEVICE_PRESETS[profile])
        else:
            key = random.choice(list(DEVICE_PRESETS.keys()))  # noqa: S311
            base = dict(DEVICE_PRESETS[key])

        rng = _mulberry32(_new_seed())
        jitter = lambda lo, hi: int(lo + next(rng) * (hi - lo + 1))  # noqa: E731

        # 小幅度扰动：avail_height（任务栏高度差异）
        base["avail_height"] = base["avail_height"] + jitter(-5, 5)
        base["avail_width"] = base["avail_width"] + jitter(-3, 3)
        # inner/outer 做 ±10 的抖动
        base["inner_width"] = base["inner_width"] + jitter(-10, 10)
        base["inner_height"] = base["inner_height"] + jitter(-10, 10)
        base["outer_width"] = base["outer_width"] + jitter(-10, 10)
        base["outer_height"] = base["outer_height"] + jitter(-10, 10)

        # CPU 核心数：在预设值 ± 2 之间扰动，至少 4
        base["hardware_concurrency"] = max(
            4, base["hardware_concurrency"] + random.choice([-2, -1, 0, 1, 2])  # noqa: S311
        )

        # device_memory 保持预设（4/8/16/32），不随意变化
        # device_pixel_ratio 保持预设（1/1.25/1.5/2/2.75/3），不随意变化

        # 字体：随机抽取
        base["fonts"] = _sample_fonts()

        return cls(**base)

    def as_dict(self) -> dict:
        return asdict(self)

    def as_json(self) -> str:
        """序列化为 JSON（ASCII-safe，避免编码问题）。"""
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))

    def build_init_script(self, anti_detection_js: str) -> str:
        """把 __fp_cfg__ 注入和反检测脚本拼成一段可直接 add_init_script 的 JS。

        参数：
            anti_detection_js: anti_detection.js 的原始内容（UTF-8 字符串）。
        """
        # JSON 中有反斜杠换行等特殊字符，但 json.dumps 默认已做转义，
        # 直接拼进 source text 即可。
        cfg_json = self.as_json()
        header = (
            'Object.defineProperty(window, "__fp_cfg__", {'
            'configurable: true, writable: true, enumerable: false, '
            'value: ' + cfg_json + ' });'
        )
        return header + "\n" + anti_detection_js