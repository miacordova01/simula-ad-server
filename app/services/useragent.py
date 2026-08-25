"""User-Agent -> operating system.

`ua-parser` handles the long tail of real-world UA strings. We only need to
collapse its output onto the campaign targeting vocabulary (ios / android /
web), so the mapping is deliberately small and explicit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ua_parser import parse as ua_parse

from ..models.common import OS

log = logging.getLogger(__name__)

_IOS_FAMILIES = {"ios", "iphone os", "ipados", "watchos", "mac os x", "macos"}
_ANDROID_FAMILIES = {"android"}


@dataclass(frozen=True)
class DeviceInfo:
    os: str
    device_family: str | None
    raw: str | None


def parse_device(user_agent: str | None) -> DeviceInfo:
    """Classify a UA string into a targeting OS.

    Apple desktop (macOS) is folded into `ios` because campaigns target app
    stores, and an iOS campaign's store URL is the one that makes sense for an
    Apple user. Anything we cannot place becomes `unknown`, which fails closed
    against OS-targeted campaigns rather than guessing.
    """
    if not user_agent or not user_agent.strip():
        return DeviceInfo(os=OS.unknown, device_family=None, raw=user_agent)

    try:
        result = ua_parse(user_agent)
    except Exception:
        log.exception("ua parse failed")
        return DeviceInfo(os=OS.unknown, device_family=None, raw=user_agent)

    os_family = (result.os.family if result.os else "") or ""
    device_family = (result.device.family if result.device else None) or None
    fam = os_family.strip().lower()

    if fam in _ANDROID_FAMILIES:
        resolved = OS.android
    elif fam in _IOS_FAMILIES:
        resolved = OS.ios
    elif fam:
        # A recognised OS that is not mobile (Windows, Linux, Chrome OS...).
        resolved = OS.web
    else:
        resolved = OS.unknown

    return DeviceInfo(os=resolved, device_family=device_family, raw=user_agent)
