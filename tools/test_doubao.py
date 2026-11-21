#!/usr/bin/env python3
"""豆包签到 / 扫码登录离线自测（不联网、不发任何真实请求）。

跑法：python3 tools/test_doubao.py
"""
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dailycheckin.doubao import main as engine
from panel import catalog, doubao_auth, verify

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f" :: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)


# --------------------------------------------------------------------------- #
section("1) 多账号隔离：不同账号绝不共用设备号")
a = engine.device_ids(engine.account_seed({"cookie": "sessionid=AAA"}))
b = engine.device_ids(engine.account_seed({"cookie": "sessionid=BBB"}))
check("两个不同 Cookie 的 web_id 不同", a["web_id"] != b["web_id"], f"{a} / {b}")
check("两个不同 Cookie 的 tea_uuid 不同", a["tea_uuid"] != b["tea_uuid"])
check("没有 seed 时用 Cookie 哈希兜底", len(engine.account_seed({"cookie": "x=1"})) == 32)

s1 = engine.device_ids("seed-A")
check("同 seed 派生结果稳定", s1 == engine.device_ids("seed-A"))
check("不同 seed 派生结果不同", s1 != engine.device_ids("seed-B"))

check("手动指定 seed 优先于 Cookie", engine.account_seed({"cookie": "x=1", "seed": "mine"}) == "mine")
url = engine.api_url(engine.DETAIL_URL, "seed-A")
check("接口 URL 带 aid / scene 所需公共参数", "aid=497858" in url and "web_id=" in url, url)
check("设备号不同 → URL 不同", engine.api_url(engine.DETAIL_URL, "seed-B") != url)


# --------------------------------------------------------------------------- #
section("2) Cookie 解析保留值里的 =")
parsed = engine.parse_cookie("sessionid=abc==; ttwid=x=y; msToken=ok")
check("sessionid 的 == 没被截断", parsed.get("sessionid") == "abc==", str(parsed))
check("普通等号值也没被截断", parsed.get("ttwid") == "x=y", str(parsed))


# --------------------------------------------------------------------------- #
section("2b) 账号级并发锁：同一账号串行，不同账号并行")
check("不同账号的锁文件不同", engine.lock_path("seed-A") != engine.lock_path("seed-B"))
check("同一账号的锁文件稳定", engine.lock_path("seed-A") == engine.lock_path("seed-A"))
if engine.fcntl is not None:
    result = []

    def _try_lock(name: str, timeout: float) -> None:
        try:
            with engine.account_lock(name, timeout=timeout):
                result.append("acquired")
        except ValueError:
            result.append("timeout")

    with engine.account_lock("lock-test", timeout=2):
        t = threading.Thread(target=_try_lock, args=("lock-test", 0.3))
        t.start()
        t.join(3)
    check("同一账号被锁住时不会并发进入", result == ["timeout"], str(result))

    result.clear()
    with engine.account_lock("lock-test", timeout=2):
        t = threading.Thread(target=_try_lock, args=("lock-other", 0.3))
        t.start()
        t.join(3)
    check("不同账号互不阻塞", result == ["acquired"], str(result))
else:  # pragma: no cover - 非 POSIX 平台只做降级验证
    check("无 fcntl 时降级为无锁但不报错", bool(engine.account_lock("x")))


# --------------------------------------------------------------------------- #
section("3) 签到分支：已签 / 首次领取 / 幂等 / 错误码")
DETAIL_OK = {
    "code": 0,
    "data": {
        "tasks": [{
            "task_kind": "daily_signin",
            "daily_checkin": {
                "checked_in_today": False,
                "continuous_days": 3,
                "reward": {"reward_amount": 200},
            },
        }],
        "progress": {"total_amount": 1500},
    },
}
CHECKIN_OK = {
    "code": 0,
    "data": {"checked_in": True, "continuous_days": 4, "reward_amount": 200, "idempotent": False},
}


def make_doubao(replies):
    one = engine.Doubao({"cookie": "sessionid=AAA", "seed": "test-seed"})
    calls = []

    def fake_post(url, body):
        calls.append((url, body))
        item = replies[len(calls) - 1] if len(calls) <= len(replies) else replies[-1]
        if isinstance(item, Exception):
            raise item
        return item

    one._post = fake_post
    return one, calls


