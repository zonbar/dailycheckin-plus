#!/usr/bin/env python3
"""AI 编辑器类签到模块离线测试：不联网、不起浏览器。

覆盖四件容易回归的事：
1. 新站点注册进引擎与面板元数据，旧站点彻底移除；
2. 设备指纹按账号稳定派生、账号之间不串线；
3. 四个模块都先预检再领取，已签不重复打写接口；
4. Qoder 陈旧活动不算今日已签，国际版无活动不算失败。

跑法：python3 tools/test_ai_modules.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ide_spec
from dailycheckin.configs import checkin_map
from panel import catalog

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f" :: {detail}" if not cond else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Scripted:
    """按调用顺序返回预设响应；handler 为可调用时按 (method,url,kw) 动态返回。"""

    def __init__(self, seq=None, handler=None):
        self.seq, self.handler, self.calls = list(seq or []), handler, []

    def _take(self, method, url, kw):
        self.calls.append((method, url, kw))
        if self.handler:
            return self.handler(method, url, kw)
        if not self.seq:
            raise AssertionError("fake session 响应用完了：" + url)
        item = self.seq.pop(0)
        return Resp(*item) if isinstance(item, tuple) else Resp(item)

    def get(self, url, **kw):
        return self._take("GET", url, kw)

    def post(self, url, **kw):
        return self._take("POST", url, kw)


# --------------------------------------------------------------------------- #
section("1) 站点注册与元数据")
keys = set(checkin_map)
for key in ("TRAE", "QODER", "MINIMAXCODE", "GLOBALCOPILOT"):
    check(f"{key} 已注册进引擎", key in keys, str(sorted(keys)))
for key in ("AOLAXING", "ACFUN", "BAIDU"):
    check(f"{key} 已从引擎移除", key not in keys)
    check(f"{key} 元数据已删除", key not in catalog.SITE_META)
    check(f"{key} 官网链接已删除", key not in catalog.SITE_LINKS)
check("贴吧 / 百度网盘仍在", "TIEBA" in keys and "BAIDUWP" in keys)
check("面板元数据无缺口", catalog.missing_meta() == [], str(catalog.missing_meta()))
for key in ("TRAE", "QODER", "MINIMAXCODE", "GLOBALCOPILOT"):
    meta = catalog.site_meta(key)
    check(f"{key} 是表单凭据站点", meta["credential"] == "form" and meta["known"] is True)
    check(f"{key} 有必填字段说明", any(f.get("required") for f in meta["fields"]), str(meta["fields"]))

# --------------------------------------------------------------------------- #
section("2) 设备派生与账号隔离")
raw = ide_spec.seeded_bytes("seed-a", "devid", 40)
check("确定性派生可复现", raw == ide_spec.seeded_bytes("seed-a", "devid", 40))
check("不同种子派生不同设备", raw != ide_spec.seeded_bytes("seed-b", "devid", 40))
check("数字设备号长度正确", len(ide_spec.digits("seed-a", "devid", 15)) == 15)
check("十六进制会话号长度正确", len(ide_spec.hex_string("seed-a", "sess", 64)) == 64)
check("UUID 版本位正确", ide_spec.uuid_from_seed("a", "market")[14] == "4")
dev_a = ide_spec.trae_device({}, "seed-a")
dev_b = ide_spec.trae_device({}, "seed-b")
check("Trae 三件套按账号隔离",
      dev_a["device_id"] != dev_b["device_id"]
      and dev_a["session_id"] != dev_b["session_id"]
      and dev_a["market_user_id"] != dev_b["market_user_id"], str(dev_a))
check("显式设备号优先", ide_spec.trae_device({"device_id": "123"}, "s")["device_id"] == "123")
qd_a = ide_spec.qoder_device({"email": "a@x.com"}, "sa")
qd_b = ide_spec.qoder_device({"email": "b@x.com"}, "sb")
check("Qoder 机器指纹按账号隔离",
      qd_a["machine_token"] != qd_b["machine_token"]
      and qd_a["machine_code"] != qd_b["machine_code"])
check("显式机器指纹不被覆盖",
      ide_spec.qoder_device({"machine_token": "real"}, "s")["machine_token"] == "real")

# --------------------------------------------------------------------------- #
section("3) Trae：预检 + 幂等领取")
from dailycheckin.trae.main import Trae

trae = Trae({"jwt": "header.eyJleHAiOjQxMDI0NDQ4MDAwLCJkYXRhIjp7ImlkIjoxMjM0fX0.sig"})
trae.session = Scripted([
    ({"code": 0, "data": {"checked_in": False, "credits": 10}},),
    ({"code": 0, "data": {"credits": 12}},),
    ({"code": 0, "data": {"credits": [{"credits_limit": 100, "used": 30}]}},),
])
res = trae.checkin()
check("Trae 领取积分", res["signed"] is True and res["credit"] == 12, str(res))
check("Trae JWT 里解析出 user id", trae.user_id == "1234", trae.user_id)
check("Trae 请求带 Cloud-IDE-JWT",
      any("Cloud-IDE-JWT " in str(c[2].get("headers", {}).get("authorization", ""))
          for c in trae.session.calls if c[0] == "POST"))
trae_done = Trae({"jwt": "x.y.z"})
trae_done.session = Scripted([{"code": 0, "data": {"checked_in": True, "credits": 10}}])
res2 = trae_done.checkin()
check("Trae 已签不再打领取接口",
      res2["already"] is True and len(trae_done.session.calls) == 1, str(trae_done.session.calls))
past_jwt = "h." + __import__("base64").urlsafe_b64encode(
    json.dumps({"exp": 1}).encode()).decode().rstrip("=") + ".s"
trae_bad = Trae({"jwt": past_jwt})
trae_bad.session = Scripted([{"code": 1001, "msg": "unauthorized"}])
try:
    trae_bad.status()
    check("Trae JWT 过期给出可操作提示", False)
except ValueError as exc:
    check("Trae JWT 过期给出可操作提示", "过期" in str(exc), str(exc))

# --------------------------------------------------------------------------- #
section("4) Qoder：活动分类与领取")
from dailycheckin.qoder.main import Qoder

today = int(time.time())
yesterday = today - 86400
campaigns = {"code": 0, "data": {"campaigns": [
    {"campaignId": "c1", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
     "startAt": today, "benefit": {"amount": 100}},
    {"campaignId": "old", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMED",
     "startAt": yesterday, "benefit": {"amount": 100}},
]}}
qoder = Qoder({"token": "tk", "email": "a@x.com"})
qoder.session = Scripted([
    (campaigns,),
    ({"code": 0, "data": {"status": "CLAIMED", "benefit": {"amount": 100}}},),
])
res = qoder.checkin()
check("Qoder 只领今日可领活动", res["signed"] is True and res["credit"] == 100, str(res))
check("Qoder 领取请求带机器指纹",
      any("Cosy-MachineToken" in str(c[2].get("headers", {})) for c in qoder.session.calls))
qoder_claimed = Qoder({"token": "tk"})
qoder_claimed.session = Scripted([{"code": 0, "data": {"campaigns": [
    {"campaignId": "today", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMED",
     "startAt": today, "benefit": {"amount": 100}},
]}}])
res2 = qoder_claimed.checkin()
check("Qoder 今日已领不重复请求",
      res2["already"] is True and len(qoder_claimed.session.calls) == 1)
qoder_stale = Qoder({"token": "tk"})
qoder_stale.session = Scripted([{"code": 0, "data": {"campaigns": [
    {"campaignId": "old", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMED",
     "startAt": yesterday, "benefit": {"amount": 100}},
]}}])
res3 = qoder_stale.checkin()
check("Qoder 陈旧活动不算今日已签",
      res3.get("pending") is True and res3["already"] is False, str(res3))

# --------------------------------------------------------------------------- #
section("5) MiniMax Code：签到 / 积分 / 自动续期")
from dailycheckin.minimaxcode.main import MiniMaxCode

mm = MiniMaxCode({"access_token": "at"})
mm.session = Scripted([
    ({"base_resp": {"status_code": 0}, "data": {"days": [
        {"day_no": 3, "points": 10, "status": 1, "is_today": True}]}},),
    ({"base_resp": {"status_code": 0}, "data": {
        "claim_result": 1, "day_no": 3, "points": 10}},),
    ({"base_resp": {"status_code": 0}, "data": {"details": [
        {"remaining_amount": "50", "granted_amount": "100"}]}},),
])
res = mm.checkin()
check("MiniMax 领取今日积分", res["signed"] is True and res["credit"] == 10, str(res))
check("MiniMax 账单带结算时区",
      all("timezone_id=" in c[1] for c in mm.session.calls if "/signin/" in c[1]))
mm_done = MiniMaxCode({"access_token": "at"})
mm_done.session = Scripted([{"base_resp": {"status_code": 0}, "data": {"days": [
    {"day_no": 3, "points": 10, "status": 3, "is_today": True}]}}])
res2 = mm_done.checkin()
check("MiniMax 已签不再领取", res2["already"] is True and len(mm_done.session.calls) == 1)

state = {"n": 0}


def mm_refresh(method, url, kw):
    if "/oauth2/token" in url:
        return Resp({"access_token": "at-new", "refresh_token": "rt-new"})
    state["n"] += 1
    if state["n"] == 1:
        return Resp({"msg": "expired"}, status=401)
    return Resp({"base_resp": {"status_code": 0}, "data": {"days": [
        {"day_no": 1, "points": 5, "status": 3, "is_today": True}]}})


mm_renew = MiniMaxCode({"access_token": "old", "refresh_token": "rt"})
mm_renew.session = Scripted(handler=mm_refresh)
res3 = mm_renew.checkin()
check("MiniMax 401 后自动续期再签到",
      res3["already"] is True and mm_renew.access_token == "at-new", str(res3))

# --------------------------------------------------------------------------- #
section("6) 国际版 Copilot：签到与无活动")
from dailycheckin.globalcopilot.main import GlobalCopilot

gc = GlobalCopilot({"access_token": "at"})
gc.session = Scripted([
    ({"code": 0, "data": {"checked_in": False}},),
    ({"code": 0, "data": {"points": 5}},),
    ({"code": 0, "data": {"credits": 30}},),
])
res = gc.checkin()
check("国际版领取积分", res["signed"] is True and res["credit"] == 5, str(res))
check("国际版请求带匿名头",
      all(c[2]["headers"].get("X-No-User-Id") == "true" for c in gc.session.calls))
gc_none = GlobalCopilot({"access_token": "at"})
gc_none.session = Scripted([
    {"code": 0, "data": {"checked_in": False}},
    {"code": 10001, "msg": "签到活动未开启或已过期"},
])
res2 = gc_none.checkin()
check("无活动不算凭证失效",
      res2.get("no_activity") is True and res2["already"] is False, str(res2))

# --------------------------------------------------------------------------- #
section("7) 同步工具不会让已删站点复活")
from tools import sync_upstream as su

check("四个新站点登记为本地资产",
      {"trae", "qoder", "minimaxcode", "globalcopilot"} <= set(su.LOCAL_SITE_MODULES))
check("三个旧站点登记为永久移除",
      {"acfun", "aolaxing", "baidu"} == set(su.REMOVED_SITE_MODULES))
check("比对时忽略本地与已移除站点",
      su.upstream_comparable({"ACFUN": "A", "TRAE": "T", "BILIBILI": "B"})
      == {"BILIBILI": "B"})
tmp = tempfile.mkdtemp(prefix="ai-sync-")
old_engine = su.ENGINE_DIR
try:
    os.makedirs(os.path.join(tmp, "acfun"))
    os.makedirs(os.path.join(tmp, "baidu"))
    su.ENGINE_DIR = tmp
    check("同步落地后删掉已移除站点",
          su.prune_removed_modules() == ["acfun", "baidu"]
          and not os.path.isdir(os.path.join(tmp, "acfun")))
finally:
    su.ENGINE_DIR = old_engine

print("\n" + "=" * 68)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)
sys.exit(1 if failed else 0)
