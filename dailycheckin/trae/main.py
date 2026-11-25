"""Trae（CN）每日签到：领积分与余额查询。

端点与请求头对齐 Trae 客户端内置脚本：

* ``POST /trae/api/v2/ug/checkin_credits/status``  预检（是否已签、可领积分）
* ``POST /trae/api/v2/ug/checkin_credits/claim``   签到领积分（服务端幂等）
* ``POST /trae/api/v2/pay/ide_user_ent_usage``     积分余额（可选展示）

鉴权是 ``Authorization: Cloud-IDE-JWT <jwt>``。设备三件套按账号确定性派生：
同一账号每次签到设备号一致，不同账号互不串线。
"""

import os
import time

import requests

from dailycheckin import CheckIn
from ide_spec import account_lock, jwt_payload, jwt_user_id, request_id, seed_of, trae_device

STATUS_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/status"
CLAIM_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim"
USAGE_URL = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"

TIMEOUT = 20
UA = "VSCode 1.107.1 (TRAE SOLO CN)"


class Trae(CheckIn):
    name = "Trae"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}
        jwt = str(self.check_item.get("jwt") or "").strip()
        if not jwt:
            raise ValueError("必须提供 Trae 的 Cloud-IDE-JWT（在 Trae 客户端登录态里复制）")
        self.jwt = jwt.removeprefix("Cloud-IDE-JWT ")
        self.seed = seed_of(self.check_item, "user_id", "jwt")
        self.user_id = str(self.check_item.get("user_id") or "").strip() or jwt_user_id(self.jwt)
        self.device = trae_device(self.check_item, self.seed)
        self.timeout = int(self.check_item.get("timeout") or TIMEOUT)
        self.session = requests.Session()
        # 参考实现显式禁用环境代理：Trae 助手写的系统代理常指向已退出的本地端口。
        self.session.trust_env = False
        proxy = str(self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _auth_hint(self) -> str:
        payload = jwt_payload(self.jwt)
        exp = payload.get("exp")
        try:
            if exp and float(exp) < time.time():
                return "Trae JWT 已过期，请在 Trae 客户端重新登录后复制新的 Cloud-IDE-JWT"
        except (TypeError, ValueError):
            pass
        return ""

    def _post(self, url: str, body: dict) -> dict:
        dev = self.device
        auth = "Cloud-IDE-JWT " + self.jwt
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN",
            "authorization": auth,
            "content-type": "application/json",
            "user-agent": UA,
            "x-market-client-id": UA,
            "x-market-user-id": dev["market_user_id"],
            "x-user-region": "CN",
            "x-device-id": dev["device_id"],
            "x-lgw-req-sdk-type": "3",
            "package-type": "stable_cn",
            "x-request-id": request_id(),
            "x-lscbd-aid": "787976",
            "x-lscbd-platform": "windows",
            "app-version": "0.1.45",
            "x-tt-trace-id": "00-" + request_id().replace("-", "")[:16] + "-01",
            "vscode-sessionid": dev["session_id"],
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "none",
        }
        try:
            resp = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ValueError(f"请求 Trae 接口失败：{exc}") from exc
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Trae 接口返回了非 JSON 内容（HTTP {resp.status_code}），可能凭证失效或网络不通"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError("Trae 接口返回的数据格式无法识别")
        return data

    def _check(self, data: dict, what: str) -> dict:
        code = int(data.get("code") or 0)
        if code == 0:
            return data
        hint = self._auth_hint()
        if hint:
            raise ValueError(hint)
        msg = str(data.get("message") or data.get("msg") or "").strip()
        raise ValueError(f"Trae{what}失败：{msg}（code {code}）")

    @staticmethod
    def _credits(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def status(self) -> dict:
        data = self._check(self._post(STATUS_URL, {}), "任务面板")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        return {
            "checked_in": payload.get("checked_in") is True or payload.get("checked_in_today") is True,
            "credits": self._credits(payload.get("credits")),
            "message": str(payload.get("message") or "").strip(),
        }

    def credits_usage(self) -> float:
        """查询积分余额；失败不影响签到，返回 0 让调用方自行忽略。"""
        try:
            data = self._check(self._post(USAGE_URL, {"require_usage": True}), "积分查询")
        except ValueError:
            return 0.0
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        packs = payload.get("credits") or payload.get("credit_packs") or []
        remain = 0.0
        if isinstance(packs, list):
            for one in packs:
                if not isinstance(one, dict):
                    continue
                limit = self._credits(one.get("credits_limit") or one.get("limit"))
                used = self._credits(one.get("used") or one.get("used_amount"))
                remain += max(0.0, limit - used)
        return remain or self._credits(payload.get("remaining"))

    def checkin(self) -> dict:
        with account_lock("TRAE", self.seed):
            state = self.status()
            if state["checked_in"]:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            data = self._check(self._post(CLAIM_URL, {}), "签到")
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            if payload.get("idempotent") is True or payload.get("already") is True:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            credit = self._credits(payload.get("credits") or state["credits"])
            new_state = {
                "checked_in": True,
                "credits": credit,
                "message": str(payload.get("message") or "").strip(),
            }
            return {"signed": True, "already": False, "credit": credit, "state": new_state}

    def run(self) -> dict:
        return self.checkin()

    def main(self) -> str:
        result = self.checkin()
        state = result["state"]
        lines = ["账号信息: Trae"]
        if result["already"]:
            lines.append("今日签到: 今日已签到（未重复请求）")
        else:
            lines.append(f"今日签到: 签到成功，+{result['credit']:.0f} 积分")
        if state.get("message"):
            lines.append(f"接口提示: {state['message']}")
        balance = self.credits_usage()
        if balance:
            lines.append(f"积分余额: {balance:.0f}")
        return "\n".join(lines)


if __name__ == "__main__":
    import json

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(path, encoding="utf-8") as f:
        _data = json.load(f)
    print(Trae(check_item=_data.get("TRAE", [])[0]).main())
