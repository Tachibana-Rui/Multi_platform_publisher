from .browser import get_publisher as get_browser_publisher
from .wechat_moments import WechatMomentsPublisher


def get_publisher(platform: str):
    if platform == "wechat_moments":
        return WechatMomentsPublisher()
    return get_browser_publisher(platform)

__all__ = ["get_publisher"]
