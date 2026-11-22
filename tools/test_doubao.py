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

import doubao_spec

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
check("面板校验与引擎共用同一份设备号和接口常量",
      verify.doubao_ids({"seed": "seed-A"}) == s1
      and verify.DOUBAO_DETAIL == doubao_spec.DETAIL_URL
      and verify.DOUBAO_PARAMS == doubao_spec.COMMON_PARAMS,
      str(verify.doubao_ids({"seed": "seed-A"})))
with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
    _dockerfile = f.read()
with open(os.path.join(ROOT, "setup.py"), encoding="utf-8") as f:
    _setup_src = f.read()
check("镜像构建会把共享规格模块装进 site-packages",
      "COPY doubao_spec.py ./" in _dockerfile and 'py_modules=["doubao_spec"]' in _setup_src,
      "Dockerfile/setup.py 缺少 doubao_spec 声明")
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
section("5b) 手机稍晚点确认：二维码过期后仍能拿到登录态")
sid2 = "late-confirm-session"
old_at = __import__("time").time() - 90  # 已超过二维码 55 秒 TTL，但没到 180 秒轮询窗口
doubao_auth._SESSIONS[sid2] = {
    "id": sid2,
    "cookies": {"ttwid": "t", "sessionid": "late-sid", "passport_csrf_token": "csrf"},
    "csrf": "csrf", "token": "tok", "qrcode": "data:image/png;base64,AAAA",
    "ttl": 55, "created": old_at, "qr_at": old_at, "status": "scanned",
    "seed": "late-seed", "state": "scanned",
}
original_get = doubao_auth._get
try:
    def late_get(url, cookies, headers=None, follow=False, timeout=15):
        if "check_qrconnect" in url:
            return 200, json.dumps({"data": {"error_code": 0, "status": "confirmed"}}), {}
        return 200, "{}", {}

    doubao_auth._get = late_get
    out = doubao_auth.status(sid2)
    check("超过二维码 TTL 后手机确认，仍能导出登录态",
          out["status"] == "confirmed" and "sessionid=late-sid" in out.get("cookie", ""), str(out))
finally:
    doubao_auth._get = original_get
    doubao_auth._SESSIONS.pop(sid2, None)

# 超过整个轮询窗口才真正判过期
sid3 = "timeout-session"
too_old = __import__("time").time() - (doubao_auth.POLL_WINDOW + 5)
doubao_auth._SESSIONS[sid3] = {
    "id": sid3,
    "cookies": {"ttwid": "t"}, "csrf": "csrf", "token": "tok",
    "qrcode": "data:image/png;base64,AAAA", "ttl": 55,
    "created": too_old, "qr_at": too_old, "status": "waiting",
    "seed": "timeout-seed", "state": "new",
}
out = doubao_auth.status(sid3)
check("超过整个轮询窗口才判超时", out["status"] == "expired", str(out))
doubao_auth._SESSIONS.pop(sid3, None)


# --------------------------------------------------------------------------- #
section("5c) redirect 跟进失败不否定已确认的登录态")
sid4 = "redirect-fail-session"
now4 = __import__("time").time()
doubao_auth._SESSIONS[sid4] = {
    "id": sid4,
    "cookies": {"ttwid": "t", "sessionid": "redirect-sid", "passport_csrf_token": "csrf"},
    "csrf": "csrf", "token": "tok", "qrcode": "data:image/png;base64,AAAA",
    "ttl": 55, "created": now4, "qr_at": now4, "status": "waiting",
    "seed": "redirect-seed", "state": "new",
}
original_get = doubao_auth._get
try:
    def broken_redirect(url, cookies, headers=None, follow=False, timeout=15):
        if "check_qrconnect" in url:
            return 200, json.dumps({
                "data": {"error_code": 0, "status": "confirmed",
                         "redirect_url": "https://example.invalid/cb"},
            }), {}
        if "example.invalid" in url:
            return 0, "network down", {}
        return 200, "{}", {}

    doubao_auth._get = broken_redirect
    out = doubao_auth.status(sid4)
    check("redirect 跟进失败但已有 sessionid，仍算登录成功",
          out["status"] == "confirmed" and "sessionid=redirect-sid" in out.get("cookie", ""), str(out))
