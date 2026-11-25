"""Qoder 每日签到：活动列表查询 + 领取积分。

接口（国内 / 国际双 base 自动探测）：

* ``GET  /sash/api/v1/me/campaigns``
* ``POST /sash/api/v1/me/campaigns/{id}/claim``  body ``{}``
* ``POST /api/v1/deviceToken/refresh``           轮换 refresh_token

只领取 ``actionType=CLAIM_BENEFIT`` 且 ``claimStatus=CLAIMABLE`` 的活动。
「今天」按 UTC+8 自然日判断：昨天的已领活动会留在列表里，不能算今日已签。
服务端对真实机器指纹才稳定下发活动，因此账号可显式配置 Cosy-* 指纹字段；
没配置时按账号派生一组稳定值（可能被风控，签到结果会照实提示）。
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from dailycheckin import CheckIn
from ide_spec import account_lock, qoder_device, seed_of

BASE_CN = "https://openapi.qoder.com.cn"
BASE_GLOBAL = "https://openapi.qoder.sh"
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
PATH_CLAIM = "/sash/api/v1/me/campaigns/{}/claim"
PATH_REFRESH = "/api/v1/deviceToken/refresh"

CST = timezone(timedelta(hours=8))
TIMEOUT = 20


class Qoder(CheckIn):
    name = "Qoder"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}
        self.token = str(self.check_item.get("token") or "").strip()
        self.refresh_token = str(self.check_item.get("refresh_token") or "").strip()
        if not self.token and not self.refresh_token:
            raise ValueError("必须提供 Qoder 的 token（access token）或 refresh_token")
        self.email = str(self.check_item.get("email") or "").strip()
        self.seed = seed_of(self.check_item, "email", "user_id", "token")
        self.device = qoder_device(self.check_item, self.seed)
        self.scope = str(self.check_item.get("scope") or "cn").strip().lower()
        self.timeout = int(self.check_item.get("timeout") or TIMEOUT)
        self.refreshed = False
        self.session = requests.Session()
        proxy = str(self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _bases(self) -> list[str]:
        custom = str(self.check_item.get("base") or "").strip().rstrip("/")
        if custom:
            return [custom]
        return [BASE_GLOBAL, BASE_CN] if "global" in self.scope else [BASE_CN, BASE_GLOBAL]

    def _headers(self, token: str, with_body: bool = False) -> dict:
        dev = self.device
        headers = {
            "Accept": "application/json",
            "User-Agent": "Qoder/claim",
        }
        if with_body:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        for key, value in (
            ("Cosy-ClientType", dev["client_type"]),
            ("Cosy-MachineOS", dev["machine_os"]),
            ("Cosy-MachineHostname", dev["machine_hostname"]),
            ("Cosy-MachineId", dev["machine_id"]),
            ("Cosy-MachineToken", dev["machine_token"]),
            ("Cosy-MachineCode", dev["machine_code"]),
            ("Cosy-MachineType", dev["machine_type"]),
            ("Cosy-Version", dev["version"]),
        ):
            if value:
                headers[key] = value
        return headers

    def _request(self, base: str, path: str, method: str = "GET",
                 body: dict | None = None, token: str | None = None):
        url = base.rstrip("/") + path
        try:
            if method == "POST":
                resp = self.session.post(
                    url, json=body if body is not None else {},
                    headers=self._headers(token or "", with_body=True),
                    timeout=self.timeout,
                )
            else:
                resp = self.session.get(
                    url, headers=self._headers(token or ""), timeout=self.timeout
                )
        except requests.RequestException as exc:
            raise ValueError(f"请求 Qoder 接口失败：{exc}") from exc
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        return resp.status_code, data

    def _refresh(self, base: str) -> bool:
        if not self.refresh_token:
            return False
        status, data = self._request(
            base, PATH_REFRESH, "POST", {"refresh_token": self.refresh_token}
        )
        if status != 200 or not isinstance(data, dict):
            return False
        obj = data.get("data") if isinstance(data.get("data"), dict) else data
        access = ""
        for key in ("device_token", "accessToken", "access_token", "token"):
            if str(obj.get(key) or "").strip():
                access = str(obj[key]).strip()
                break
        if not access:
            return False
        self.token = access
        new_refresh = str(obj.get("refreshToken") or obj.get("refresh_token") or "").strip()
        if new_refresh:
            self.refresh_token = new_refresh
        self.refreshed = True
        return True

    @staticmethod
    def _campaign_list(data) -> list[dict]:
        if not isinstance(data, dict):
            return []
        for obj in (data, data.get("data")):
            if not isinstance(obj, dict):
                continue
            items = obj.get("campaigns")
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    def _campaigns(self) -> list[dict]:
        last = ""
        empty: list[dict] = []
        for base in self._bases():
            status, data = self._request(base, PATH_CAMPAIGNS, token=self.token)
            if status == 401 and self._refresh(base):
                status, data = self._request(base, PATH_CAMPAIGNS, token=self.token)
            if status == 401:
                last = "token 已过期且刷新失败，请在 Qoder 重新登录并更新 token"
                continue
            if status != 200:
                last = f"查询活动失败（HTTP {status}）"
                continue
            items = self._campaign_list(data)
            if items:
                return items
            empty = items
        if not last and not empty:
            last = "未查到可领取的签到活动"
        if last:
            raise ValueError(last)
        return []

    @staticmethod
    def _is_today(campaign: dict) -> bool:
        start = campaign.get("startAt") or campaign.get("start_at") or 0
        try:
            start = int(start)
        except (TypeError, ValueError):
            start = 0
        if start <= 0:
            return True
        today = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
        return datetime.fromtimestamp(start, CST) >= today

    @staticmethod
    def _classify(campaigns: list[dict]):
        claimable, claimed_today, stale = [], None, None
        for one in campaigns:
            if str(one.get("actionType") or "") != "CLAIM_BENEFIT":
                continue
            status = str(one.get("claimStatus") or "")
            if status == "CLAIMABLE":
                claimable.append(one)
            elif status == "CLAIMED":
                if Qoder._is_today(one):
                    claimed_today = claimed_today or one
                else:
                    stale = stale or one
        return claimable, claimed_today, stale

    @staticmethod
    def _claim_result(status: int, data) -> tuple[bool, float]:
        if status != 200 or not isinstance(data, dict):
            return False, 0.0
        for obj in (data.get("data"), data):
            if not isinstance(obj, dict):
                continue
            if str(obj.get("status") or "") == "CLAIMED":
                benefit = obj.get("benefit")
                amount = benefit.get("amount") if isinstance(benefit, dict) else obj.get("amount")
                try:
                    return True, float(amount or 0)
                except (TypeError, ValueError):
                    return True, 0.0
        if isinstance(data.get("data"), dict) and "CLAIMED" in json.dumps(data, ensure_ascii=False):
            return True, 0.0
        return False, 0.0

    def status(self) -> dict:
        claimable, claimed, stale = self._classify(self._campaigns())
        amount = 0.0
        if claimed:
            benefit = claimed.get("benefit")
            if isinstance(benefit, dict):
                try:
                    amount = float(benefit.get("amount") or 0)
                except (TypeError, ValueError):
                    amount = 0.0
        return {
            "checked_in": claimed is not None,
            "claimable": len(claimable),
            "pending": bool(stale) and claimed is None and not claimable,
            "credits": amount,
        }

    def checkin(self) -> dict:
        with account_lock("QODER", self.seed):
            claimable, claimed, stale = self._classify(self._campaigns())
            state = {
                "checked_in": claimed is not None,
                "claimable": len(claimable),
                "pending": bool(stale) and claimed is None and not claimable,
                "credits": 0.0,
            }
            if not claimable:
                if claimed is not None:
                    return {"signed": False, "already": True, "credit": 0, "state": state}
                return {
                    "signed": False, "already": False, "pending": True,
                    "credit": 0, "state": state,
                }
            base = self._bases()[0]
            got, done, failed = 0.0, 0, []
            for campaign in claimable:
                cid = str(campaign.get("campaignId") or campaign.get("campaign_id") or "").strip()
                if not cid:
                    continue
                status, data = self._request(base, PATH_CLAIM.format(cid), "POST", {})
                if status == 401 and self._refresh(base):
                    status, data = self._request(base, PATH_CLAIM.format(cid), "POST", {})
                ok, amount = self._claim_result(status, data)
                if ok:
                    done += 1
                    got += amount or float((campaign.get("benefit") or {}).get("amount") or 0)
                else:
                    failed.append(f"{cid}:HTTP{status}")
            if done == 0:
                raise ValueError("Qoder 签到失败：" + ("；".join(failed) or "没有可领活动"))
            state.update({"checked_in": True, "credits": got})
            return {"signed": True, "already": False, "credit": got, "state": state}

    def run(self) -> dict:
        return self.checkin()

    def main(self) -> str:
        result = self.checkin()
        lines = ["账号信息: Qoder"]
        if self.refreshed:
            lines.append("凭证状态: 本次已自动续期（请把新的 token / refresh_token 回填到配置）")
        if result.get("pending"):
            lines.append("今日签到: 还没有下发可领活动，请稍后重试或在客户端确认")
        elif result["already"]:
            lines.append("今日签到: 今日已签到（未重复请求）")
        else:
            lines.append(f"今日签到: 签到成功，+{result['credit']:.0f} Credits")
        return "\n".join(lines)


if __name__ == "__main__":
    import json

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(path, encoding="utf-8") as f:
        _data = json.load(f)
    print(Qoder(check_item=_data.get("QODER", [])[0]).main())