one, calls = make_doubao([DETAIL_OK, CHECKIN_OK])
r = one.checkin()
check("未签到时先查 detail 再领取", len(calls) == 2 and "landing/checkin" in calls[1][0], str(calls))
check("领取成功返回 +200", r["signed"] is True and r["credit"] == 200, str(r))
check("累计额度按领取后增加", r["state"]["total_amount"] == 1700, str(r))
one.run = lambda: r
text = one.main()
check("输出里带额度与连签天数", "累计额度: 1700 点" in text and "连续签到: 4 天" in text, text)

ALREADY = dict(DETAIL_OK)
ALREADY["data"] = json.loads(json.dumps(DETAIL_OK["data"]))
ALREADY["data"]["tasks"][0]["daily_checkin"]["checked_in_today"] = True
one, calls = make_doubao([ALREADY])
r = one.checkin()
check("今日已签时不再打写接口", len(calls) == 1 and r["already"] is True, str(calls))

IDEMPOTENT = dict(CHECKIN_OK)
IDEMPOTENT["data"] = {**CHECKIN_OK["data"], "idempotent": True}
one, _ = make_doubao([DETAIL_OK, IDEMPOTENT])
r = one.checkin()
check("服务端幂等返回按 already 处理", r["already"] is True and r["signed"] is False, str(r))

one, _ = make_doubao([{"code": 710012001, "msg": "login expired"}])
try:
    one.checkin()
    check("登录失效码抛出明确错误", False)
except ValueError as exc:
    check("登录失效码抛出明确错误", "过期" in str(exc), str(exc))

one, _ = make_doubao([{"code": 710010702, "msg": "system error"}])
try:
    one.checkin()
    check("无签到活动码不误判成 cookie 失效", False)
except ValueError as exc:
    check("无签到活动码不误判成 cookie 失效", "没有" in str(exc) and "过期" not in str(exc), str(exc))


# --------------------------------------------------------------------------- #
section("4) 在线校验：显示积分 / 判失效")
original_http = verify.http
try:
    resp = verify.Resp(200, json.dumps(DETAIL_OK), engine.DETAIL_URL)
    verify.http = lambda *a, **kw: resp
    out = verify.verify_account("DOUBAO", {"cookie": "sessionid=AAA"})
    check("校验返回有效", out["ok"] is True, str(out))
    check("校验文案带累计额度", "累计 1500 额度" in out["message"], out["message"])
    check("校验文案带今日可领额度", "今日可领 +200" in out["message"], out["message"])

    resp = verify.Resp(200, json.dumps({"code": 710012001}), engine.DETAIL_URL)
    verify.http = lambda *a, **kw: resp
    out = verify.verify_account("DOUBAO", {"cookie": "x"})
    check("登录失效码判定为失效", out["ok"] is False, str(out))
finally:
    verify.http = original_http


# --------------------------------------------------------------------------- #
section("5) 扫码状态机与 cookie 导出（全部用假响应）")
check("授权入口指向 chat 页而不是不存在的 login 页",
      doubao_auth.AUTH_URL == "https://www.doubao.com/chat/", doubao_auth.AUTH_URL)
check("ICON 指向官网 head 声明的官方 CDN PNG，而不是会返回 HTML 的 /favicon.ico",
      catalog.SITE_LINKS["DOUBAO"]["logo"]
      == "https://lf-flow-web-cdn.doubao.com/obj/flow-doubao/favicon/new-doubao/64x64.png",
      catalog.SITE_LINKS["DOUBAO"]["logo"])

# error_code=0 是合法值：曾经被 `0 or -1` 误判成获取失败，NAS 上因此拿不到二维码。
_original_qr_get = doubao_auth._get
try:
    doubao_auth._get = lambda *a, **kw: (
        200,
        json.dumps({"data": {"error_code": 0, "token": "qr-token",
                             "qrcode": "data:image/png;base64,AAAA",
                             "expire_time": 1795246599}}),
        {},
    )
    token, uri, ttl = doubao_auth._qrcode({}, "csrf")
    check("error_code=0 的二维码响应被正确解析", token == "qr-token" and uri.startswith("data:image/png"), f"{token} {uri[:30]}")
    check("绝对时间戳过期时间回落到默认 TTL", ttl == doubao_auth.QR_TTL, str(ttl))

    doubao_auth._get = lambda *a, **kw: (
        200, json.dumps({"data": {"error_code": 3, "description": "缺少参数"}}), {}
    )
    try:
        doubao_auth._qrcode({}, "csrf")
        check("真实错误码仍会被抛出", False)
    except ValueError as exc:
        check("真实错误码仍会被抛出", "缺少参数" in str(exc), str(exc))
finally:
    doubao_auth._get = _original_qr_get