finally:
    doubao_auth._get = original_get
    doubao_auth._SESSIONS.pop(sid4, None)


# --------------------------------------------------------------------------- #
section("5d) 确认态不可逆：迟到的 scanned 不能把状态回退")
sid5 = "no-regress-session"
now5 = __import__("time").time()
doubao_auth._SESSIONS[sid5] = {
    "id": sid5,
    "cookies": {"ttwid": "t", "sessionid": "stable-sid", "passport_csrf_token": "csrf"},
    "csrf": "csrf", "token": "tok", "qrcode": "data:image/png;base64,AAAA",
    "ttl": 55, "created": now5, "qr_at": now5, "status": "confirmed",
    "seed": "stable-seed", "state": "confirmed",
    "cookie": "sessionid=stable-sid;",
}
original_get = doubao_auth._get
try:
    def stale_scan(url, cookies, headers=None, follow=False, timeout=15):
        return 200, json.dumps({"data": {"error_code": 0, "status": "scanned"}}), {}

    doubao_auth._get = stale_scan
    out = doubao_auth.status(sid5)
    check("确认后迟到的 scanned 不会回退状态",
          out["status"] == "confirmed" and "sessionid=stable-sid" in out.get("cookie", ""), str(out))
finally:
    doubao_auth._get = original_get
    doubao_auth._SESSIONS.pop(sid5, None)


# --------------------------------------------------------------------------- #
section("5e) 手机确认的多种 passport 表达都能识别（面板按钮不再卡灰）")

def _fresh_session(**kw):
    sid = "confirm-variants-" + str(kw.get("tag", "x"))
    now = __import__("time").time()
    doubao_auth._SESSIONS[sid] = {
        "id": sid,
        # 关键：扫码初始 Cookie 不带 sessionid，模拟真实起点
        "cookies": {"ttwid": "t", "passport_csrf_token": "csrf"},
        "csrf": "csrf", "token": "tok", "qrcode": "data:image/png;base64,AAAA",
        "ttl": 55, "created": now, "qr_at": now, "status": kw.get("status", "scanned"),
        "seed": "variant-seed", "state": kw.get("state", "scanned"),
    }
    return sid

original_get = doubao_auth._get
try:
    # 变体 1：status 仍 scanned，但本次响应新下发 sessionid
    sid = _fresh_session(tag="sid")
    def fresh_sid_get(url, cookies, headers=None, follow=False, timeout=15):
        return 200, json.dumps({"data": {"error_code": 0, "status": "scanned"}}), {"sessionid": "just-issued"}
    doubao_auth._get = fresh_sid_get
    out = doubao_auth.status(sid)
    check("scanned 但新下发 sessionid 也算已确认",
          out["status"] == "confirmed" and "sessionid=just-issued" in out.get("cookie", ""), str(out))
    check("响应带原始诊断字段 debug",
          out.get("debug", {}).get("sessionid") is True, str(out.get("debug")))
    doubao_auth._SESSIONS.pop(sid, None)

    # 变体 2：status 是 confirm 变体，没有 sessionid 也不卡死
    sid = _fresh_session(tag="variant")
    def variant_get(url, cookies, headers=None, follow=False, timeout=15):
        return 200, json.dumps({"data": {"error_code": 0, "status": "confirm_v2"}}), {}
    doubao_auth._get = variant_get
    out = doubao_auth.status(sid)
    check("status=confirm_v2 变体也算已确认", out["status"] == "confirmed", str(out))
    check("无 sessionid 仍给出可用结果与诊断",
          "sessionid" in out.get("cookie", "") or "未下发" in out.get("message", ""), str(out))
    doubao_auth._SESSIONS.pop(sid, None)

    # 变体 3：只给 redirect_url
    sid = _fresh_session(tag="redirect")
    def redirect_get(url, cookies, headers=None, follow=False, timeout=15):
        if "check_qrconnect" in url:
            return 200, json.dumps({"data": {"error_code": 0, "status": "new",
                                             "redirect_url": "https://www.doubao.com/landing"}}), {}
        return 200, "{}", {"sessionid": "from-redirect"}
    doubao_auth._get = redirect_get
    out = doubao_auth.status(sid)
    check("只给 redirect_url 也算已确认", out["status"] == "confirmed", str(out))
    doubao_auth._SESSIONS.pop(sid, None)
