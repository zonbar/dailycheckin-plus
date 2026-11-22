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
import inspect
import json
import os
import re
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

    def __init__(self, js_values=None, cookies=None, shot="", frames=None, contexts=None):
        self.js_values = js_values or {}
        self.cookies = cookies or []
        self.shot = shot
        self.frames = frames or {}
        self.contexts_value = contexts or []
        self.calls = []

    def js(self, expr, context_id=None):
        if "querySelectorAll('iframe')" in expr:
            for mark, rect in self.frames.items():
                if mark in expr:
                    return json.dumps(rect)
            return ""
        for key, value in self.js_values.items():
            if key in expr:
                return value
        return ""

    def contexts(self):
        return self.contexts_value

    def browser(self, method, params=None):
        if method == "Storage.getCookies":
            return {"cookies": self.cookies}
        return {}

    def page(self, method, params=None):
        self.calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {"data": self.shot}
        return {}

    def close(self):
        pass


# --------------------------------------------------------------------------- #
section("1) 规则表结构")
check("可扫码站点不少于 9 个", len(sb.SUPPORTED) >= 9, str(sb.SUPPORTED))
check("阿里云盘在支持列表里", "ALIYUN" in sb.SUPPORTED)
check("全民 K 歌 / 有道云笔记已接入扫码",
      "KGQQ" in sb.SUPPORTED and "YOUDAO" in sb.SUPPORTED, str(sb.SUPPORTED))
check("恩山 / 飞牛 NAS 论坛已接入扫码",
      "ENSHAN" in sb.SUPPORTED and "FNNASCLUB" in sb.SUPPORTED, str(sb.SUPPORTED))
enshan = sb.site_rule("ENSHAN")
check("恩山走 QQ 互联，二维码在 ptlogin iframe",
      "connect.php" in enshan["login_url"] and "xui.ptlogin2.qq.com" in enshan["qr_frame"]
      and "qr_supported" not in enshan)
fnnas = sb.site_rule("FNNASCLUB")
check("飞牛走微信登录插件，取 cache 下的二维码",
      "xigua_login" in fnnas["login_url"]
      and any("xigua_login/cache" in s for s in fnnas["qr_selectors"])
      and "qr_supported" not in fnnas)
check("豆包不在通用列表（它有专用通道）", "DOUBAO" not in sb.SUPPORTED)
check("V2EX 仍是账密站，不冒充扫码入口", "V2EX" not in sb.SUPPORTED)
try:
    sb.start("V2EX")
    check("账密站启动扫码会被拒", False)
except ValueError:
    check("账密站启动扫码会被拒", True)
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
check("登录码在 passport iframe 里取，不是首页下载码",
      ali.get("qr_frame") == ["passport.alipan.com", "auth.alipan.com"]
      and "#qrcode-img canvas" in ali["qr_selectors"])
check("选择器不再命中首页下载码", not any("qrcode-box" in s for s in ali["qr_selectors"]))

wb = sb.site_rule("WEIBO")
check("微博按二维码图片地址取码",
      any("qr.weibo.cn" in s for s in wb["qr_selectors"]))
zdm = sb.site_rule("SMZDM")
check("值得买登录地址改用实际域名", "zhiyou.smzdm.com" in zdm["login_url"])
check("值得买选择器命中真实 canvas",
      ".login-container__qrcode-real" in zdm["qr_selectors"])
# 手机扫码后到登录完成之间，用户还要在手机上过图形验证码；这段窗口期
# 页面把 canvas 换成「扫描成功」状态层，必须停止截图并给出对应提示。
check("值得买配置了扫码后存活检测与图形验证码提示",
      "login-container__qrcode-real" in zdm.get("qr_alive_js", "")
      and "扫描成功" in zdm.get("qr_alive_js", "")
      and "图形验证码" in zdm.get("confirm_hint", ""))
# 实测 poll 接口的 Set-Cookie：登录态是 sess(HttpOnly)+user+smzdm_id，
# 根本不下发老字段 s；死等 s 会导致手机已登录却判不出成功。
check("值得买按真实登录 cookie 字段判成功（非老的 s）",
      "user" in zdm["ready_cookies"] and "smzdm_id" in zdm["ready_cookies"]
      and "s" not in zdm["ready_cookies"])
zdm_alive = FakeCdp(contexts=[{"id": 2, "origin": "https://zhiyou.smzdm.com"}])
zdm_alive.js_values = {"qrcode-real": "1"}
check("值得买初始码有效", sb._qr_alive(zdm_alive, zdm) is True)
zdm_scanned = FakeCdp(contexts=[{"id": 2, "origin": "https://zhiyou.smzdm.com"}])
zdm_scanned.js_values = {}  # 状态层出现，检测返回空
check("值得买扫码后进入待确认、不再截图", sb._qr_alive(zdm_scanned, zdm) is False)
iqy = sb.site_rule("IQIYI")
check("爱奇艺改从首页登录弹窗取 SVG 码",
      "iqiyi.com" in iqy["login_url"] and "click_login" in iqy
      and any("svg" in s for s in iqy["qr_selectors"]))
