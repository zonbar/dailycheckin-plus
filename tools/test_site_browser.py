#!/usr/bin/env python3
"""通用站点浏览器登录离线自测（不联网、不起浏览器）。

锁住几件容易回归的事：
1. 规则表齐全，且每条规则字段合法（登录 URL、凭据类型、二维码选择器）。
2. 阿里云盘是 localStorage 类型，从 token.refresh_token 取凭据（不是 cookie）。
3. cookie 类站点按域名收集、按 required 字段体检缺漏。
4. credential_of 产出「字段->值」，与站点凭据类型一致。
5. apply 新增/续期只动凭据字段，保留其它账号与可选参数。
6. 前端通用扫码入口已接好（弹窗、按钮、API 路径）。

跑法：python3 tools/test_site_browser.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from panel import catalog, site_browser as sb
from panel import paths, store

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f" :: {detail}" if not cond else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)


class FakeCdp:
    """够用的 CDP 替身：js 按表达式返回预设值，browser 返回预设 cookies。"""

    def __init__(self, js_values=None, cookies=None, shot=""):
        self.js_values = js_values or {}
        self.cookies = cookies or []
        self.shot = shot

    def js(self, expr):
        for key, value in self.js_values.items():
            if key in expr:
                return value
        return ""

    def browser(self, method, params=None):
        if method == "Storage.getCookies":
            return {"cookies": self.cookies}
        return {}

    def page(self, method, params=None):
        if method == "Page.captureScreenshot":
            return {"data": self.shot}
        return {}

    def close(self):
        pass


# --------------------------------------------------------------------------- #
section("1) 规则表结构")
check("支持站点不少于 8 个", len(sb.SUPPORTED) >= 8, str(sb.SUPPORTED))
check("阿里云盘在支持列表里", "ALIYUN" in sb.SUPPORTED)
check("豆包不在通用列表（它有专用通道）", "DOUBAO" not in sb.SUPPORTED)
for key, rule in sb.RULES.items():
    ok = bool(rule.get("login_url", "").startswith("https://"))
    ok = ok and rule.get("credential") in ("cookie", "localstorage")
    ok = ok and bool(rule.get("qr_selectors"))
    if rule.get("credential") == "localstorage":
        ok = ok and bool(rule.get("ls_key")) and bool(rule.get("ls_field")) and bool(rule.get("emit_key"))
    else:
        ok = ok and bool(rule.get("domain")) and bool(rule.get("ready_cookies"))
    check(f"{key} 规则字段合法", ok, str(rule)[:120])

# --------------------------------------------------------------------------- #
section("2) 阿里云盘：localStorage 凭据（用户点名的场景）")
ali = sb.site_rule("ALIYUN")
check("阿里云盘凭据类型是 localstorage", ali["credential"] == "localstorage")
check("从 token 的 refresh_token 字段取", ali["ls_key"] == "token" and ali["ls_field"] == "refresh_token")
check("回填字段是 refresh_token", ali["emit_key"] == "refresh_token")
check("登录页指向 alipan.com", "alipan.com" in ali["login_url"])
check("二维码选择器含 canvas", any("canvas" in s for s in ali["qr_selectors"]))

# --------------------------------------------------------------------------- #
section("3) 凭据提取：localStorage 与 cookie")
cdp_ls = FakeCdp(js_values={"localStorage.getItem": '{"refresh_token":"rt-abc","access_token":"at-x"}'})
val, obj = sb._localstorage(cdp_ls, "token", "refresh_token")
check("从 localStorage JSON 取出 refresh_token", val == "rt-abc", val)
check("完整对象也返回（便于排查）", obj.get("access_token") == "at-x")

# localStorage 值是裸字符串（非 JSON）时也能兜底
cdp_raw = FakeCdp(js_values={"localStorage.getItem": "rt-plain"})
val2, _ = sb._localstorage(cdp_raw, "token", "refresh_token")
check("裸字符串 token 也能取出", val2 == "rt-plain", val2)

cookie_list = [
    {"name": "SESSDATA", "value": "abc", "domain": ".bilibili.com"},
    {"name": "bili_jct", "value": "def", "domain": "bilibili.com"},
    {"name": "other", "value": "x", "domain": "example.com"},
]
cdp_ck = FakeCdp(cookies=cookie_list)
got = sb._cookies(cdp_ck, "bilibili.com")
check("cookie 按域名过滤，只留目标站点", set(got) == {"SESSDATA", "bili_jct"}, str(got))

# credential_of 产出正确字段
one_ali = {"site": "ALIYUN", "credential_value": "rt-1"}
check("阿里云盘产出 {refresh_token}", sb.credential_of(one_ali) == {"refresh_token": "rt-1"})
one_bili = {"site": "BILIBILI", "cookies": {"SESSDATA": "a", "bili_jct": "b"}}
cred_bili = sb.credential_of(one_bili)
check("B 站产出 {cookie}", set(cred_bili) == {"cookie"} and "SESSDATA=a" in cred_bili["cookie"], str(cred_bili))

# --------------------------------------------------------------------------- #
section("4) 二维码提取")
shot_b64 = "iVBORw0KGgoAAAANSUhEUg=="
cdp_qr = FakeCdp(js_values={"scrollIntoView": '{"x":10,"y":20,"width":160,"height":160}'}, shot=shot_b64)
qr = sb._qrcode(cdp_qr, [".qrcode canvas"])
check("截图返回 data:image/png data URI", qr == sb.PNG_URI + shot_b64, qr[:40])
cdp_noqr = FakeCdp()
check("找不到二维码时返回空", sb._qrcode(cdp_noqr, [".nope"]) == "")

# --------------------------------------------------------------------------- #
section("5) apply 落盘：新增与续期")
tmp = tempfile.mkdtemp(prefix="sb-test-")
old_home, old_config, old_data = paths.HOME, paths.CONFIG_PATH, paths.DATA_DIR
try:
    paths.HOME = tmp
    paths.CONFIG_PATH = os.path.join(tmp, "config", "config.json")
    os.makedirs(os.path.dirname(paths.CONFIG_PATH), exist_ok=True)
    paths.DATA_DIR = os.path.join(tmp, "data")
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("{}")

    # 造一个已确认的阿里云盘会话
    sb._SESSIONS.clear()
    sb._SESSIONS["sid1"] = {
        "id": "sid1", "site": "ALIYUN", "confirmed": True,
        "credential_value": "rt-new", "cookies": {},
    }
    r = sb.apply("sid1")
    check("apply 新增账号成功", r.get("ok") and r.get("count") == 1, str(r))
    data = store.load()
    accs = store.accounts(data, "ALIYUN")
    check("refresh_token 已写入", accs[0].get("refresh_token") == "rt-new", str(accs))

    # 追加一个带可选字段的账号，再续期，确认可选字段保留
    data = store.load()
    accs = store.accounts(data, "ALIYUN")
    accs.append({"refresh_token": "rt-old", "note": "keep-me"})
    store.set_accounts(data, "ALIYUN", accs)
    store.write(data, backup=False)
    sb._SESSIONS["sid1"]["credential_value"] = "rt-renewed"
    r2 = sb.apply("sid1", index=1)
    check("apply 续期指定账号", r2.get("ok"), str(r2))
    accs2 = store.accounts(store.load(), "ALIYUN")
    check("续期只改 refresh_token", accs2[1].get("refresh_token") == "rt-renewed", str(accs2))
    check("续期保留其它字段", accs2[1].get("note") == "keep-me", str(accs2))
    check("续期不动别的账号", accs2[0].get("refresh_token") == "rt-new", str(accs2))

    # 未确认会话不能落盘
    sb._SESSIONS["sid2"] = {"id": "sid2", "site": "ALIYUN", "confirmed": False}
    try:
        sb.apply("sid2")
        check("未确认会话拒绝落盘", False)
    except ValueError:
        check("未确认会话拒绝落盘", True)
finally:
    sb._SESSIONS.clear()
    paths.HOME, paths.CONFIG_PATH, paths.DATA_DIR = old_home, old_config, old_data

# --------------------------------------------------------------------------- #
section("6) 前端入口已接好")
ui = open(os.path.join(ROOT, "panel", "static", "app.js"), encoding="utf-8").read()
html = open(os.path.join(ROOT, "panel", "static", "index.html"), encoding="utf-8").read()
check("通用扫码弹窗存在", 'id="siteQrDlg"' in html)
check("加载支持列表", "/site-browser/supported" in ui)
check("扫码入口按钮", 'data-action="site-qr"' in ui)
check("续期按钮", 'data-action="site-qr-renew"' in ui)
check("start 走通用 API", '/start' in ui and 'bqrApi' in ui)
check("apply 走通用 API", 'bqrApi("/apply")' in ui)
check("手动填写仍保留（表单字段动态渲染）", 'data-field="${esc(f.key)}"' in ui and "refresh_token" in open(os.path.join(ROOT, "panel", "catalog.py"), encoding="utf-8").read())

print("\n" + "=" * 68)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)
sys.exit(1 if failed else 0)