finally:
    doubao_auth._get = original_get


# --------------------------------------------------------------------------- #
section("5f) 2046 风控二次验证：不卡死、自动续等、验证后仍能确认")

def _risk_session(sid, status="waiting", verify_at=None):
    now = __import__("time").time()
    doubao_auth._SESSIONS[sid] = {
        "id": sid,
        "cookies": {"ttwid": "t", "s_v_web_id": "verify_fp_123", "biz_trace_id": "abcd1234",
                    "passport_csrf_token": "csrf"},
        "csrf": "csrf", "token": "tok", "qrcode": "data:image/png;base64,AAAA",
        "ttl": 55, "created": now, "qr_at": now, "status": status,
        "seed": "risk-seed", "state": "new", "verify_at": verify_at,
    }


sid6 = "risk-verify-session"
_risk_session(sid6)
original_get = doubao_auth._get
seen_urls = []
try:
    def risk_get(url, cookies, headers=None, follow=False, timeout=15):
        seen_urls.append(url)
        return 200, json.dumps({"data": {
            "error_code": 2046,
            "description": "为保证账号安全，暂不支持此操作",
        }}), {}

    doubao_auth._get = risk_get
    out = doubao_auth.status(sid6)
    check("2046 被识别为 verify 而不是失败", out["status"] == "verify", str(out))
    check("2046 提示包含错误码和可操作指引",
          "2046" in out.get("message", "") and ("安全验证" in out.get("message", "") or "手动填 Cookie" in out.get("message", "")),
          out.get("message", ""))
    check("2046 把等待窗口放宽到 10 分钟", 0 < out.get("expires_in", 0) <= doubao_auth.VERIFY_WINDOW, str(out))
    check("风控响应也带原始诊断字段", out.get("debug", {}).get("code") == 2046, str(out.get("debug")))
    check("passport 请求带浏览器风格 fp/verifyFp 参数",
          bool(seen_urls) and "fp=verify_fp_123" in seen_urls[0] and "verifyFp=verify_fp_123" in seen_urls[0],
          str(seen_urls[:1]))

    def ok_get(url, cookies, headers=None, follow=False, timeout=15):
        return 200, json.dumps({"data": {"error_code": 0, "status": "confirmed"}}), {}

    doubao_auth._get = ok_get
    out = doubao_auth.status(sid6)
    check("完成安全验证后同一张码仍能确认登录", out["status"] == "confirmed", str(out))
finally:
    doubao_auth._get = original_get
    doubao_auth._SESSIONS.pop(sid6, None)

# 超过 10 分钟 verification 窗口才判超时，且不再发无谓请求
sid7 = "risk-verify-timeout"
_risk_session(sid7, status="verify",
              verify_at=__import__("time").time() - (doubao_auth.VERIFY_WINDOW + 5))
doubao_auth._get = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("超时会话不应再请求 passport"))
try:
    out = doubao_auth.status(sid7)
    check("风控等待超过 10 分钟才判超时", out["status"] == "expired", str(out))
finally:
    doubao_auth._get = original_get
    doubao_auth._SESSIONS.pop(sid7, None)


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
section("7) 浏览器扫码登录：CDP 直接读二维码与登录 Cookie")
import base64 as _b64
import time as _time

from panel import doubao_browser as dbb


class _FakeCdp:
    def __init__(self, qr, cookies=None):
        self.qr, self.cookies, self.closed = qr, cookies or [], False

    def js(self, expr):
        return self.qr

    def browser(self, method, params=None):
        if method == "Storage.getCookies":
            return {"cookies": self.cookies}
        if method == "Browser.close":
            self.closed = True
        return {}

    def page(self, method, params=None):
        return {}

    def close(self):
        pass


qr_png = _b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()
qr_uri = dbb._PNG_URI + qr_png
check("二维码从 CSS 背景图取到并补成可显示的 data URI",
      dbb._qrcode(_FakeCdp(qr_png)) == qr_uri, dbb._qrcode(_FakeCdp(qr_png))[:40])
