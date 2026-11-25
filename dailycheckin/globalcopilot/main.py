"""国际版 Copilot 每日签到与积分查询。

接口：

* ``GET  {endpoint}/v2/plugin/checkin/status``
* ``POST {endpoint}/v2/plugin/checkin/claim``  body ``{}``
* ``GET  {endpoint}/v2/plugin/account/usage``  积分/用量（可选展示）
* ``POST {endpoint}/v2/plugin/auth/token/refresh``  续期（可选）

账号填 access_token 即可；refresh_token / domain 可选。国际版没有国内那套
每日签到活动时，上游会回 code=10001「活动未开启或已过期」，这里按「无活动」
如实说明，不当成凭证失效。
"""

from __future__ import annotations

import json
import os

import requests

from dailycheckin import CheckIn
from ide_spec import account_lock, seed_of

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
TIMEOUT = 20
NO_ACTIVITY_CODE = 10001


class GlobalCopilot(CheckIn):
    name = "WorkBuddy 国际版"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}
        self.access_token = str(self.check_item.get("access_token") or "").strip()
        self.refresh_token = str(self.check_item.get("refresh_token") or "").strip()
        if not self.access_token and not self.refresh_token:
            raise ValueError("必须提供国际版 Copilot 的 access_token（或 refresh_token）")
        self.seed = seed_of(self.check_item, "access_token", "refresh_token")
        self.endpoint = str(
            self.check_item.get("endpoint") or DEFAULT_ENDPOINT
        ).strip().rstrip("/")
        self.domain = str(self.check_item.get("domain") or "").strip()
        self.timeout = int(self.check_item.get("timeout") or TIMEOUT)
        self.refreshed = False
        self.session = requests.Session()
        proxy = str(self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _headers(self, with_refresh: bool = False) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        if self.refresh_token:
            headers["X-Refresh-Token"] = self.refresh_token
        if self.domain:
            headers["X-Domain"] = self.domain
        if with_refresh:
            headers["X-Auth-Refresh-Source"] = "plugin"
        return headers

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 retry: bool = True) -> dict:
        url = self.endpoint + path
        try:
            if method == "POST":
                resp = self.session.post(url, json=body or {}, headers=self._headers(), timeout=self.timeout)
            else:
                resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise ValueError(f"请求国际版 Copilot 接口失败：{exc}") from exc
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            raise ValueError(
                f"接口返回了非 JSON 内容（HTTP {resp.status_code}），请检查 endpoint 或登录态"
            )
        if not isinstance(data, dict):
            raise ValueError("接口返回的数据格式无法识别")
        code = int(data.get("code") or 0)
        if code == NO_ACTIVITY_CODE and path.endswith("/checkin/claim"):
            data["_no_activity"] = True
            return data
        if retry and code in (401, 1001, 10001) and self._refresh():
            return self._request(path, method, body, retry=False)
        if code not in (0, NO_ACTIVITY_CODE):
            msg = str(data.get("msg") or data.get("message") or "").strip()
            if "已签" in msg:
                data["_already"] = True
                return data
            raise ValueError(f"接口返回 code={code}：{msg}")
        return data

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        url = self.endpoint + "/v2/plugin/auth/token/refresh"
        try:
            resp = self.session.post(url, json={}, headers=self._headers(True), timeout=self.timeout)
            data = resp.json()
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(data, dict) or int(data.get("code") or 0) != 0:
            return False
        obj = data.get("data") if isinstance(data.get("data"), dict) else data
        access = str(obj.get("accessToken") or obj.get("access_token") or "").strip()
        if not access:
            return False
        self.access_token = access
        self.refresh_token = str(obj.get("refreshToken") or obj.get("refresh_token") or self.refresh_token).strip()
        self.refreshed = True
        return True

    def status(self) -> dict:
        data = self._request("/v2/plugin/checkin/status")
        obj = data.get("data") if isinstance(data.get("data"), dict) else {}
        checked = bool(
            obj.get("checked_in") or obj.get("checkedIn") or obj.get("signed") or data.get("_already")
        )
        return {
            "checked_in": checked,
            "points": float(obj.get("points") or obj.get("credits") or 0),
            "no_activity": bool(data.get("_no_activity")),
        }

    def credits(self) -> float:
        try:
            data = self._request("/v2/plugin/account/usage")
        except ValueError:
            return 0.0
        obj = data.get("data") if isinstance(data.get("data"), dict) else {}
        for key in ("credits", "remaining", "balance"):
            try:
                value = float(obj.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value:
                return value
        return 0.0

    def checkin(self) -> dict:
        with account_lock("GLOBALCOPILOT", self.seed):
            state = self.status()
            if state["checked_in"]:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            data = self._request("/v2/plugin/checkin/claim", "POST", {})
            if data.get("_no_activity"):
                state["no_activity"] = True
                return {"signed": False, "already": False, "no_activity": True, "credit": 0, "state": state}
            if data.get("_already"):
                return {"signed": False, "already": True, "credit": 0, "state": state}
            obj = data.get("data") if isinstance(data.get("data"), dict) else {}
            credit = float(obj.get("points") or obj.get("credits") or 0)
            new_state = {"checked_in": True, "points": credit, "no_activity": False}
            return {"signed": True, "already": False, "credit": credit, "state": new_state}

    def run(self) -> dict:
        return self.checkin()

    def main(self) -> str:
        result = self.checkin()
        lines = ["账号信息: 国际版 Copilot"]
        if self.refreshed:
            lines.append("凭证状态: 本次已自动续期（请把新的 token 回填到配置）")
        if result.get("no_activity"):
            lines.append("今日签到: 当前没有开放的签到活动（上游 code=10001），不算失败")
        elif result["already"]:
            lines.append("今日签到: 今日已签到（未重复请求）")
        else:
            lines.append(f"今日签到: 签到成功，+{result['credit']:.0f} 积分")
        balance = self.credits()
        if balance:
            lines.append(f"积分余额: {balance:.0f}")
        return "\n".join(lines)


if __name__ == "__main__":
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(path, encoding="utf-8") as f:
        _data = json.load(f)
    print(GlobalCopilot(check_item=_data.get("GLOBALCOPILOT", [])[0]).main())
