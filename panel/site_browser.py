"""通用站点浏览器登录：无头浏览器打开登录页，扫二维码登录后自动抓凭据。

设计目标
--------
豆包的扫码登录（doubao_browser / doubao_auth）已经跑通，但它是**单站点专用**
的：登录 URL、二维码选择器、成功判据、凭据字段全写死。这里把这套能力抽象成
**数据驱动**的通用框架，让别的签到站点也能用同一条路：

1. 面板在无头浏览器里打开站点登录页；
2. 把页面上的登录二维码截图发给面板显示；
3. 用户用手机扫码 / 登录；
4. 面板轮询，按站点规则判断「登录成功」；
5. 成功后按规则抓凭据——**cookie**（CDP Storage.getCookies）或
   **localStorage 字段**（如阿里云盘 ``localStorage.token.refresh_token``）；
6. 点「保存」把凭据写进对应站点的账号池（新增或续期指定账号）。

为什么不全站点端到端保证
------------------------
各站登录页结构、二维码选择器、风控都不一样，且需要真实账号才能完整验证。
因此这里采取「**通用选择器 + 站点专属规则 + 手动兜底**」：
* 规则里写站点专属的登录 URL / 二维码选择器 / 成功判据；
* 覆盖不到的站点也能靠通用选择器尽力抓码；
* 任何一步失败都保留原来的手动填写入口，绝不让用户卡死。

每个登录会话独立 profile + 调试端口，账号之间不串登录态。浏览器缺失时直接
报错，由面板引导用户重启容器下载或用手动填写。
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import cookies as ck
from .doubao_browser import CDP, _launch, _terminate  # 复用已验证的 CDP 能力

try:
    from . import store  # 延迟用到， import 在此仅为类型清晰
except ImportError:  # pragma: no cover
    store = None  # type: ignore

__all__ = [
    "SUPPORTED", "available", "start", "status", "refresh", "cancel",
    "apply", "credential_of", "site_rule",
]

SESSION_TTL = 900        # 一次登录会话最长 15 分钟
QR_TTL = 55              # 前端倒计时用
PNG_URI = "data:image/png;base64,"

# Discuz 的 cookie 前缀（cookiepre）由站点自己配置，会随重装/升级变化：
# 实测恩山当前是 rHEX_2132_*，飞牛论坛是 pvRK_2132_*，旧文档里的 TWcq_2132_* 已失效。
# 登录标志固定是 <前缀>_auth，所以按模式识别，不能再写死某个前缀。
DISCUZ_AUTH_COOKIE = r"^[A-Za-z0-9]{2,16}_2132_auth$"

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}   # sid -> 会话（支持同时给不同站点登录）


# --------------------------------------------------------------------------- #
# 站点规则表
# --------------------------------------------------------------------------- #
# 每条规则：
#   login_url      登录页地址（直接打开就到登录/扫码）
#   click_login    可选 JS：落在首页时点「登录」进入登录弹窗/页
#   qr_selectors   二维码元素选择器（按序尝试，命中第一个可见的截图）
#   qr_frame       二维码在跨域 iframe 里时，从内到外列出所属域名；坐标会逐层加上 iframe 偏移
#   qr_supported   False 表示该站没有可用扫码登录，不展示扫码入口（只能手动填凭据）
#   credential     "cookie" | "localstorage"
#   domain         cookie 类：只收集域名含此串的 cookie
#   ready_cookies  cookie 类：这些 cookie 出现即判定登录成功（任意一个）
#   ls_key         localstorage 类：localStorage 的键名
#   ls_field       localstorage 类：JSON 解析后取哪个字段
#   emit_key       回填到账号的字段名（cookie 类固定 "cookie"）
#   hint           给用户的扫码提示
#   manual_howto   抓不到时引导用户手动获取的文案
#
# 说明：cookie 类的 required 字段从 catalog 读取，抓到后按域名收齐并过滤，
# 若缺关键字段会在状态里给出提示（不误判成功）。
RULES: dict[str, dict] = {
    "ALIYUN": {
        "login_url": "https://www.alipan.com/sign/in",
        # 首页那个 .qrcode canvas 是「下载 App」的码，不是登录码。真正的登录码在
        # www → auth → passport 两层跨域 iframe 里，必须按上下文取，不能按顶层 DOM 取。
        "qr_frame": ["passport.alipan.com", "auth.alipan.com"],
        "qr_selectors": [
            "#qrcode-img canvas", "#qrcode-img", ".qrcode-login canvas",
            ".qrcode-login img",
        ],
        "credential": "localstorage",
        "ls_key": "token",
        "ls_field": "refresh_token",
        "emit_key": "refresh_token",
        "hint": "请用阿里云盘 App 扫码登录；面板会自动从浏览器取出 refresh_token",
        "manual_howto": "登录 www.alipan.com → F12 → Console → 执行 "
        "copy(JSON.parse(localStorage.token).refresh_token)，把结果粘到 refresh_token 字段",
    },
    "BILIBILI": {
        "login_url": "https://passport.bilibili.com/login",
        "qr_selectors": [
            ".login-qrcode canvas", "[class*=qrcode] canvas", ".qrcode-img",
            "[class*=qrcode] img", "canvas",
        ],
        "credential": "cookie",
        "domain": "bilibili.com",
        "ready_cookies": ["SESSDATA"],
        "hint": "请用哔哩哔哩 App 扫码登录；面板会自动取出登录 Cookie",
        "manual_howto": "登录 bilibili.com → F12 → Application → Cookies，整块复制（含 SESSDATA、bili_jct）",
    },
    "WEIBO": {
        "login_url": "https://passport.weibo.cn/signin/login?entry=mweibo",
        "qr_selectors": [
            'img[src*="qr.weibo.cn"]', "[class*=qrcode] canvas", ".qrcode canvas",
            "canvas", "[class*=qrcode] img",
        ],
        "credential": "cookie",
        "domain": "weibo.cn",
        "ready_cookies": ["SUB"],
        "hint": "请用微博 App 扫码登录；面板会自动取出登录 Cookie",
        "manual_howto": "登录 m.weibo.cn → F12 → Application → Cookies，复制 SUB（值末尾的 = 不要删）",
    },
    "IQIYI": {
        # 老 passport 登录页已经 404；现在从首页登录弹窗取二维码（页面内是内联 SVG）。
        "login_url": "https://www.iqiyi.com/",
        "click_login": "(()=>{let e=document.querySelector('#btn_user');if(e){e.click();return true}return false})()",
        "qr_selectors": [
            "[class*=_qrBox_] svg", "[class*=qrBox] svg", "[class*=_qrBox_]",
            "[class*=qrcode] canvas", ".qrcode canvas", "canvas",
        ],
        "credential": "cookie",
        "domain": "iqiyi.com",
        "ready_cookies": ["P00001"],
        "hint": "请用爱奇艺 App 扫码登录；面板会自动取出登录 Cookie",
        "manual_howto": "登录 iqiyi.com → F12 → Application → Cookies，整块复制（含 P00001）",
    },
    "TIEBA": {
        "login_url": "https://passport.baidu.com/v2/?login",
        "qr_selectors": [
            "[class*=qrcode] canvas", ".qrcode canvas", "canvas",
            "[class*=qrcode] img",
        ],
        "credential": "cookie",
        "domain": "baidu.com",
        "ready_cookies": ["BDUSS"],
        "hint": "请用百度 App 扫码登录；面板会自动取出 BDUSS",
        "manual_howto": "登录 tieba.baidu.com → F12 → Application → Cookies，复制 BDUSS",
    },
    "BAIDUWP": {
        "login_url": "https://passport.baidu.com/v2/?login",
        "qr_selectors": [
            "[class*=qrcode] canvas", ".qrcode canvas", "canvas",
            "[class*=qrcode] img",
        ],
        "credential": "cookie",
        "domain": "baidu.com",
        "ready_cookies": ["BDUSS"],
        "hint": "请用百度网盘 App 扫码登录；面板会自动取出 BDUSS / STOKEN",
        "manual_howto": "登录 pan.baidu.com → F12 → Application → Cookies，复制 BDUSS 与 STOKEN",
    },
    "KGQQ": {
        "login_url": "https://kg.qq.com/",
        # 首页上 .mod_code canvas 是「下载 App 码」；点右上角登录后才弹真正的扫码框，
        # 真码在 .login_qrcode__img 里的 198×198 canvas。
        "click_login": "(()=>{let e=document.querySelector('a.j_login');if(e){e.click();return true}return false})()",
        "qr_selectors": [
            ".login_qrcode__img canvas", ".login_qrcode canvas",
            "[class*=login_qrcode] canvas",
        ],
        "credential": "cookie",
        "domain": "qq.com",
        "ready_cookies": ["openkey", "uid"],
        "hint": "请用全民 K 歌 App 扫码登录；面板会自动取出 uid / openid / openkey",
        "manual_howto": "登录 kg.qq.com → F12 → Application → Cookies，uid / openid / openkey 三个都要",
    },
    "YOUDAO": {
        "login_url": "https://note.youdao.com/signIn/index.html?&callback=https://note.youdao.com/web/&from=web",
        # 登录页默认是账密 iframe；点右上角切换按钮才出扫码面板，真码是
        # .web-qrcode-container 下的 data:image/png img（168×168）。
        "click_login": "(()=>{let e=document.querySelector('.login-type-switch');if(e){e.click();return true}return false})()",
        "qr_selectors": [
            ".web-qrcode-container img", "[class*=qrcode-container] img",
            "[class*=qrcode] img",
        ],
        "credential": "cookie",
        "domain": "youdao.com",
        "ready_cookies": ["YNOTE_PERS"],
        "hint": "请用有道云笔记 App 扫码登录；面板会自动取出 YNOTE_PERS / YNOTE_SESS",
        "manual_howto": "登录 note.youdao.com → F12 → Application → Cookies，重点是 YNOTE_PERS 与 YNOTE_SESS",
    },
    "V2EX": {
        "login_url": "https://v2ex.com/signin",
        "qr_selectors": ["[class*=qrcode] canvas", "canvas"],
        "qr_supported": False,  # 只有账密登录，无头浏览器里用户没法交互
        "credential": "cookie",
        "domain": "v2ex.com",
        "ready_cookies": ["A2"],
        "hint": "请在打开的页面用账号登录（V2EX 没有扫码）；面板会自动取出 A2",
        "manual_howto": "登录 v2ex.com → F12 → Application → Cookies，复制 A2",
    },
    "SMZDM": {
        # sso.smzdm.com 已经解析不到；登录弹窗实际来自 zhiyou.smzdm.com。
        "login_url": "https://zhiyou.smzdm.com/user/login/window/?redirect_url=https%3A%2F%2Fwww.smzdm.com%2F",
        "qr_selectors": [
            ".login-container__qrcode-real", ".login-container__qrcode-content canvas",
            "[class*=qrcode] canvas", "canvas",
        ],
        "credential": "cookie",
        "domain": "smzdm.com",
        "ready_cookies": ["s"],
        "hint": "请扫码或登录什么值得买；面板会自动取出登录 Cookie",
        "manual_howto": "登录 smzdm.com → F12 → Application → Cookies，整块复制（关键是 s）",
    },
    "ENSHAN": {
        # Discuz 账密页没有码，但「快捷登录」里的 QQ 互联会跳到 graph.qq.com，
        # 真码在 xui.ptlogin2.qq.com 的 iframe 里（img#qrlogin_img，87×87）。
        "login_url": "https://www.right.com.cn/forum/connect.php?mod=login&op=init"
        "&referer=https%3A%2F%2Fwww.right.com.cn%2Fforum%2F&statfrom=login",
        "qr_frame": ["xui.ptlogin2.qq.com"],
        "qr_selectors": ["#qrlogin_img", "img.qrImg"],
        # QQ 扫码确认后，二维码 img 会被成功提示文字覆盖（无头浏览器里渲染成方框），
        # 但恩山cookie要等 QQ 回调才设置。这段时间别截图，否则把方框当二维码回传。
        "qr_alive_js": "(()=>{let e=document.querySelector('#qrlogin_img');"
        "if(!e||!(e.getAttribute('src')||'').includes('ptqrshow'))return '';"
        "let t=document.body.innerText||'';"
        "if(/扫描成功|登录成功|扫码成功|授权成功/.test(t))return '';return '1'})()",
        "confirm_hint": "已扫码，正在完成 QQ 授权，请稍候…",
        "confirm_hint_long": "QQ 授权回调还没完成，请确认手机上已点「确认登录」。若一直没反应，可关闭本窗口改用手动 Cookie：",
        # 手机确认后 graph.qq.com 有时停在「授权并登录」中间页，不会自己回调恩山；
        # 这里在等待期自动点一次授权按钮，避免用户卡在 confirm_hint 上。
        "after_scan_frames": ["graph.qq.com", "connect.qq.com", "xui.ptlogin2.qq.com"],
        "after_scan_js": "(()=>{let b=[...document.querySelectorAll('a,button,input[type=submit],[class*=btn]')]"
        ".find(e=>{let t=(e.innerText||e.value||'').trim();"
        "return t&&t.length<12&&/登录|授权|同意|确认|允许/.test(t)"
        "&&!/QQ登录|授权管理|申请接入|找回|注册|切换|绑定|取消|返回|关闭|拒绝/.test(t)});"
        "if(b){b.click();return '1'}return ''})()",
        "credential": "cookie",
        "domain": "right.com.cn",
        # 保留旧前缀兼容，关键是通用模式：真实浏览器里恩山是 rHEX_2132_auth。
        "ready_cookies": ["TWcq_2132_auth"],
        "ready_cookie_patterns": [DISCUZ_AUTH_COOKIE],
        "auth_cookie_pattern": DISCUZ_AUTH_COOKIE,
        "hint": "请用 QQ 手机版扫码登录恩山；面板会自动取出登录 Cookie",
        "manual_howto": "登录 right.com.cn → F12 → Application → Cookies，整块复制包含 xxx_2132_auth 与 xxx_2132_saltkey 的 Cookie（前缀以实际页面为准）",
    },
    "FNNASCLUB": {
        # 右上角微信图标（img.qq_bind）点开的是 xigua_login 插件：
        # plugin.php?id=xigua_login:login&qr=1 里直接挂着微信二维码（220×220）。
        "login_url": "https://club.fnnas.com/plugin.php?id=xigua_login:login&qr=1",
        "qr_selectors": ['img[src*="xigua_login/cache"]'],
        # 微信确认后同样别再截图，否则把成功提示（无头里是方框）当二维码回传。
        "qr_alive_js": "(()=>{let e=document.querySelector('img[src*=xigua_login/cache]');"
        "if(!e||!e.naturalWidth)return '';"
        "let t=document.body.innerText||'';"
        "if(/登录成功|扫码成功|扫描成功|授权成功/.test(t))return '';return '1'})()",
        # 微信扫码确认后 open.weixin.qq.com 会出「确认登录」中间页，自动点掉再等回调。
        "after_scan_frames": ["open.weixin.qq.com", "mp.weixin.qq.com"],
        "after_scan_js": "(()=>{let b=[...document.querySelectorAll('a,button,[class*=btn]')]"
        ".find(e=>/确认登录|同意登录|授权登录/.test((e.innerText||'').trim()));"
        "if(b){b.click();return '1'}return ''})()",
        "credential": "cookie",
        "domain": "fnnas.com",
        "ready_cookies": ["pvRK_2132_auth"],
        "ready_cookie_patterns": [DISCUZ_AUTH_COOKIE],
        "auth_cookie_pattern": DISCUZ_AUTH_COOKIE,
        "confirm_hint": "已扫码，正在完成微信授权，请稍候…",
        "confirm_hint_long": "微信授权回调还没完成，请确认手机上已点「确认登录」。若一直没反应，可关闭本窗口改用手动 Cookie：",
        "hint": "请用微信扫码登录飞牛 NAS 论坛；面板会自动取出登录 Cookie",
        "manual_howto": "登录 club.fnnas.com → F12 → Application → Cookies，复制 pvRK_2132_* 字段",
    },
}

# 通用兜底选择器：规则里的都没命中时再试这些
GENERIC_QR_SELECTORS = [
    "[class*=qrcode] canvas", "[class*=qr-code] canvas", ".qrcode canvas",
    "[data-testid*=qr] canvas", "canvas",
    "[class*=qrcode] img", "[class*=qr-code] img", 'img[src^="data:image/png"]',
]


def site_rule(site: str) -> dict | None:
    return RULES.get(str(site or "").upper())


def supported() -> list[str]:
    """只列真的能扫码的站点；账密站留在 RULES 里仅供凭据字段参考。"""
    return sorted(k for k, v in RULES.items() if v.get("qr_supported") is not False)


# 兼容调用方可能用的名字
SUPPORTED = supported()


def available() -> tuple[bool, str]:
    """面板用来判断要不要展示「浏览器登录」入口。"""
    from .doubao_browser import available as db_available

    return db_available()


# --------------------------------------------------------------------------- #
# 二维码提取
# --------------------------------------------------------------------------- #
_QR_RECT_JS = """(sel)=>{let e=document.querySelector(sel);if(!e)return '';
// img 刚挂上 DOM 时 src 已存在但像素还没到，截图会是一张白板；等 naturalWidth 出来。
if(e.tagName==='IMG'&&(!e.complete||!e.naturalWidth))return '';
let r=e.getBoundingClientRect();if(r.width<40||r.height<40)return '';
e.scrollIntoView({block:'center'});r=e.getBoundingClientRect();
return JSON.stringify({x:r.x,y:r.y,width:r.width,height:r.height})}"""

# OOPIF 的 frameElement 在子上下文里经常是 null；反过来让父页面按 src 找 iframe，
# 读取它嵌入父页面时的位置。多层 iframe 逐层相加，就得到二维码在顶层 viewport 的坐标。
_IFRAME_RECT_JS = """(mark)=>{let e=[...document.querySelectorAll('iframe')]
.find(f=>(f.src||'').includes(mark));if(!e)return '';
let r=e.getBoundingClientRect();return JSON.stringify({x:r.x,y:r.y})}"""


def _eval_context(cdp: CDP, mark: str, expression: str):
    """在域名含 mark 的 JS 上下文（含跨域 iframe）里执行一段表达式。"""
    contexts = getattr(cdp, "contexts", None)
    if not callable(contexts):
        return ""
    for ctx in contexts():
        hay = " ".join(str(ctx.get(key) or "") for key in ("origin", "name"))
        if mark not in hay:
            continue
        try:
            return cdp.js(expression, ctx["id"]) or ""
        except Exception:  # noqa: BLE001 - 导航中上下文会短暂失效，下一轮再取
            return ""
    return ""


def _frame_offset(cdp: CDP, frames: list[str]) -> tuple[float, float]:
    x = y = 0.0
    contexts = getattr(cdp, "contexts", None)
    if not callable(contexts):
        return x, y
    for mark in frames:
        expr = f"({_IFRAME_RECT_JS})({json.dumps(mark)})"
        for ctx in contexts():
            try:
                raw = cdp.js(expr, ctx["id"])
            except Exception:  # noqa: BLE001 - 已销毁的上下文/导航中，换下一个
                continue
            if not raw:
                continue
            try:
                rect = json.loads(raw)
                x += float(rect.get("x") or 0)
                y += float(rect.get("y") or 0)
            except (TypeError, ValueError):
                continue
            break
    return x, y


def _qrcode(cdp: CDP, selectors: list[str], frames: list[str] | None = None) -> str:
    """在页面（或指定 iframe）里找可见二维码，截图返回 data URI。

    截图而非取 canvas.toDataURL：拿到的是用户实际看到的完整二维码，
    也不受跨域 canvas 污染影响。frames 从内到外列出 iframe 所属域名，
    避免把外层页面上的「下载 App 码」误当成登录码。
    """
    frames = list(frames or [])
    offset_x, offset_y = _frame_offset(cdp, frames) if frames else (0.0, 0.0)
    # 有 frames 时只在最内层 iframe 里找；没有就走顶层页面。
    target = frames[0] if frames else ""
    for sel in list(selectors) + GENERIC_QR_SELECTORS:
        try:
            expr = f"({_QR_RECT_JS})({json.dumps(sel)})"
            raw = _eval_context(cdp, target, expr) if target else cdp.js(expr)
            if not raw:
                continue
            clip = json.loads(raw)
            clip["x"] = float(clip.get("x") or 0) + offset_x
            clip["y"] = float(clip.get("y") or 0) + offset_y
            clip["scale"] = 3
            shot = cdp.page("Page.captureScreenshot", {
                "format": "png", "clip": clip, "captureBeyondViewport": False,
            })
            data = str((shot or {}).get("data") or "")
            if data:
                return PNG_URI + data
        except Exception:  # noqa: BLE001 - 页面导航中/元素消失，试下一个选择器
            continue
    return ""


def _qr_alive(cdp: CDP, rule: dict) -> bool:
    """二维码是否还有效。

    有些站点（如恩山的 QQ 互联）扫码确认后，二维码区域会被中文成功提示覆盖，
    但本站 cookie 要等第三方回调才写入。这段窗口期若继续截图，就会把渲染成
    方框的中文当成二维码回传。规则可用 ``qr_alive_js`` 精确判断；返回 '1'
    表示码还在，其余情况一律认为已失效。
    """
    expr = rule.get("qr_alive_js") or ""
    if not expr:
        return True
    frames = rule.get("qr_frame") or []
    target = frames[0] if frames else ""
    try:
        raw = _eval_context(cdp, target, expr) if target else cdp.js(expr)
    except Exception:  # noqa: BLE001 - 上下文导航中会短暂失效
        raw = ""
    return raw == "1"


def _nudge_callback(cdp: CDP, rule: dict) -> bool:
    """扫码确认后，替用户点掉第三方授权中间页上的「确认」按钮。

    QQ 互联 / 微信登录在手机确认后，浏览器侧经常还要点一次「授权并登录」
    才会跳回站点写 cookie。无头浏览器里没人点，面板就一直停在 confirm_hint。
    只在规则列出的第三方上下文里执行，避免误点站点自己的按钮。
    """
    expr = rule.get("after_scan_js") or ""
    marks = rule.get("after_scan_frames") or []
    if not expr or not marks:
        return False
    contexts = getattr(cdp, "contexts", None)
    if not callable(contexts):
        return False
    clicked = False
    for ctx in contexts():
        hay = " ".join(str(ctx.get(key) or "") for key in ("origin", "name"))
        if not any(mark in hay for mark in marks):
            continue
        try:
            if str(cdp.js(expr, ctx["id"]) or "") == "1":
                clicked = True
        except Exception:  # noqa: BLE001 - 上下文正在跳转
            continue
    return clicked


# --------------------------------------------------------------------------- #
# 凭据提取
# --------------------------------------------------------------------------- #
def _localstorage(cdp: CDP, key: str, field: str) -> tuple[str, dict]:
    """读 localStorage[key]，按 field 取出凭据；返回 (值, 完整对象)。"""
    expr = """(()=>{let raw=localStorage.getItem(%s);if(!raw)return '';
try{let o=JSON.parse(raw);return JSON.stringify(o)}catch(e){return raw}})()""" % json.dumps(key)
    raw = cdp.js(expr)
    if not raw:
        return "", {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    value = str(obj.get(field) or "").strip() if obj else str(raw).strip()
    return value, obj


def _cookies(cdp: CDP, domain: str) -> dict:
    data = cdp.browser("Storage.getCookies")
    out: dict = {}
    for item in data.get("cookies", []):
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        dom = str(item.get("domain") or "")
        if name and value and (not domain or domain in dom):
            out[name] = value
    return out


def credential_of(one: dict) -> dict:
    """把会话里抓到的原始凭据整理成「账号字段 -> 值」。"""
    rule = site_rule(one.get("site", "")) or {}
    kind = rule.get("credential") or "cookie"
    if kind == "localstorage":
        value = one.get("credential_value") or ""
        emit = rule.get("emit_key") or "refresh_token"
        return {emit: value} if value else {}
    pairs = [(k, v) for k, v in (one.get("cookies") or {}).items() if k and v]
    cookie = ck.build(pairs)
    return {"cookie": cookie} if cookie else {}


# --------------------------------------------------------------------------- #
# 会话生命周期
# --------------------------------------------------------------------------- #
def _stop_locked(sid: str) -> None:
    one = _SESSIONS.pop(sid, None)
    if not one:
        return
    try:
        one["cdp"].browser("Browser.close")
    except Exception:  # noqa: BLE001
        pass
    one["cdp"].close()
    _terminate(one["proc"], one["profile"])


def _open_login(cdp: CDP, rule: dict) -> str:
    cdp.page("Page.enable")
    cdp.page("Runtime.enable")
    cdp.page("Page.navigate", {"url": rule["login_url"]})
    for _ in range(80):
        if cdp.js("document.readyState === 'complete' && !!document.body"):
            break
        time.sleep(0.25)
    click = rule.get("click_login") or ""
    if click:
        for _ in range(20):
            try:
                if cdp.js(click):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
    for _ in range(40):
        qr = _qrcode(cdp, rule.get("qr_selectors") or [], rule.get("qr_frame"))
        if qr:
            return qr
        time.sleep(0.5)
    return ""


def start(site: str) -> dict:
    site = str(site or "").upper()
    rule = site_rule(site)
    if not rule or rule.get("qr_supported") is False:
        raise ValueError(f"站点 {site} 没有可用的扫码登录，请手动填写凭据")
    available_ok, why = available()
    if not available_ok:
        raise RuntimeError(why)

    with _LOCK:
        sid = secrets.token_hex(12)
        info = _launch()
        qr = _open_login(info["cdp"], rule)
        if not qr:
            _terminate(info["proc"], info["profile"])
            raise RuntimeError(
                "登录页上没找到二维码；该站点可能需要账密登录或页面改版了。"
                "请用手动方式填写凭据"
            )
        now = time.time()
        _SESSIONS[sid] = {
            "id": sid, "site": site, "rule": rule,
            "qrcode": qr, "qr_at": now, "started_at": now,
            "confirmed": False, "cookies": {},
            "credential_value": "", "credential_obj": {},
            **info,
        }
        return {
            "id": sid, "site": site, "qrcode": qr, "status": "waiting",
            "expires_in": QR_TTL, "auth_url": rule["login_url"],
            "hint": rule.get("hint", ""), "mode": "browser",
        }


def status(sid: str) -> dict:
    with _LOCK:
        one = _SESSIONS.get(sid)
        if not one:
            raise ValueError("浏览器登录会话不存在或已结束")
        rule = one["rule"]
        site = one["site"]

        if one["confirmed"]:
            cred = credential_of(one)
            return {
                "status": "confirmed", "site": site, "credential": cred,
                "expires_in": 0, "message": "登录成功，点「保存」生效",
                "mode": "browser",
            }

        if time.time() - one["started_at"] > SESSION_TTL:
            _stop_locked(sid)
            raise ValueError("扫码等待超时，请重新开始")

        try:
            cdp = one["cdp"]
            # 先判登录态，再决定要不要取图：手机上点确认后，页面会立刻把二维码
            # 换成「对勾 + 登录成功」浮层，这时再按坐标截图就会把浮层（以及无头
            # 浏览器渲染成方框的中文字）当成二维码回传，面板里就出现乱码图片。
            # 已经登录时直接返回 confirmed，根本不取图，前端自绘成功态。
            if rule.get("credential") == "localstorage":
                value, obj = _localstorage(cdp, rule["ls_key"], rule["ls_field"])
                one["credential_value"], one["credential_obj"] = value, obj
                ok = bool(value)
            else:
                found = _cookies(cdp, rule.get("domain", ""))
                one["cookies"].update(found)
                ready = rule.get("ready_cookies") or []
                patterns = rule.get("ready_cookie_patterns") or []
                if ready or patterns:
                    ok = any(k in one["cookies"] for k in ready) or any(
                        re.search(p, k) for k in one["cookies"] for p in patterns
                    )
                else:
                    ok = bool(found)
            if not ok:
                # 二维码会自己翻新，顺带更新给前端；但确认后二维码区域会被
                # 成功提示覆盖，这时停在上一张有效码，不要截到方框乱码。
                # 一旦进入「已扫码待回调」就锁定，后续轮询不再取图。
                if one.get("awaiting_callback"):
                    _nudge_callback(cdp, rule)
                elif _qr_alive(cdp, rule):
                    qr = _qrcode(cdp, rule.get("qr_selectors") or [], rule.get("qr_frame"))
                    if qr and qr != one["qrcode"]:
                        one["qrcode"] = qr
                        one["qr_at"] = time.time()
                else:
                    one["awaiting_callback"] = True
                    one["awaiting_at"] = time.time()
                    _nudge_callback(cdp, rule)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error", "site": site, "expires_in": 0,
                "message": f"读取浏览器登录态失败：{exc}", "mode": "browser",
            }

        if ok:
            one["confirmed"] = True
            cred = credential_of(one)
            # cookie 类再体检一次：关键字段缺失就别谎报成功
            if rule.get("credential") == "cookie":
                try:
                    from . import catalog

                    def satisfied(key: str) -> bool:
                        if key in one["cookies"]:
                            return True
                        # Discuz 前缀可变：required 里写的是旧前缀，只要按模式找到了
                        # 同类 auth cookie（如 rHEX_2132_auth），就算这个字段齐了。
                        pat = rule.get("auth_cookie_pattern") or ""
                        if pat and key.endswith("_auth"):
                            if any(re.search(pat, name) for name in one["cookies"]):
                                return True
                        return False

                    missing = [
                        k for k in catalog.required_of(site) if not satisfied(k)
                    ]
                except Exception:  # noqa: BLE001
                    missing = []
                if missing and cred:
                    return {
                        "status": "waiting", "site": site, "qrcode": one["qrcode"],
                        "expires_in": max(0, int(SESSION_TTL - (time.time() - one["started_at"]))),
                        "message": "已登录，但还缺字段：" + "、".join(missing) + "；请确认登录的是该站点账号",
                        "mode": "browser", "hint": rule.get("manual_howto", ""),
                    }
            return {
                "status": "confirmed", "site": site, "credential": cred,
                "expires_in": 0, "message": "登录成功，点「保存」生效", "mode": "browser",
            }

        remaining = max(0, int(SESSION_TTL - (time.time() - one["started_at"])))
        waiting_msg = one.get("awaiting_callback") and rule.get("confirm_hint")
        if waiting_msg:
            waited = time.time() - float(one.get("awaiting_at") or time.time())
            if waited > 60:
                waiting_msg = (
                    rule.get("confirm_hint_long", "第三方授权回调还没完成，请稍候。")
                    + rule.get("manual_howto", "")
                )
        return {
            "status": "waiting", "site": site,
            "qrcode": "" if one.get("awaiting_callback") else one["qrcode"],
            "expires_in": remaining,
            "message": waiting_msg or rule.get("hint", "请用手机扫码登录"),
            "mode": "browser",
        }


def refresh(sid: str) -> dict:
    with _LOCK:
        one = _SESSIONS.get(sid)
        if not one:
            raise ValueError("浏览器登录会话不存在或已结束")
        site, rule = one["site"], one["rule"]
        _stop_locked(sid)
        info = _launch()
        qr = _open_login(info["cdp"], rule)
        if not qr:
            _terminate(info["proc"], info["profile"])
            raise RuntimeError("重新打开登录页后没找到二维码")
        now = time.time()
        _SESSIONS[sid] = {
            "id": sid, "site": site, "rule": rule,
            "qrcode": qr, "qr_at": now, "started_at": now,
            "confirmed": False, "cookies": {},
            "credential_value": "", "credential_obj": {},
            **info,
        }
        return {
            "id": sid, "site": site, "qrcode": qr, "status": "waiting",
            "expires_in": QR_TTL, "auth_url": rule["login_url"],
            "hint": rule.get("hint", ""), "mode": "browser",
        }


def cancel(sid: str) -> dict:
    with _LOCK:
        _stop_locked(sid)
    return {"ok": True}


def apply(sid: str, index: int | None = None) -> dict:
    """把已确认会话的凭据写进站点账号池：index=None 新增，否则续期指定账号。"""
    from . import runner

    with _LOCK:
        one = _SESSIONS.get(sid)
        if not one or not one.get("confirmed"):
            raise ValueError("还没拿到登录态，请先完成扫码登录")
        site = one["site"]
        cred = credential_of(one)
        if not cred:
            raise ValueError("没有可用的凭据，请重新扫码")

    data = store.load()
    accounts = store.accounts(data, site)
    if index is None:
        accounts.append(dict(cred))
        where = "新增"
    else:
        if not isinstance(index, int) or index < 0 or index >= len(accounts):
            raise ValueError("账号序号不存在")
        # 只覆盖凭据字段，其余字段（如 B 站投币设置、代理）原样保留
        accounts[index].update(cred)
        where = f"第 {index + 1} 个账号续期"
    store.set_accounts(data, site, accounts)
    store.write(data)
    runner.clear_site_status(site, "登录态已更新，等待重新校验")
    return {"ok": True, "site": site, "count": len(accounts), "where": where}
