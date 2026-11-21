"""豆包「天天领额度」每日签到。

接口只有两个，全部走 doubao.com 登录 Cookie，没有 a_bogus / msToken 签名：

1. ``POST /alice/ug/campaign/v3/detail/``   任务面板（含今天签没签、累计额度）
2. ``POST /alice/ug/campaign/landing/checkin``   领取（服务端幂等，重复领返回 idempotent:true）

只有个人账号有这个活动；豆包工作版 / 飞书企业账号的任务面板里没有
``daily_signin``，服务端会回 code=710010702，这里会如实说明，不当成 Cookie 失效。

多账号隔离
----------
每个账号用**自己的** seed 派生 web_id / tea_uuid 设备标识。同一个 Cookie
永远不会被套上别人的设备号，多个账号挂在同一个容器里也不会互相串线。
手动粘 Cookie 的账号没写 seed 时，用 Cookie 本身的哈希当 seed（稳定且各自不同）。
"""

import hashlib
import json
import os
import tempfile
import time
import urllib.parse
from contextlib import contextmanager

import requests

try:  # 目标是 Linux 容器 / NAS；非 POSIX 平台退化成不加锁，功能不受影响
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from dailycheckin import CheckIn

DETAIL_URL = "https://www.doubao.com/alice/ug/campaign/v3/detail/"
CHECKIN_URL = "https://www.doubao.com/alice/ug/campaign/landing/checkin"
CAMPAIGN_GROUP = "pc_incentive_task_mvp_v3"

LOGIN_INVALID = 710012001
NO_ACTIVITY = 710010702

TIMEOUT = 20

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
    """按账号 seed 派生一组稳定的网页设备号。seed 不同，设备号一定不同。"""
    digest = hashlib.sha256(("doubao-dev:" + str(seed or "")).encode("utf-8")).digest()
    return {
        "web_id": "76" + "".join(str(b % 10) for b in digest[:8]),
        "tea_uuid": "".join(str(b % 10) for b in digest[8:16]).ljust(16, "7"),
        "device_id": "",
        "region": "",
        "sys_region": "",
    }


def account_seed(check_item: dict) -> str:
    """扫码登录的账号带 seed；手动粘 Cookie 的账号用 Cookie 哈希兜底。"""
    seed = str((check_item or {}).get("seed") or "").strip()
    if seed:
        return seed
    return hashlib.sha256(str((check_item or {}).get("cookie") or "").encode("utf-8")).hexdigest()[:32]


def api_url(base: str, seed: str) -> str:
    params = dict(COMMON_PARAMS)
    params.update(device_ids(seed))
    return base + "?" + urllib.parse.urlencode(params)


LOCK_TIMEOUT = 120


def _lock_dir() -> str:
    """锁文件放运行时数据目录；面板起的进程和命令行进程会拿到同一个目录。"""
    home = os.environ.get("DC_HOME", "").strip()
    base = (
        os.path.join(home, "data", "locks")
        if home
        else os.path.join(tempfile.gettempdir(), "dailycheckin-doubao-locks")
    )
    os.makedirs(base, exist_ok=True)
    return base


def lock_path(seed: str) -> str:
    """每个账号（seed）一个锁文件，账号之间互不阻塞。"""
    digest = hashlib.sha256(("doubao-lock:" + str(seed or "")).encode("utf-8")).hexdigest()
    return os.path.join(_lock_dir(), digest[:32] + ".lock")


