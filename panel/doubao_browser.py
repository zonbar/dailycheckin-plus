"""豆包浏览器登录：容器内无头浏览器 + CDP 捞 Cookie。

签到凭据就是豆包网页的 Cookie；HttpOnly 的 sessionid 用 JS 读不到，
所以走浏览器级 CDP（Storage.getCookies）把登录弹窗所在域的 Cookie 一次取全。
这和"面板自拉浏览器、用户登录后收集会话"是同一条路，区别只在容器里没有桌面，
固定用无头模式，并把二维码从页面里取出来显示到面板上。

二维码不是 img 标签，而是登录弹窗里 ``[data-testid=qrcode_switcher]`` 的
CSS background-image（data:image/png;base64,...），直接从 JS 取即可。

每次登录都用全新 profile + 独立调试端口，账号之间不会串登录态。
``--no-sandbox`` / ``--disable-dev-shm-usage`` 是为在 Docker（含 NAS 自带的
Docker）里跑起来；浏览器缺失时直接报错，由面板引导用户改用手动 Cookie。
"""

from __future__ import annotations

import json
import os
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

    def call(self, ws, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != mid:
                continue  # 忽略事件通知
            if "error" in msg:
                raise CdpError(str(msg["error"].get("message") or msg["error"]))
            return msg.get("result") or {}

    def page(self, method: str, params: dict | None = None) -> dict:
        return self.call(self.page_ws, method, params)

    def browser(self, method: str, params: dict | None = None) -> dict:
        return self.call(self.browser_ws, method, params)

    def js(self, expression: str):
        result = self.page("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
        }).get("result") or {}
        return result.get("value")

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


_QR_JS = r"""(()=>{let e=document.querySelector('[data-testid=qrcode_switcher]');
if(!e)return '';
let m=getComputedStyle(e).backgroundImage.match(/data:image\/png;base64,([^"')]+)/);
return m?m[1]:''})()"""

_LOGIN_JS = r"""(()=>{let e=[...document.querySelectorAll('button')].find(
 e=>/^登录$|^登陆$/.test((e.textContent||'').trim()));if(e){e.click();return true}return false})()"""


def _qrcode(cdp: CDP) -> str:
    try:
        return str(cdp.js(_QR_JS) or "")
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
