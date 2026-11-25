"""MiniMax Code 每日签到与积分。

接口（国内 / 国际按账号 region 选择）：

* ``GET  /minimax-cloud/api/v1/signin/status?timezone_id=...``
* ``POST /minimax-cloud/api/v1/signin/claim?timezone_id=...``  body ``{}``
* ``GET  /minimax-cloud/api/v1/credit/details``

鉴权是 ``Authorization: Bearer <access_token>``。access token 只有约 1 小时，
账号填了 refresh_token 时，遇到 401 会自动按客户端同款表单 grant 续期。
签到是账号级的，不需要切号、不需要客户端在线。
"""

from __future__ import annotations

import json
import os

import requests

from dailycheckin import CheckIn
from ide_spec import account_lock, seed_of

CLOUD_CN = "https://agent.minimax.cn/minimax-cloud/api/v1"
CLOUD_GLOBAL = "https://agent.minimax.io/minimax-cloud/api/v1"
OAUTH_CN = "https://account.minimax.cn/oauth2/token"
OAUTH_GLOBAL = "https://account.minimax.io/oauth2/token"
CLIENT_ID = "mcode-public"
SCOPE = "agent.default"
AUDIENCE = "agent-backend"
TIMEOUT = 20


class MiniMaxCode(CheckIn):
    name = "MiniMax Code"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}
        self.access_token = str(self.check_item.get("access_token") or "").strip()
        self.refresh_token = str(self.check_item.get("refresh_token") or "").strip()
        if not self.access_token and not self.refresh_token:
            raise ValueError("必须提供 MiniMax Code 的 access_token 或 refresh_token")
        self.seed = seed_of(self.check_item, "access_token", "refresh_token")
        self.region = str(self.check_item.get("region") or "cn").strip().lower()
        self.timezone = str(self.check_item.get("timezone") or "Asia/Shanghai").strip()
        self.timeout = int(self.check_item.get("timeout") or TIMEOUT)
        self.refreshed = False
        self.session = requests.Session()
        proxy = str(self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _cloud_base(self) -> str:
        custom = str(self.check_item.get("cloud_base") or "").strip().rstrip("/")
        if custom:
            return custom
        return CLOUD_GLOBAL if "global" in self.region else CLOUD_CN

    def _oauth_url(self) -> str:
        custom = str(self.check_item.get("oauth_url") or "").strip()
        if custom:
            return custom
        return OAUTH_GLOBAL if "global" in self.region else OAUTH_CN

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 retry: bool = True):
        url = self._cloud_base() + path
        sep = "&" if "?" in path else "?"
        url += sep + "timezone_id=" + requests.utils.quote(self.timezone, safe="")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        try:
            if method == "POST":
                resp = self.session.post(url, json=body or {}, headers=headers, timeout=self.timeout)
            else:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ValueError(f"请求 MiniMax 接口失败：{exc}") from exc
        if resp.status_code == 401 and retry and self._refresh():
            return self._request(path, method, body, retry=False)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            raise ValueError(
                f"MiniMax 接口返回了非 JSON 内容（HTTP {resp.status_code}），可能凭证失效或网络不通"
            )
        base_resp = data.get("base_resp") or {}
        code = int(base_resp.get("status_code") or 0)
        if code:
            raise ValueError(
                f"MiniMax 接口报错 {code}：{base_resp.get('status_msg') or '未知错误'}"
            )
        return data

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "audience": AUDIENCE,
        }
        try:
            resp = self.session.post(self._oauth_url(), data=form, timeout=self.timeout)
            data = resp.json()
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(data, dict):
            return False
        access = str(data.get("access_token") or "").strip()
        if not access:
            return False
        self.access_token = access
        self.refresh_token = str(data.get("refresh_token") or self.refresh_token).strip()
        self.refreshed = True
        return True

    def status(self) -> dict:
        data = self._request("/signin/status")
        obj = data.get("data") if isinstance(data.get("data"), dict) else data
        days = obj.get("days") if isinstance(obj.get("days"), list) else []
        today = next((d for d in days if isinstance(d, dict) and d.get("is_today")), None)
        checked = bool(today and int(today.get("status") or 0) == 3)
        return {
            "checked_in": checked,
            "points": float((today or {}).get("points") or 0),
            "day": int((today or {}).get("day_no") or 0),
        }

    def credits(self) -> float:
        try:
            data = self._request("/credit/details")
        except ValueError:
            return 0.0
        obj = data.get("data") if isinstance(data.get("data"), dict) else data
        details = obj.get("details") if isinstance(obj.get("details"), list) else []
        total = 0.0
        for one in details:
            if not isinstance(one, dict):
                continue
            try:
                total += float(one.get("remaining_amount") or 0)
            except (TypeError, ValueError):
                continue
        return total

    def checkin(self) -> dict:
        with account_lock("MINIMAXCODE", self.seed):
            state = self.status()
            if state["checked_in"]:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            data = self._request("/signin/claim", "POST", {})
            obj = data.get("data") if isinstance(data.get("data"), dict) else data
            result = int(obj.get("claim_result") or 0)
            if result == 2:
                return {"signed": False, "already": True, "credit": 0, "state": state}
            if result != 1:
                raise ValueError(f"MiniMax 签到返回未知结果 {result}")
            credit = float(obj.get("points") or state["points"] or 0)
            new_state = {
                "checked_in": True,
                "points": credit,
                "day": int(obj.get("day_no") or state["day"] or 0),
            }
            return {"signed": True, "already": False, "credit": credit, "state": new_state}

    def run(self) -> dict:
        return self.checkin()

    def main(self) -> str:
        result = self.checkin()
        lines = ["账号信息: MiniMax Code"]
        if self.refreshed:
            lines.append("凭证状态: 本次已自动续期（请把新的 token 回填到配置）")
        if result["already"]:
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
    print(MiniMaxCode(check_item=_data.get("MINIMAXCODE", [])[0]).main())