@contextmanager
def account_lock(seed: str, timeout: float = LOCK_TIMEOUT):
    """跨进程串行化同一账号的签到，避免手动点击与定时任务同时打写接口。"""
    with open(lock_path(seed), "a+") as fh:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("同一豆包账号的签到正在进行中，请稍后再试")
                    time.sleep(0.1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def parse_cookie(cookie: str) -> dict:
    """`k=v; k2=v2` → dict；值里的 `=` 原样保留（只切第一个等号）。"""
    out = {}
    for part in (cookie or "").replace("\r\n", "\n").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


class Doubao(CheckIn):
    name = "豆包"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}
        cookie = str(self.check_item.get("cookie") or "").strip()
        if not cookie:
            raise ValueError("必须提供豆包登录 Cookie（登录后从浏览器复制，或在面板里扫码获取）")
        self.seed = account_seed(self.check_item)
        self.timeout = int(self.check_item.get("timeout") or TIMEOUT)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": CLIENT_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json",
                "Origin": "https://www.doubao.com",
                "Referer": "https://www.doubao.com/pc/agent-explore",
            }
        )
        requests.utils.add_dict_to_cookiejar(self.session.cookies, parse_cookie(cookie))
        proxy = str(self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _post(self, url: str, body: dict) -> dict:
        try:
            resp = self.session.post(url, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ValueError(f"请求豆包接口失败：{exc}") from exc
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"豆包接口返回了非 JSON 内容（HTTP {resp.status_code}），可能被风控或网络不通"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError("豆包接口返回的数据格式无法识别")
        code = int(data.get("code") or 0)
        if code == LOGIN_INVALID:
            raise ValueError("豆包登录已过期（Cookie 失效），请重新扫码授权或粘贴新的 Cookie")
        return data

    # ------------------------------------------------------------------ #
    # 接口
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        """任务面板：签到状态 + 额度。没有签到任务时抛 ValueError 说明原因。"""
        data = self._post(
            api_url(DETAIL_URL, self.seed),
            {"campaign_group_code": CAMPAIGN_GROUP},
        )
        code = int(data.get("code") or 0)
        if code == LOGIN_INVALID:
            raise ValueError("豆包登录已过期（Cookie 失效），请重新扫码授权或粘贴新的 Cookie")
        if code == NO_ACTIVITY:
            raise ValueError("这个豆包账号没有「每日签到」活动（个人账号才有，工作版/飞书企业账号没有）")
        if code != 0:
            raise ValueError(f"豆包任务面板返回异常：{data.get('msg') or data.get('message') or ''}（code {code}）")
        payload = data.get("data") or {}
        tasks = payload.get("tasks") or []
        task = next((t for t in tasks if isinstance(t, dict) and t.get("task_kind") == "daily_signin"), None)
        if not task:
            raise ValueError("这个豆包账号没有「每日签到」任务（个人账号才有，工作版/飞书企业账号没有）")
        daily = task.get("daily_checkin") or {}
        reward = daily.get("reward") or {}
        progress = payload.get("progress") or {}
        return {
            "checked_in": daily.get("checked_in_today") is True,
            "continuous_days": int(daily.get("continuous_days") or 0),
            "reward_amount": int(reward.get("reward_amount") or 0),
            "total_amount": int(progress.get("total_amount") or 0),
        }

    def checkin(self) -> dict:
        """领取签到额度。同一账号加跨进程锁，且已签就不打写接口。"""
        with account_lock(self.seed):
            state = self.status()
            if state["checked_in"]:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            data = self._post(
                api_url(CHECKIN_URL, self.seed),
                {"scene": CAMPAIGN_GROUP},
            )
            code = int(data.get("code") or 0)
            if code == LOGIN_INVALID:
                raise ValueError("豆包登录已过期（Cookie 失效），请重新扫码授权或粘贴新的 Cookie")
            if code == NO_ACTIVITY:
                raise ValueError("豆包说这个账号今天没有可领的签到活动（个人账号才有）")
            if code != 0:
                raise ValueError(f"豆包签到失败：{data.get('msg') or data.get('message') or ''}（code {code}）")
            payload = data.get("data") or {}
            if payload.get("idempotent") is True:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            credit = int(payload.get("reward_amount") or state["reward_amount"] or 0)
            days = int(payload.get("continuous_days") or state["continuous_days"] or 0)
            total = int(state["total_amount"] or 0) + credit
            return {
                "signed": True,
                "already": False,
                "credit": credit,
                "state": {
                    "checked_in": bool(payload.get("checked_in", True)),
                    "continuous_days": days,
                    "reward_amount": credit,
                    "total_amount": total,
                },
            }

    def run(self) -> dict:
        return self.checkin()

    def main(self) -> str:
        result = self.run()
        state = result["state"]
        lines = ["账号信息: 豆包"]
        if result["already"]:
            lines.append("今日签到: 今日已签到（额度已领，未重复请求）")
        else:
            lines.append(f"今日签到: 签到成功，+{result['credit'] or state['reward_amount']} 额度")
        lines.append(f"累计额度: {state['total_amount']} 点")
        lines.append(f"连续签到: {state['continuous_days']} 天")
        return "\n".join(lines)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(config_path, encoding="utf-8") as f:
        _data = json.load(f)
    print(Doubao(check_item=_data.get("DOUBAO", [])[0]).main())