check("已是 data URI 的二维码不被重复加前缀",
      dbb._qr_uri(qr_uri) == qr_uri)
check("带空白的裸 base64 也能规整成 data URI",
      dbb._qr_uri("  " + qr_png[:8] + "\n" + qr_png[8:] + "  ") == qr_uri)

class _ShotCdp(_FakeCdp):
    def __init__(self):
        super().__init__("")
        self.calls = []
    def js(self, expr):
        if "getBoundingClientRect" in expr:
            return json.dumps({"x": 10, "y": 20, "width": 162, "height": 162})
        return ""
    def page(self, method, params=None):
        self.calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {"data": qr_png}
        return {}

shot = _ShotCdp()
check("截图兜底能拿到真实的 qrcode_image 元素",
      dbb._qrcode(shot) == qr_uri
      and any(m == "Page.captureScreenshot" for m, _ in shot.calls))
check("截图只裁二维码区域并放大 2 倍",
      next(p for m, p in shot.calls if m == "Page.captureScreenshot")["clip"]["width"] == 162
      and next(p for m, p in shot.calls if m == "Page.captureScreenshot")["clip"]["scale"] == 2)
check("截图定位只认二维码图形并排除成功页文字",
      "svg,canvas,img[src^=" in dbb._QR_RECT_JS and "(e.innerText||'').trim()" in dbb._QR_RECT_JS)

class _CanvasCdp(_FakeCdp):
    def __init__(self, value):
        super().__init__("")
        self.value, self.calls = value, []
    def page(self, method, params=None):
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": self.value}}
        return {}

canvas = _CanvasCdp(qr_uri)
check("首次加载优先用 DOM 里的 SVG 转 PNG，避免截到浮层文字",
      dbb._qrcode(canvas) == qr_uri
      and not any(m == "Page.captureScreenshot" for m, _ in canvas.calls))
check("SVG 转 PNG 用 awaitPromise 等待图片解码",
      canvas.calls[0][1].get("awaitPromise") is True
      and "XMLSerializer" in canvas.calls[0][1].get("expression", ""))
check("对勾等小图形被尺寸守卫挡下，不生成二维码",
      "r.width<100||r.height<100" in dbb._QR_CANVAS_JS)

class _DoneCdp(_ShotCdp):
    def js(self, expr):
        # 手机确认后页面换成「圆形对勾 + 登录成功」，没有二维码图形可截。
        return ""

done = _DoneCdp()
check("确认页不再截图带方框文字的对勾",
      dbb._qrcode(done) == ""
      and not any(m == "Page.captureScreenshot" for m, _ in done.calls), str(done.calls))

class _SmallCdp(_ShotCdp):
    def js(self, expr):
        if "getBoundingClientRect" in expr:
            return json.dumps({"x": 10, "y": 20, "width": 48, "height": 48})
        return ""

small = _SmallCdp()
check("几十像素的成功对勾不会被当成二维码截图",
      dbb._qrcode(small) == ""
      and not any(m == "Page.captureScreenshot" for m, _ in small.calls), str(small.calls))
svg = '<svg width="162" height="162"><rect fill="#000000"/></svg>'
svg_uri = dbb._qr_uri(json.dumps({"mime": "image/svg+xml", "data": svg}))
check("SVG 兜底补命名空间再编码成 data URI",
      svg_uri.startswith(dbb._SVG_URI)
      and 'xmlns="http://www.w3.org/2000/svg"'
      in _b64.b64decode(svg_uri.split(",", 1)[1]).decode("utf-8"))
check("登录按钮优先用官方 data-testid 点击", "to_login_button" in dbb._LOGIN_JS)

only = dbb._cookies(_FakeCdp("", [
    {"name": "ttwid", "value": "T", "domain": ".doubao.com"},
    {"name": "sessionid", "value": "S", "domain": "www.doubao.com"},
    {"name": "other", "value": "O", "domain": "example.com"},
]))
check("CDP Cookie 只收豆包域（含 HttpOnly 会话）",
      only == {"ttwid": "T", "sessionid": "S"}, str(only))

