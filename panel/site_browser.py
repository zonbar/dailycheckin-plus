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

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}   # sid -> 会话（支持同时给不同站点登录）


# --------------------------------------------------------------------------- #
# 站点规则表
# --------------------------------------------------------------------------- #
# 每条规则：
#   login_url      登录页地址（直接打开就到登录/扫码）
#   click_login    可选 JS：落在首页时点「登录」进入登录弹窗/页
#   qr_selectors   二维码元素选择器（按序尝试，命中第一个可见的截图）
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
        "qr_selectors": [
            ".qrcode canvas", ".qrcode-box canvas", "[class*=qrcode] canvas",
            ".qrcode img", "[class*=qrcode] img",
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
            "[class*=qrcode] canvas", ".qrcode canvas", "canvas",
            "[class*=qrcode] img",
        ],
        "credential": "cookie",
        "domain": "weibo.cn",
        "ready_cookies": ["SUB"],
        "hint": "请用微博 App 扫码登录；面板会自动取出登录 Cookie",
        "manual_howto": "登录 m.weibo.cn → F12 → Application → Cookies，复制 SUB（值末尾的 = 不要删）",
    },
    "IQIYI": {
        "login_url": "https://passport.iqiyi.com/pages/action/login/index.htm",
        "qr_selectors": [
            "[class*=qrcode] canvas", ".qrcode canvas", "canvas",
            "[class*=qr] img",
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
    "V2EX": {
        "login_url": "https://v2ex.com/signin",
        "qr_selectors": ["[class*=qrcode] canvas", "canvas"],
        "credential": "cookie",
        "domain": "v2ex.com",
        "ready_cookies": ["A2"],
        "hint": "请在打开的页面用账号登录（V2EX 没有扫码）；面板会自动取出 A2",
        "manual_howto": "登录 v2ex.com → F12 → Application → Cookies，复制 A2",
    },
    "SMZDM": {
        "login_url": "https://sso.smzdm.com/",
        "qr_selectors": ["[class*=qrcode] canvas", "canvas"],
        "credential": "cookie",
        "domain": "smzdm.com",
        "ready_cookies": ["s"],
        "hint": "请扫码或登录什么值得买；面板会自动取出登录 Cookie",
        "manual_howto": "登录 smzdm.com → F12 → Application → Cookies，整块复制（关键是 s）",
    },
    "ENSHAN": {
        "login_url": "https://www.right.com.cn/forum/member.php?mod=logging&action=login",
        "qr_selectors": ["[class*=qrcode] canvas", "canvas"],
        "credential": "cookie",
        "domain": "right.com.cn",
        "ready_cookies": ["auth"],
        "hint": "请登录恩山论坛（Discuz 多为账密）；面板会自动取出登录 Cookie",
        "manual_howto": "登录 right.com.cn → F12 → Application → Cookies，复制 TWcq_2132_auth 与 saltkey",
    },
    "FNNASCLUB": {
        "login_url": "https://club.fnnas.com/member.php?mod=logging&action=login",
        "qr_selectors": ["[class*=qrcode] canvas", "canvas"],
        "credential": "cookie",
        "domain": "fnnas.com",
        "ready_cookies": ["auth"],
        "hint": "请登录飞牛 NAS 论坛（Discuz 多为账密）；面板会自动取出登录 Cookie",
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
    return sorted(RULES)


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
let r=e.getBoundingClientRect();if(r.width<40||r.height<40)return '';
e.scrollIntoView({block:'center'});r=e.getBoundingClientRect();
return JSON.stringify({x:r.x,y:r.y,width:r.width,height:r.height})}"""


def _qrcode(cdp: CDP, selectors: list[str]) -> str:
    """在页面里找第一个可见的二维码元素，截图返回 data URI。

    截图而非取 canvas.toDataURL：拿到的是用户实际看到的完整二维码，
    也不受跨域 canvas 污染影响。
    """
    for sel in list(selectors) + GENERIC_QR_SELECTORS:
        try:
            raw = cdp.js(f"({_QR_RECT_JS})({json.dumps(sel)})")
            if not raw:
                continue
            clip = json.loads(raw)
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
        qr = _qrcode(cdp, rule.get("qr_selectors") or [])
        if qr:
            return qr
        time.sleep(0.5)
    return ""


def start(site: str) -> dict:
    site = str(site or "").upper()
    rule = site_rule(site)
    if not rule:
        raise ValueError(f"站点 {site} 暂不支持浏览器登录，请手动填写凭据")
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
            # 二维码会自己翻新，顺带更新给前端
            qr = _qrcode(cdp, rule.get("qr_selectors") or [])
            if qr and qr != one["qrcode"]:
                one["qrcode"] = qr
                one["qr_at"] = time.time()

            if rule.get("credential") == "localstorage":
                value, obj = _localstorage(cdp, rule["ls_key"], rule["ls_field"])
                one["credential_value"], one["credential_obj"] = value, obj
                ok = bool(value)
            else:
                found = _cookies(cdp, rule.get("domain", ""))
                one["cookies"].update(found)
                ready = rule.get("ready_cookies") or []
                ok = any(k in one["cookies"] for k in ready) if ready else bool(found)
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

                    missing = [
                        k for k in catalog.required_of(site)
                        if k not in one["cookies"]
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
        return {
            "status": "waiting", "site": site, "qrcode": one["qrcode"],
            "expires_in": remaining, "message": rule.get("hint", "请用手机扫码登录"),
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
