"""豆包浏览器登录：容器内无头浏览器 + CDP 捞 Cookie。

签到凭据就是豆包网页的 Cookie；HttpOnly 的 sessionid 用 JS 读不到，
所以走浏览器级 CDP（Storage.getCookies）把登录弹窗所在域的 Cookie 一次取全。
这和"面板自拉浏览器、用户登录后收集会话"是同一条路，区别只在容器里没有桌面，
固定用无头模式，并把二维码从页面里取出来显示到面板上。

真正的登录二维码在 ``[data-testid=qrcode_image]`` 里，是一个 162×162 的
**内联 SVG**；``[data-testid=qrcode_switcher]`` 只是隐藏的装饰图标（0×0、
蓝色斜切的半张 QR 图），早先误取它才导致面板里只显示半张图。
优先用 CDP 对真实的二维码元素截图（PNG，约 17 KiB）；截图失败再退回
序列化 SVG / 背景图。任何返回给前端的图都必须带 ``data:`` 前缀，否则
``<img src>`` 会当相对路径处理，弹窗空白。

每次登录都用全新 profile + 独立调试端口，账号之间不会串登录态。
``--no-sandbox`` / ``--disable-dev-shm-usage`` 是为在 Docker（含 NAS 自带的
Docker）里跑起来；浏览器缺失时直接报错，由面板引导用户改用手动 Cookie。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import cookies as ck
from . import doubao_auth

try:
    import websocket  # type: ignore
except ImportError:  # 依赖没装时给出可执行的提示，而不是裸的 ImportError
    websocket = None  # type: ignore

__all__ = ["start", "status", "refresh", "cancel", "apply_cookie", "available"]

AUTH_URL = doubao_auth.AUTH_URL
QR_TTL = 55            # 前端倒计时用；页面会自动换新码
SESSION_TTL = 900      # 一次登录会话最长 15 分钟
HTTP_TIMEOUT = 10
CDP_TIMEOUT = 20
BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
LAUNCH_RETRIES = 60    # 0.25s * 60 = 15s，够慢盘容器启动浏览器

_LOCK = threading.RLock()
_SESSION: dict | None = None   # 单会话：同时只开一个登录浏览器


class CdpError(RuntimeError):
    """浏览器调试通道不可用。"""


class CDP:
    """极简 CDP 客户端：只发命令、等对应 id 的响应。"""

    def __init__(self, port: int):
        if websocket is None:
            raise CdpError("缺少 websocket-client 依赖，请安装后重启容器")
        version = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=HTTP_TIMEOUT
            ).read()
        )
        tabs = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=HTTP_TIMEOUT
            ).read()
        )
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if not page:
            raise CdpError("浏览器没有可操作的页面")
        self.browser_ws = websocket.create_connection(
            version["webSocketDebuggerUrl"], timeout=CDP_TIMEOUT
        )
        self.page_ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=CDP_TIMEOUT
        )
        self._id = 0
        self.events: list[dict] = []  # Runtime 事件留给跨 iframe 取二维码用

    def call(self, ws, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != mid:
                # 执行上下文创建/销毁是 Runtime 事件，不挂 id；留着才能定位 iframe。
                if msg.get("method", "").startswith("Runtime."):
                    self.events.append(msg)
                continue
            if "error" in msg:
                raise CdpError(str(msg["error"].get("message") or msg["error"]))
            return msg.get("result") or {}

    def page(self, method: str, params: dict | None = None) -> dict:
        return self.call(self.page_ws, method, params)

    def browser(self, method: str, params: dict | None = None) -> dict:
        return self.call(self.browser_ws, method, params)

    def js(self, expression: str, context_id: int | None = None):
        params: dict = {"expression": expression, "returnByValue": True}
        if context_id is not None:
            params["contextId"] = context_id
        result = self.page("Runtime.evaluate", params).get("result") or {}
        return result.get("value")

    def contexts(self) -> list[dict]:
        """Runtime 开启后见过的所有 JS 上下文（含跨域 iframe）。"""
        self.page("Runtime.enable")
        out: dict[int, dict] = {}
        for event in self.events:
            if event.get("method") != "Runtime.executionContextCreated":
                continue
            ctx = ((event.get("params") or {}).get("context") or {})
            if isinstance(ctx.get("id"), int):
                out[ctx["id"]] = ctx
        return list(out.values())

    def close(self) -> None:
        for ws in (self.page_ws, self.browser_ws):
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败不阻塞清理
                pass


def available() -> tuple[bool, str]:
    """面板用来判断要不要展示「浏览器登录」入口。"""
    if websocket is None:
        return False, "缺少 websocket-client 依赖"
    try:
        browser_path()
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


def browser_path() -> str:
    custom = os.environ.get("DOUBAO_BROWSER", "").strip()
    if custom:
        if os.path.isfile(custom) and os.access(custom, os.X_OK):
            return custom
        raise RuntimeError(f"DOUBAO_BROWSER 指向的浏览器不可执行：{custom}")
    # 优先用首次启动时下载到持久卷里的 headless shell（见 browser_bootstrap）。
    from .browser_bootstrap import installed_path

    installed = installed_path()
    if installed:
        return str(installed)
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "没找到浏览器；请重启容器让首次下载完成，或设置 DOUBAO_BROWSER，"
        "也可以在面板里手动粘贴 Cookie"
    )


def _free_port() -> int:
    for _ in range(20):
        port = secrets.randbelow(20000) + 30000
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise CdpError("找不到可用的调试端口")


def _profile_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "doubao-browser"
    base.mkdir(parents=True, exist_ok=True)
    return base / ("login-" + secrets.token_hex(6))


def _launch() -> dict:
    binary = browser_path()
    port = _free_port()
    profile = _profile_dir()
    cmd = [
        binary,
        "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-software-rasterizer", "--disable-dev-shm-usage",
        "--disable-extensions", "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--window-size=1280,900", "about:blank",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    cdp = None
    for _ in range(LAUNCH_RETRIES):
        if proc.poll() is not None:
            break
        try:
            cdp = CDP(port)
            break
        except Exception:  # noqa: BLE001 - 浏览器还没就绪，继续等
            time.sleep(0.25)
    if cdp is None:
        _terminate(proc, profile)
        raise CdpError("无头浏览器启动失败或调试端口没起来")
    return {
        "port": port, "profile": str(profile), "proc": proc, "cdp": cdp,
        "seed": secrets.token_hex(16),
    }


def _terminate(proc: subprocess.Popen | None, profile: str | Path) -> None:
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:  # noqa: BLE001
            proc.kill()
    shutil.rmtree(str(profile), ignore_errors=True)


_PNG_URI = "data:image/png;base64,"
_SVG_URI = "data:image/svg+xml;base64,"

# 真实二维码：162×162 内联 SVG；switcher 是 0×0 的装饰图，绝不能再取它。
# 只截二维码图形本身（svg / img / canvas），不要截外层容器：手机确认后外层会
# 换成「圆形对勾 + 登录成功」，容器里没中文字体的无头浏览器会把它渲染成方框，
# 截图回传前端就成了用户看到的「对勾下面一堆方框」。图形消失时直接不回图，
# 前端继续用上一张真二维码，等 Cookie 落地后画自己的成功态。
_QR_RECT_JS = r"""(()=>{let e=document.querySelector('[data-testid=qrcode_image]');
if(!e)return '';let g=e.querySelector('svg,canvas,img[src^="data:image/"]');
if(!g){let bg=getComputedStyle(e).backgroundImage;
if(!/url\(/.test(bg)||(e.innerText||'').trim())return '';g=e}
let r=g.getBoundingClientRect();
if(r.width<100||r.height<100)return '';   // 成功页的对勾只有几十像素，别当成二维码
return JSON.stringify({x:r.x,y:r.y,width:r.width,height:r.height})})()"""

_QR_JS = r"""(()=>{let s=document.querySelector('[data-testid=qrcode_image] svg');
if(s)return JSON.stringify({mime:'image/svg+xml',data:s.outerHTML});
let e=document.querySelector('[data-testid=qrcode_image] img[src^="data:image/"]');
if(e)return JSON.stringify({mime:'image/png',data:e.getAttribute('src')});
return ''})()"""

_LOGIN_JS = r"""(()=>{let b=document.querySelector('[data-testid=to_login_button]');
if(b){b.click();return true}
let e=[...document.querySelectorAll('button')].find(
 x=>/^登录$|^登陆$/.test((x.textContent||'').trim()));if(e){e.click();return true}return false})()"""


def _qr_uri(raw: str) -> str:
    """把浏览器里取到的二维码统一成 ``<img src>`` 能直接用的 data URI。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("data:") or re.match(r"^https?://", text, re.I):
        return text
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        data = str(obj.get("data") or "").strip()
        if obj.get("mime") == "image/svg+xml" and data:
            # HTML 里的 outerHTML 会省略默认 xmlns，独立当 SVG 打开必须补上。
            if "<svg" in data and "xmlns=" not in data.split(">", 1)[0]:
                data = data.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
            return _SVG_URI + base64.b64encode(data.encode("utf-8")).decode("ascii")
        return _qr_uri(data)
    return _PNG_URI + "".join(text.split())


def _qr_screenshot(cdp: CDP) -> str:
    """对真实二维码元素截图：体积比内联 SVG 小一个数量级，也不怕 SVG 命名空间。"""
    try:
        raw = cdp.js(_QR_RECT_JS)
        rect = json.loads(raw) if raw else None
        if not isinstance(rect, dict):
            return ""
        clip = {
            "x": float(rect["x"]), "y": float(rect["y"]),
            "width": float(rect["width"]), "height": float(rect["height"]),
            "scale": 2,
        }
        if clip["width"] < 100 or clip["height"] < 100:
            return ""
        shot = cdp.page("Page.captureScreenshot", {
            "format": "png", "clip": clip, "captureBeyondViewport": False,
        })
        data = str((shot or {}).get("data") or "")
    except Exception:  # noqa: BLE001 - 截图失败还有 SVG 兜底
        return ""
    return _PNG_URI + data if data else ""


def _qrcode(cdp: CDP) -> str:
    # 先截图：拿到的一定是用户实际看到的完整二维码（不是隐藏的 switcher 图标）。
    uri = _qr_screenshot(cdp)
    if uri:
        return uri
    try:
        return _qr_uri(cdp.js(_QR_JS))
    except Exception:  # noqa: BLE001 - 页面导航中取不到，下一轮再试
        return ""


def _open_login(cdp: CDP) -> str:
    cdp.page("Page.enable")
    cdp.page("Runtime.enable")
    cdp.page("Page.navigate", {"url": AUTH_URL})
    for _ in range(60):
        if cdp.js("document.readyState === 'complete' && !!document.body"):
            break
        time.sleep(0.5)
    # 页面右上角的「登录」按钮（豆包没有独立登录页）
    for _ in range(20):
        try:
            if cdp.js(_LOGIN_JS):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    # 弹窗里的二维码是 CSS 背景图，会自动变；拿到第一张就先返回
    for _ in range(40):
        qr = _qrcode(cdp)
        if qr:
            return qr
        time.sleep(0.5)
    return ""


def _cookies(cdp: CDP) -> dict:
    data = cdp.browser("Storage.getCookies")
    out: dict = {}
    for item in data.get("cookies", []):
        if "doubao.com" in str(item.get("domain") or ""):
            out[str(item.get("name"))] = str(item.get("value") or "")
    return out


def _stop_locked() -> None:
    global _SESSION
    one = _SESSION
    if not one:
        return
    try:
        one["cdp"].browser("Browser.close")
    except Exception:  # noqa: BLE001
        pass
    one["cdp"].close()
    _terminate(one["proc"], one["profile"])
    _SESSION = None


def _new_session(seed: str | None = None, sid: str | None = None) -> dict:
    info = _launch()
    if seed:
        info["seed"] = seed
    qr = _open_login(info["cdp"])
    if not qr:
        _terminate(info["proc"], info["profile"])
        raise CdpError("登录弹窗里没找到二维码；豆包页面可能改版，请改用手动 Cookie")
    now = time.time()
    return {
        "id": sid or secrets.token_hex(12),
        **info,
        "qrcode": qr,
        "qr_at": now,
        "started_at": now,
        "status": "waiting",
        "cookie": "",
        "cookies": {},
    }


def start() -> dict:
    global _SESSION
    with _LOCK:
        _stop_locked()          # 同时只保留一个登录浏览器
        one = _new_session()
        _SESSION = one
        return {
            "id": one["id"], "qrcode": one["qrcode"], "status": "waiting",
            "expires_in": QR_TTL, "auth_url": AUTH_URL, "mode": "browser",
        }


def status(sid: str) -> dict:
    with _LOCK:
        one = _SESSION
        if not one or one["id"] != sid:
            raise ValueError("浏览器登录会话不存在或已结束")
        if one["status"] == "confirmed":
            return {
                "status": "confirmed", "cookie": one["cookie"],
                "seed": one["seed"], "expires_in": 0,
                "message": "登录成功，点「保存」或「续期」生效", "mode": "browser",
            }
        if time.time() - one["started_at"] > SESSION_TTL:
            _stop_locked()
            raise ValueError("扫码等待超时，请重新开始")

        # CDP 调用放在锁内：串行轮询和「取消 / 换码」不会同时踩同一条连接。
        try:
            qr = _qrcode(one["cdp"])
            cookies = _cookies(one["cdp"])
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error", "expires_in": 0,
                "message": f"读取浏览器登录态失败：{exc}", "mode": "browser",
            }
        one["cookies"].update(cookies)
        if qr and qr != one["qrcode"]:
            one["qrcode"] = qr
            one["qr_at"] = time.time()   # 页面换新码，倒计时重新开始

        sessionid = one["cookies"].get("sessionid") or one["cookies"].get("sessionid_ss")
        if sessionid:
            pairs = [(k, v) for k, v in one["cookies"].items() if k and v]
            one["cookie"] = ck.build(pairs)
            one["status"] = "confirmed"
            return {
                "status": "confirmed", "cookie": one["cookie"],
                "seed": one["seed"], "expires_in": 0,
                "message": "登录成功，点「保存」或「续期」生效", "mode": "browser",
            }
        remaining = max(0, int(SESSION_TTL - (time.time() - one["started_at"])))
        return {
            "status": "waiting", "qrcode": one["qrcode"],
            "expires_in": remaining,
            "message": "请用手机豆包 / 飞书 App 扫码，并在手机上确认",
            "mode": "browser",
        }


def refresh(sid: str) -> dict:
    global _SESSION
    with _LOCK:
        one = _SESSION
        if not one or one["id"] != sid:
            raise ValueError("浏览器登录会话不存在或已结束")
        seed = one["seed"]       # 换码不换设备号，避免被当成新设备
        _stop_locked()
        new_one = _new_session(seed=seed, sid=sid)
        _SESSION = new_one
        return {
            "id": sid, "qrcode": new_one["qrcode"], "status": "waiting",
            "expires_in": QR_TTL, "auth_url": AUTH_URL, "mode": "browser",
        }


def cancel(sid: str) -> dict:
    with _LOCK:
        if _SESSION and _SESSION["id"] == sid:
            _stop_locked()
    return {"ok": True}


def apply_cookie(sid: str, index: int | None = None) -> dict:
    with _LOCK:
        one = _SESSION
        if not one or one["id"] != sid or one["status"] != "confirmed":
            raise ValueError("还没拿到登录态，请先完成扫码并在手机上确认")
        cookie, seed = one["cookie"], one["seed"]
    return doubao_auth.save_cookie(cookie, seed, index)
