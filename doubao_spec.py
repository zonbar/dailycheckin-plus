"""豆包签到公共规格：引擎与网页面板共用同一份常量和设备号规则。

这里只依赖标准库。面板必须保持零第三方依赖，不能 import `dailycheckin`
（它会连带导入 requests），所以把这些定义放在仓库顶层由两边共同引用。
设备号规则必须与引擎一致，否则面板显示「有效」而实际签到会串账号。
"""

from __future__ import annotations

import hashlib

DETAIL_URL = "https://www.doubao.com/alice/ug/campaign/v3/detail/"
CAMPAIGN_GROUP = "pc_incentive_task_mvp_v3"
LOGIN_INVALID = 710012001
NO_ACTIVITY = 710010702

CLIENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) DoubaoDesktop/2.32.6 Chrome/147.0.7727.149 Safari/537.36"
)

COMMON_PARAMS = {
    "version_code": "20800",
    "language": "zh",
    "device_platform": "web",
    "doubao_device_platform": "web",
    "aid": "497858",
    "real_aid": "497858",
    "pkg_type": "release_version",
    "pc_version": "2.32.6",
    "doubao_pc_version": "5.72.66",
    "samantha_web": "1",
    "web_platform": "desktop",
    "use-olympus-account": "1",
    "web_tab_id": "0",
}


def device_ids(seed: str) -> dict:
    """按账号 seed 派生一组稳定的网页设备号；seed 不同，设备号一定不同。"""
    digest = hashlib.sha256(("doubao-dev:" + str(seed or "")).encode("utf-8")).digest()
    return {
        "web_id": "76" + "".join(str(b % 10) for b in digest[:8]),
        "tea_uuid": "".join(str(b % 10) for b in digest[8:16]).ljust(16, "7"),
        "device_id": "",
        "region": "",
        "sys_region": "",
    }


def account_seed(account: dict) -> str:
    """扫码账号带 seed；手动粘 Cookie 的账号用 Cookie 哈希兜底。"""
    seed = str((account or {}).get("seed") or "").strip()
    if seed:
        return seed
    return hashlib.sha256(str((account or {}).get("cookie") or "").encode("utf-8")).hexdigest()[:32]