sid = "fake-session"
qr_at = __import__("time").time()
doubao_auth._SESSIONS[sid] = {
    "id": sid,
    "cookies": {"ttwid": "t", "sessionid": "sid-value", "passport_csrf_token": "csrf"},
    "csrf": "csrf",
    "token": "tok",
    "qrcode": "data:image/png;base64,AAAA",
    "ttl": 55,
    "created": qr_at,
    "qr_at": qr_at,
    "status": "waiting",
    "seed": "seed-xyz",
    "state": "new",
}
original_get = doubao_auth._get
try:
    def fake_get(url, cookies, headers=None, follow=False, timeout=15):
        if "check_qrconnect" in url:
            state = fake_get.state
            return 200, json.dumps({"data": {"error_code": 0, "status": state}}), {}
        return 200, "{}", {}

    fake_get.state = "scanned"
    doubao_auth._get = fake_get
    out = doubao_auth.status(sid)
    check("scanned 状态被识别", out["status"] == "scanned", str(out))

    fake_get.state = "confirmed"
    out = doubao_auth.status(sid)
    check("confirmed 状态导出 cookie", out["status"] == "confirmed" and "sessionid=sid-value" in out["cookie"], str(out))
    check("确认后返回独立 seed", out["seed"] == "seed-xyz", str(out))
    check("cookie 是引擎需要的 '; ' 分隔且带结尾分号", out["cookie"].endswith(";") and "; " in out["cookie"], out["cookie"])

    out2 = doubao_auth.status(sid)
    check("确认结果可重复读取（防前端漏拿）", out2["status"] == "confirmed", str(out2))
finally:
    doubao_auth._get = original_get
    doubao_auth._SESSIONS.pop(sid, None)


# --------------------------------------------------------------------------- #
section("6) 续期只动目标账号，其它账号原样保留")
from panel import paths, runner, store

with tempfile.TemporaryDirectory(prefix="dc-doubao-test-") as tmp:
    old_config, old_status = paths.CONFIG_PATH, paths.STATUS_PATH
    paths.CONFIG_PATH = os.path.join(tmp, "config.json")
    paths.STATUS_PATH = os.path.join(tmp, "status.json")
    try:
        store.write({
            "DOUBAO": [
                {"cookie": "sessionid=KEEP", "seed": "keep", "proxy": "http://127.0.0.1:1"},
                {"cookie": "sessionid=OLD", "seed": "old"},
            ],
            "BILIBILI": [{"cookie": "SESSDATA=untouched"}],
        }, backup=False)
        sid = "renew-session"
        doubao_auth._SESSIONS[sid] = {
            "id": sid, "cookies": {}, "csrf": "", "token": "", "qrcode": "",
            "ttl": 55, "created": qr_at, "qr_at": qr_at, "status": "confirmed",
            "cookie": "sessionid=NEW; ttwid=new;", "seed": "new-seed", "state": "confirmed",
        }
        out = doubao_auth.apply_cookie(sid, 1)
        data = store.load()
        acc = data["DOUBAO"]
        check("续期作用在第 2 个账号", out["where"] == "第 2 个账号续期", str(out))
        check("第 1 个账号的 cookie / seed / proxy 全保留",
              acc[0] == {"cookie": "sessionid=KEEP", "seed": "keep", "proxy": "http://127.0.0.1:1"}, str(acc[0]))
        check("第 2 个账号换成了新 cookie", acc[1]["cookie"] == "sessionid=NEW; ttwid=new;", str(acc[1]))
        check("第 2 个账号换了独立 seed", acc[1]["seed"] == "new-seed", str(acc[1]))
        check("其它站点不受影响", data["BILIBILI"] == [{"cookie": "SESSDATA=untouched"}], str(data["BILIBILI"]))

        doubao_auth._SESSIONS["add-session"] = {
            "id": "add-session", "cookies": {}, "csrf": "", "token": "", "qrcode": "",
            "ttl": 55, "created": qr_at, "qr_at": qr_at, "status": "confirmed",
            "cookie": "sessionid=THIRD;", "seed": "third-seed", "state": "confirmed",
        }
        doubao_auth.apply_cookie("add-session", None)
        acc = store.accounts(store.load(), "DOUBAO")
        check("扫码新增账号不覆盖已有账号", len(acc) == 3 and acc[2]["cookie"] == "sessionid=THIRD;", str(acc))
    finally:
        paths.CONFIG_PATH, paths.STATUS_PATH = old_config, old_status
        doubao_auth._SESSIONS.clear()


# --------------------------------------------------------------------------- #
print("\n" + "=" * 68)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)