kg = sb.site_rule("KGQQ")
check("全民 K 歌从登录弹窗取真码，不取首页下载码",
      "click_login" in kg and "j_login" in kg["click_login"]
      and ".login_qrcode__img canvas" in kg["qr_selectors"]
      and not any("mod_code" in s for s in kg["qr_selectors"]))
check("全民 K 歌取 uid / openid / openkey",
      kg["domain"] == "qq.com" and "openkey" in kg["ready_cookies"])
yd = sb.site_rule("YOUDAO")
check("有道云笔记先切扫码再取码",
      "signIn" in yd["login_url"] and "login-type-switch" in yd["click_login"]
      and ".web-qrcode-container img" in yd["qr_selectors"])
check("有道云笔记按 YNOTE_PERS 判成功",
      yd["domain"] == "youdao.com" and "YNOTE_PERS" in yd["ready_cookies"])

# 恩山 QQ 互联：确认后二维码区域被中文成功提示覆盖，必须停止截图
es = sb.site_rule("ENSHAN")
check("恩山配置了二维码存活检测与确认提示",
      bool(es.get("qr_alive_js")) and bool(es.get("confirm_hint"))
      and any("QQ" in es["confirm_hint"] or "授权" in es["confirm_hint"]
              for _ in [0]))
alive_on = FakeCdp(contexts=[{"id": 3, "origin": "https://xui.ptlogin2.qq.com", "name": ""}])
alive_on.js_values = {"ptqrshow": "1"}
check("二维码还在时判定为有效", sb._qr_alive(alive_on, es) is True)
alive_off = FakeCdp(contexts=[{"id": 3, "origin": "https://xui.ptlogin2.qq.com", "name": ""}])
alive_off.js_values = {}  # 成功提示覆盖后 js 返回空
check("成功提示覆盖后判定为失效", sb._qr_alive(alive_off, es) is False)
check("恩山配置了 QQ 授权中间页自动确认",
      bool(es.get("after_scan_js")) and "graph.qq.com" in (es.get("after_scan_frames") or []))
# 真实浏览器实测：xui.ptlogin2.qq.com 是二维码来源页，里面的「快捷登录/
# 密码登录」一点就把扫码流程打断，绝不能列进自动确认的上下文。
check("二维码来源页不参与自动确认（避免误点快捷登录）",
      "xui.ptlogin2.qq.com" not in (es.get("after_scan_frames") or []))
# 飞牛 qr_alive_js 的选择器值含 /，必须加引号；否则整段 JS 抛异常被当成
# 「码已失效」，页面一打开就卡在「正在完成微信授权」不显示二维码。
fnnas = sb.site_rule("FNNASCLUB")
check("飞牛存活检测的属性选择器带引号",
      'img[src*="xigua_login/cache"]' in fnnas.get("qr_alive_js", ""))
check("多上下文页面的存活检测会逐个试其它上下文",
      "for ctx in contexts:" in inspect.getsource(sb._qr_alive))
# 真实浏览器实测：恩山现在的 cookie 前缀是 rHEX，不是文档里写的 TWcq。
# 前缀写死会导致「QQ 已回调、Cookie 已写入」却判不出登录态，一直卡在 confirm_hint。
AUTH_RE = es["auth_cookie_pattern"]
check("恩山 auth cookie 按模式识别（兼容 rHEX 等动态前缀）",
      bool(re.match(AUTH_RE, "rHEX_2132_auth"))
      and bool(re.match(AUTH_RE, "TWcq_2132_auth"))
      and not re.match(AUTH_RE, "rHEX_2132_saltkey"))
check("ready_cookie_patterns 用同一动态前缀模式",
      AUTH_RE in (es.get("ready_cookie_patterns") or []))
check("飞牛 auth cookie 同样按模式识别",
      bool(re.match(sb.site_rule("FNNASCLUB")["auth_cookie_pattern"], "pvRK_2132_auth")))
# 手动填 cookie 也不该误报缺字段
aliases = catalog.required_aliases("ENSHAN")
check("catalog 提供恩山必填字段别名", bool(aliases.get("TWcq_2132_auth")),
      str(aliases))
from panel import cookies as ck
norm = ck.normalize("rHEX_2132_auth=abc; rHEX_2132_saltkey=xyz",
                    required=["TWcq_2132_auth"], aliases=aliases)
check("手动粘贴 rHEX 前缀不再误报缺字段", norm.missing == [], str(norm.missing))
nudge_ok = FakeCdp(contexts=[{"id": 7, "origin": "https://graph.qq.com"}],
                   js_values={"class*=btn": "1"})