old_ws = dbb.websocket
dbb.websocket = None
ok, why = dbb.available()
check("缺少 CDP 客户端时明确不可用", not ok and "依赖" in why, why)
dbb.websocket = old_ws

sid = "browser-test-session"
now = _time.time()
dbb._SESSION = {
    "id": sid, "cdp": _FakeCdp(qr_png, [
        {"name": "sessionid", "value": "SID-X", "domain": ".doubao.com"},
        {"name": "ttwid", "value": "T", "domain": ".doubao.com"},
    ]),
    "proc": None, "profile": "/tmp/not-used", "seed": "browser-seed",
    "qrcode": qr_uri, "qr_at": now, "started_at": now,
    "status": "waiting", "cookie": "", "cookies": {},
}
try:
    r = dbb.status(sid)
    check("读到 sessionid 后判为登录成功", r["status"] == "confirmed", str(r)[:160])
    check("浏览器通道产出可直接落盘的 cookie",
          "sessionid=SID-X" in r.get("cookie", ""), r.get("cookie", "")[:120])
    check("浏览器通道保留独立 seed", r.get("seed") == "browser-seed")
    check("浏览器通道标记 mode=browser 供前端区分", r.get("mode") == "browser")
finally:
    dbb._SESSION = None

with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
    image = f.read()
with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
    deps = f.read()
with open(os.path.join(ROOT, "panel/static/app.js"), encoding="utf-8") as f:
    ui = f.read()
check("镜像不再内置浏览器（首启下载到持久化卷）",
      "chromium" not in image and "browser_bootstrap" in image)
check("依赖包含 CDP 客户端", "websocket-client" in deps)
check("前端先试浏览器通道再回退 passport",
      "/doubao/browser/start" in ui and "/doubao/qr/start" in ui)
check("前端对裸 base64 二维码兜底补 data 前缀",
      "function qrSrc" in ui and '"data:image/png;base64," + s.replace' in ui)
check("前端显示浏览器通道失败原因", "QR.channelNote" in ui)
check("二维码图片加载失败时给出可操作的提示",
      "addEventListener(\"error\"" in ui and "换一张" in ui)
with open(os.path.join(ROOT, "panel", "static", "style.css"), encoding="utf-8") as f:
    ui_css = f.read()
check("手机确认后用面板自绘成功态，不沿用无头浏览器截图",
      "function paintQrDone" in ui and 'paintQrDone("登录成功"' in ui
      and ".qr-done-mark::after" in ui_css)
check("确认分支先画成功态再保存",
      ui.index('if (r.status === "confirmed")') < ui.index('paintQrDone("登录成功"'))
check("成功对勾用 CSS 边框画，不依赖字体字形",
      "border-width: 0 3px 3px 0" in ui_css and "transform: rotate(45deg)" in ui_css)
check("手动粘贴 Cookie 入口保留", "手动粘贴 cookie" in ui)


# --------------------------------------------------------------------------- #
section("13) 浏览器首次下载到持久卷：下载 / 幂等 / 强制刷新 / 失败降级")
import hashlib
import stat
import zipfile
from pathlib import Path
from panel import browser_bootstrap as bb