check("第三方授权页上自动点确认", sb._nudge_callback(nudge_ok, es) is True)
nudge_skip = FakeCdp(contexts=[{"id": 8, "origin": "https://www.right.com.cn"}],
                     js_values={"授权并登录": "1"})
check("站点自己的页面不执行自动点击", sb._nudge_callback(nudge_skip, es) is False)
check("Discuz 变体 auth cookie 也能判成功",
      bool(re.search(AUTH_RE, "TWcq_2132_auth"))
      and bool(re.search(AUTH_RE, "rHEX_2132_auth")))
check("飞牛微信登录后也会停止截图", bool(fnnas.get("qr_alive_js")) and bool(fnnas.get("confirm_hint")))

# 两个百度站点换成随面板打包的官方 App 图标，不依赖外网 favicon
cat_src = open(os.path.join(ROOT, "panel", "catalog.py"), encoding="utf-8").read()
check("贴吧 / 网盘改用本地图标",
      '"/static/icons/tieba.png"' in cat_src and '"/static/icons/baiduwp.png"' in cat_src)
for icon in ("tieba.png", "baiduwp.png"):
    path = os.path.join(ROOT, "panel", "static", "icons", icon)
    check(f"{icon} 已随面板打包", os.path.isfile(path) and os.path.getsize(path) > 1000)

src = open(os.path.join(ROOT, "panel", "site_browser.py"), encoding="utf-8").read()
body = src.split("def status(", 1)[-1]
check("先判登录态再取二维码，避免把成功态浮层当码截图",
      body.index("ok = bool(value)") < body.index("qr = _qrcode(cdp"))
check("取图前先过二维码存活检测", body.index("_qr_alive(cdp, rule)") < body.index("qr = _qrcode(cdp"))
check("进入待回调后锁定不再取图", "if one.get(\"awaiting_callback\"):" in body)
check("待回调时返回空二维码与友好文案",
      '"qrcode": "" if one.get("awaiting_callback") else one["qrcode"]' in body
      and 'waiting_msg or rule.get("hint"' in body)
check("待回调时自动点授权中间页的确认按钮", "_nudge_callback(cdp, rule)" in body)
check("待回调超过一分钟给出手动 Cookie 兜底", 'waited > 60' in body and 'confirm_hint_long' in body)
check("cookie 成功判据支持正则（兼容 Discuz cookie 前缀）",
      "ready_cookie_patterns" in body and "re.search(p, k)" in body)
check("成功后不再回传浏览器截图",
      '"status": "confirmed", "site": site, "credential": cred' in body
      and "qrcode" not in body.split('"status": "confirmed", "site": site, "credential": cred', 1)[1].split("}", 1)[0])

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

# 跨域 iframe：子上下文里只拿到二维码的局部坐标，两个父 iframe 的偏移要逐层累加。
frame_cdp = FakeCdp(
    js_values={"scrollIntoView": '{"x":5,"y":7,"width":112,"height":112}'},
    shot=shot_b64,
    frames={
        "passport.alipan.com": {"x": 100, "y": 60},
        "auth.alipan.com": {"x": 20, "y": 30},
    },
    contexts=[
        {"id": 1, "origin": "https://www.alipan.com"},
        {"id": 2, "origin": "https://auth.alipan.com"},
        {"id": 3, "origin": "https://passport.alipan.com"},
    ],
)
qr_frame = sb._qrcode(frame_cdp, ["#qrcode-img canvas"],
                      ["passport.alipan.com", "auth.alipan.com"])
clip = next((p.get("clip") or {} for m, p in frame_cdp.calls if m == "Page.captureScreenshot"), {})
check("img 类型二维码等像素加载完再截图",
      "e.complete" in sb._QR_RECT_JS and "e.naturalWidth" in sb._QR_RECT_JS)
check("iframe 内二维码截图坐标逐层累加",
      qr_frame == sb.PNG_URI + shot_b64
      and clip.get("x") == 125 and clip.get("y") == 97, str(clip))

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
check("通用扫码确认后自绘成功态，不显示方框截图",
      "paintBqrDone" in ui and "qr-done-text" in ui
      and 'paintBqrDone("登录成功")' in ui)
check("待回调时保留上一张二维码、用后端文案而非等待扫码",
      'else if (r.message) $("sqStatus").textContent = tidyText(r.message);' in ui)
check("手动填写仍保留（表单字段动态渲染）", 'data-field="${esc(f.key)}"' in ui and "refresh_token" in open(os.path.join(ROOT, "panel", "catalog.py"), encoding="utf-8").read())

print("\n" + "=" * 68)
print(f"通过 {len(passed)}  失败 {len(failed)}")
for one in failed:
    print("  ✗", one)
sys.exit(1 if failed else 0)