bb_tmp = tempfile.mkdtemp(prefix="dc-browser-test-")
os.environ["DC_BROWSER_DIR"] = bb_tmp
# CI runner 自带 google-chrome；指一个不存在的路径，强制走「下载到持久卷」这条分支。
os.environ["DOUBAO_BROWSER"] = "/nonexistent/doubao-browser-test"
_real_platform = bb.platform_key
_real_fetch = bb._fetch_zip
bb.platform_key = lambda: "linux64"
bin_name = bb._BIN
try:
    check("持久目录落在 data 卷下而不是镜像层",
          "/dailycheckin/data/browser" in Path(os.path.join("/dailycheckin", "data", "browser")).as_posix())
    check("固定版本带 SHA256 校验值",
          len(bb._CHECKSUMS.get("linux64", "")) == 64 and len(bb._CHECKSUMS.get("linux-arm64", "")) == 64)
    check("镜像源含国内 npmmirror 与官方回退",
          any("npmmirror" in u for u in bb._MIRRORS) and any("googleapis" in u for u in bb._MIRRORS))

    fake_zip = os.path.join(bb_tmp, "fake.zip")
    with zipfile.ZipFile(fake_zip, "w") as zf:
        zf.writestr(f"chrome-headless-shell-linux64/{bin_name}", "#!/bin/sh\necho fake\n")

    seen_work = []
    def fake_fetch(p, v, w, log):
        seen_work.append(str(w))
        return Path(fake_zip)
    bb._fetch_zip = fake_fetch
    ok1, msg1 = bb.ensure(log=lambda m: None)
    installed = bb.installed_path()
    check("首次 ensure 下载并落盘", ok1 and installed is not None, msg1)
    check("下载临时目录也落在自定义持久目录（不会写进 root 挂载的 data 卷）",
          bool(seen_work) and seen_work[0].startswith(bb_tmp), str(seen_work[:1]))
    check("下载后二进制带可执行位",
          bool(installed) and bool(installed.stat().st_mode & stat.S_IXUSR))
    check("浏览器路径落在自定义持久目录",
          bool(installed) and installed.as_posix().startswith(bb_tmp))

    calls = []
    def should_not_download(*a, **k):
        calls.append(1)
        raise AssertionError("不该重复下载")
    bb._fetch_zip = should_not_download
    ok2, msg2 = bb.ensure(log=lambda m: None)
    check("第二次 ensure 复用已下载的浏览器", ok2 and not calls, msg2)

    bb._fetch_zip = lambda p, v, w, log: Path(fake_zip)
    ok3, _ = bb.ensure(log=lambda m: None, force=True)
    check("force=True 才重新下载", ok3 and bb.installed_path() is not None)

    bb._fetch_zip = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("网络失败"))
    ok4, msg4 = bb.ensure(log=lambda m: None, force=True)
    check("下载失败返回 False 而不抛异常", ok4 is False and "网络失败" in msg4)
    check("下载失败后仍保留上一次的浏览器", bb.installed_path() is not None)

    # 自动发现：browser_path 找到持久卷里的浏览器
    os.environ.pop("DOUBAO_BROWSER", None)
    from panel import doubao_browser as dbb2
    check("浏览器通道能自动发现持久卷里的 shell",
          dbb2.browser_path() == str(bb.installed_path()))

    # CLI 入口恒返回 0（不阻塞面板）
    rc = bb.main()
    check("引导 CLI 失败也返回 0（不阻塞启动）", rc == 0)
finally:
    bb.platform_key = _real_platform
    bb._fetch_zip = _real_fetch
    os.environ.pop("DC_BROWSER_DIR", None)
    import shutil
    shutil.rmtree(bb_tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 中文乱码清洗：手机确认后弹窗/校验文案不能出现方块或 latin-1 mojibake
# --------------------------------------------------------------------------- #
moji = "登录成功".encode("utf-8").decode("latin-1")  # UTF-8 字节被按 latin-1 误读
check("latin-1 mojibake 还原成汉字", doubao_spec.tidy_text(moji) == "登录成功",
      doubao_spec.tidy_text(moji))
check("正常中文不被误改", doubao_spec.tidy_text("登录成功，点「保存」生效") == "登录成功，点「保存」生效")
check("纯英文不被误改", doubao_spec.tidy_text("confirmed / sessionid") == "confirmed / sessionid")
check("含替换符的文本保持原样", doubao_spec.tidy_text("登录\ufffd成功") == "登录\ufffd成功")
check("空值安全", doubao_spec.tidy_text(None) == "" and doubao_spec.tidy_text("") == "")

import gzip as _gzip
from email.message import Message as _Message

_hdr = _Message()
_hdr["Content-Encoding"] = "gzip"
_gz = _gzip.compress("登录成功，点「保存」或「续期」生效".encode("utf-8"))
check("gzip 响应体解压成可读中文",
      doubao_auth._body(_gz, _hdr) == "登录成功，点「保存」或「续期」生效",
      doubao_auth._body(_gz, _hdr))
check("非 gzip 的 UTF-8 响应体正常解码",
      doubao_auth._body("登录成功".encode("utf-8"), None) == "登录成功")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 68)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)
