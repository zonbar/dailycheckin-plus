"""豆包扫码登录：拿授权二维码 → 轮询确认 → 导出登录 Cookie。

整条链路是豆包 passport 的纯 HTTP 接口，**不需要浏览器、不需要额外依赖**。
二维码来自``https://www.doubao.com/chat/``页面右上角「登录」弹出的登录框；
豆包没有独立的 login 页面，所以 ``AUTH_URL`` 指向 chat 页，浏览器打开后点「登录」即可。

1. ``GET /``                              → ttwid / web_id 等基础 Cookie（302 要手动跟）
2. ``GET /passport/safe/csrf_token/``     → passport_csrf_token
3. ``GET /passport/web/get_qrcode/``      → 二维码 PNG + 一次性 token
4. ``GET /passport/web/check_qrconnect/`` → 轮询 new → scanned → confirmed
5. confirmed 后跟 redirect_url            → 拿到 sessionid 等完整 Cookie

二维码 TTL 只有 60 秒，所以面板的「换一张」是重新取码而不是复用旧码。
每个账号的登录会话互相隔离：Cookie 罐、csrf、token、seed 都存在各自的会话对象里；
seed 在换码 / 续期时保持不变，保证设备号稳定、不会被当成新设备。

``check_qrconnect`` 返回 ``error_code=2046``（「为保证账号安全，暂不支持此操作」）
是豆包 passport 的**风控二次验证**，不是二维码状态解析失败。此时会话切到
``verify`` 态并把等待窗口放宽到 10 分钟，用户完成 App / 网页验证后继续轮询同一张码。
"""

from __future__ import annotations

import base64
import gzip
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPMessage

import doubao_spec

from . import cookies as ck

__all__ = [
    "start",
    "status",
    "refresh",
    "cancel",
    "apply_cookie",
    "save_cookie",
    "AUTH_URL",
]

BASE = "https://www.doubao.com"
AID = "497858"
AUTH_URL = "https://www.doubao.com/chat/"

QR_TTL = 55          # 二维码展示倒计时（服务端给 60 秒，留 5 秒余量）
POLL_WINDOW = 180    # 普通轮询窗口：手机扫码后用户可能过一会儿才点确认
SECURITY_VERIFY = 2046  # passport 风控二次验证：为保证账号安全，暂不支持此操作
VERIFY_WINDOW = 600  # 触发 2046 后放宽到 10 分钟，够用户在 App / 网页完成验证
SESSION_TTL = 900    # 登录会话最长保留 15 分钟（含已确认待保存的结果）
POLL_TIMEOUT = 15

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}

_CTX = ssl.create_default_context()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": AUTH_URL,
    "Origin": BASE,
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _serialize(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v not in (None, ""))


def _set_cookies(headers: HTTPMessage | None) -> dict:
    out: dict = {}
    if not headers:
        return out
    for raw in headers.get_all("Set-Cookie") or []:
        part = raw.split(";", 1)[0].strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip():
            out[name.strip()] = value.strip()
    return out


def _body(raw: bytes, headers: HTTPMessage | None) -> str:
    """把 passport 响应体解成可读 UTF-8 文本。

    豆包的 passport 接口偶尔会用 gzip 回包、或把中文按 latin-1 转义，
    直接 ``raw.decode("utf-8", "replace")`` 会把中文变成方块/乱码 —— 这正是
    手机点确认后弹窗里出现乱码的原因。这里统一解压、强制 UTF-8，再做一次
    mojibake 还原；校验层 ``verify.http`` 早已这么处理，这里补齐同一套逻辑。
    """
    if not raw:
        return ""
    if headers and str(headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # 声称 gzip 却不是，按原字节继续解
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 兜底，理论上 decode 不会抛
        text = ""
    return doubao_spec.tidy_text(text)


def _get(
    url: str,
    cookies: dict,
    headers: dict | None = None,
    follow: bool = False,
    timeout: int = POLL_TIMEOUT,
) -> tuple[int, str, dict]:
    """GET；手动跟随 3xx 并收集每一跳的 Set-Cookie。HTTP 错误码不抛，原样返回。"""
    hdr = dict(HEADERS)
    if headers:
        hdr.update(headers)
    found: dict = {}
    current = url
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_CTX),
        NoRedirect,
    )
    for _ in range(10):
        req = urllib.request.Request(current, method="GET")
        for key, value in hdr.items():
            req.add_header(key, value)
        merged = dict(cookies)
        merged.update(found)
        if merged:
            req.add_header("Cookie", _serialize(merged))
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = _body(resp.read(), resp.headers)
                found.update(_set_cookies(resp.headers))
                return resp.status, body, found
        except urllib.error.HTTPError as exc:
            if exc.headers:
                found.update(_set_cookies(exc.headers))
            if follow and exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "") if exc.headers else ""
                if location:
                    current = urllib.parse.urljoin(current, location)
                    continue
            body = ""
            try:
                body = _body(exc.read(), exc.headers)
            except Exception:  # noqa: BLE001
                pass
            return exc.code, body, found
        except Exception as exc:  # noqa: BLE001 - 网络抖动交给调用方翻译
            return 0, str(exc), found
    return 0, "重定向次数过多", found


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _passport_query(cookies: dict, **extra) -> str:
    """拼一份贴近浏览器 js-sdk 的 passport 公共参数。

    真实登录弹窗还会带 `sign` / `qs` / `a_bogus`，但扫码链路不校验它们；
    实测补上这些基础参数与 `fp` / `verifyFp` 后，get_qrcode / check_qrconnect
    行为一致，同时更接近浏览器会话，减少被当成裸脚本拦下的概率。
    """
    params = {
        "passport_jssdk_version": "4.4.6",
        "passport_jssdk_type": "normal",
        "is_from_ttaccountsdk": "1",
        "aid": AID,
        "language": "zh",
        "account_app_language": "zh-CN",
        "multi_login": "1",
        "account_sdk_source": "web",
        "device_platform": "web",
        "os_type": "3",
        "terminal_type": "2",
    }
    fp = str(cookies.get("s_v_web_id") or "")
    if fp:
        params["fp"] = fp
        params["verifyFp"] = fp
    biz = str(cookies.get("biz_trace_id") or "")
    if biz:
        params["biz_trace_id"] = biz
    params.update(extra)
    return urllib.parse.urlencode(params)


def _csrf(cookies: dict) -> str:
    # 首页是 302，跟随每一跳把 ttwid / web_id 都收进来（只取第一跳会漏 Cookie）
    _, _body, new = _get(BASE + "/", cookies, follow=True)
    cookies.update(new)
    for name in ("passport_csrf_token", "passport_csrf_token_default"):
        if cookies.get(name):
            return cookies[name]
    _, body, new = _get(f"{BASE}/passport/safe/csrf_token/?aid={AID}", cookies)
    cookies.update(new)
    data = _json(body) or {}
    inner = data.get("data") or {}
    # 浏览器在打开 chat 页时就有这两个值；裸脚本拿二维码时服务端也会下发，
    # 但先备好可以让首张二维码的请求就跟正常浏览器会话一致。
    if not cookies.get("s_v_web_id"):
        cookies["s_v_web_id"] = "verify_" + "".join(
            secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789_") for _ in range(27)
        )
    if not cookies.get("biz_trace_id"):
        cookies["biz_trace_id"] = secrets.token_hex(4)
    return str(inner.get("passport_csrf_token") or cookies.get("passport_csrf_token") or "")


def _qrcode(cookies: dict, csrf: str) -> tuple[str, str, int]:
    """返回 (token, data URI, 有效期秒)。"""
    query = _passport_query(cookies, next=BASE)
    url = f"{BASE}/passport/web/get_qrcode/?{query}"
    _, body, new = _get(
        url,
        cookies,
        headers={"x-tt-passport-csrf-token": csrf},
    )
    cookies.update(new)
    data = _json(body) or {}
    inner = data.get("data") or {}
    # 注意：error_code 合法值就是 0，不能写成 `inner.get(...) or -1`——
    # 那样会把正常的 0 当成假值，误判成「获取失败」（NAS 上拿不到二维码的根因）。
    raw_code = inner.get("error_code", data.get("error_code", -1))
    code = int(raw_code) if raw_code not in (None, "") else -1
    if code != 0:
        raise ValueError(f"获取豆包二维码失败（error_code={code}）：{inner.get('description') or data.get('message') or ''}")
    token = str(inner.get("token") or "")
    if not token:
        raise ValueError("豆包二维码响应里没有 token，请稍后重试")
    raw = str(inner.get("qrcode") or inner.get("qrcode_url") or "")
    if not raw:
        raise ValueError("豆包二维码响应里没有图片，请稍后重试")
    if raw.startswith("data:"):
        uri = raw
    elif raw.startswith("http"):
        # 少数情况下给的是图片 URL，直接拉回来再转 data URI
        _, img, _ = _get(raw, {})
        try:
            b64 = base64.b64encode(img.encode("latin-1", errors="ignore")).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("豆包二维码图片无法读取") from exc
        uri = "data:image/png;base64," + b64
    else:
        uri = "data:image/png;base64," + raw.strip()
    ttl = int(inner.get("expire_time") or QR_TTL)
    if ttl > 3600:  # 服务端给的是毫秒或绝对时间戳时按默认 TTL 处理
        ttl = QR_TTL
    return token, uri, min(ttl, QR_TTL)


def _cleanup() -> None:
    now = time.time()
    for sid in [sid for sid, one in _SESSIONS.items() if now - one["created"] > SESSION_TTL]:
        _SESSIONS.pop(sid, None)


def start() -> dict:
    """开一个扫码会话，返回二维码给前端展示。"""
    cookies: dict = {}
    csrf = _csrf(cookies)
    token, uri, ttl = _qrcode(cookies, csrf)
    sid = secrets.token_urlsafe(12)
    with _LOCK:
        _cleanup()
        _SESSIONS[sid] = {
            "id": sid,
            "cookies": cookies,
            "csrf": csrf,
            "token": token,
            "qrcode": uri,
            "ttl": ttl,
            "created": time.time(),
            "qr_at": time.time(),
            "status": "waiting",
            "seed": secrets.token_hex(16),
            "state": "new",
            "verify_at": None,
            "security_code": None,
        }
    return {
        "id": sid,
        "qrcode": uri,
        "status": "waiting",
        "expires_in": ttl,
        "auth_url": AUTH_URL,
    }


def _find(sid: str) -> dict:
    _cleanup()
    one = _SESSIONS.get(sid)
    if not one:
        raise ValueError("扫码会话不存在或已过期，请重新获取二维码")
    return one


def status(sid: str) -> dict:
    """轮询扫码状态；confirmed 时返回 cookie + seed（只给前端，不自动落盘）。"""
    with _LOCK:
        one = _find(sid)
        if one["status"] == "confirmed":
            return {
                "status": "confirmed",
                "cookie": one["cookie"],
                "seed": one["seed"],
                "expires_in": 0,
            }
        # 注意：二维码的 55 秒只是「展示倒计时」，不是轮询上限。
        # 手机扫码后用户往往还要停留几秒才点确认；以前这里按 ttl 直接判过期，
        # 导致手机确认了、面板却早已停止查询，表现为「点了确认没反应」。
        # 触发 2046 风控时窗口再放宽到 VERIFY_WINDOW，给用户完成二次验证的时间。
        if one["status"] == "expired":
            return {"status": "expired", "expires_in": 0, "message": "二维码已过期，请点「换一张」"}
        verifying = one["status"] == "verify"
        window = VERIFY_WINDOW if verifying else POLL_WINDOW
        origin = one.get("verify_at") or one["qr_at"]
        elapsed = time.time() - origin
        if elapsed > window:
            one["status"] = "expired"
            return {"status": "expired", "expires_in": 0, "message": "扫码等待超时，请点「换一张」重新扫码"}
        token = one["token"]
        csrf = one["csrf"]
        cookies = dict(one["cookies"])
        had_sessionid = bool(cookies.get("sessionid"))
        ttl = one["ttl"]
        qr_at = one["qr_at"]

    # 网络请求不持锁：轮询等接口时，「取消 / 换码」仍能立刻响应。
    query = _passport_query(cookies, next=BASE, token=token)
    url = f"{BASE}/passport/web/check_qrconnect/?{query}"
    http_code, body, new = _get(url, cookies, headers={"x-tt-passport-csrf-token": csrf})
    data = _json(body)
    # 网络失败 / 非 JSON 响应必须显式报 error。以前静默当 new 处理，前端会
    # 一直停在「等待扫码」，手机确认了也看不出来，正是「没反应」的假象来源。
    net_error = ""
    if http_code == 0:
        net_error = f"网络请求失败：{str(body)[:120]}"
    elif not isinstance(data, dict):
        net_error = f"passport 返回异常（HTTP {http_code}）"

    with _LOCK:
        one = _SESSIONS.get(sid)
        if not one:
            return {"status": "cancelled", "expires_in": 0, "message": "扫码已取消"}
        one["cookies"].update(new)
        ttl = one["ttl"]
        qr_at = one["qr_at"]
        verifying = one["status"] == "verify"
        window = VERIFY_WINDOW if verifying else POLL_WINDOW
        origin = one.get("verify_at") or qr_at
        remaining = max(0, int(window - (time.time() - origin)))

        if net_error:
            # 不立即判死：连续失败由前端 failStreak 计数，偶发抖动可自恢复。
            return {"status": one["status"], "expires_in": remaining, "message": net_error}

        inner = data.get("data") or {}
        raw_code = inner.get("error_code", data.get("error_code", 0))
        code = int(raw_code) if raw_code not in (None, "") else 0
        desc = doubao_spec.tidy_text(inner.get("description") or data.get("message") or "")
        state = str(inner.get("status") or "")
        one["state"] = state
        redirect = str(inner.get("redirect_url") or "")

        # 每次轮询都把 passport 原始字段回给前端展示。手机点了确认却卡住时，
        # 面板会直接显示服务端回的 status / code / 是否已下发 sessionid，
        # 不用再靠猜。这是定位「确认了但没反应」的关键诊断。
        debug = {
            "status": state or "空",
            "code": code,
            "http": http_code,
            "redirect": bool(redirect),
            "sessionid": bool(one["cookies"].get("sessionid")),
            "new_sid": bool(new.get("sessionid")),
            "desc": desc[:120],
        }
        one["debug"] = debug

        # 2046 = 豆包 passport 风控二次验证（实测文案：
        # 「为保证账号安全，暂不支持此操作」/「请前往豆包 App 完成验证」）。
        # 它不是二维码解析失败，也不能靠换码解决；保持同一 token 继续轮询，
        # 用户在手机 App 或浏览器完成验证后服务端就会放行本次登录。
        if code == SECURITY_VERIFY:
            one["status"] = "verify"
            one["verify_at"] = time.time()
            one["security_code"] = code
            remaining = VERIFY_WINDOW
            msg = (
                f"豆包要求安全验证（code={code}）：请在手机豆包 App 里完成安全验证，"
                "或在浏览器打开豆包网页用短信 / 扫码登录一次；完成后面板会自动继续。"
                "若一直不通过，请改用下面的手动填 Cookie。"
            )
            if desc:
                msg += f"（passport：{desc[:80]}）"
            return {"status": "verify", "expires_in": remaining, "message": msg, "debug": debug}

        if code != 0:
            if "expired" in desc.lower() or "过期" in desc:
                one["status"] = "expired"
            return {
                "status": one["status"], "expires_in": remaining,
                "message": desc or f"passport 返回 error_code={code}", "debug": debug,
            }

        # 风控解除后 passport 会恢复正常的 new / scanned / confirmed 流程。
        if one["status"] == "verify":
            one["status"] = "waiting"
            one["verify_at"] = None

        # 状态单向：confirmed 不可逆。并发轮询时，确认前发出的请求可能稍后
        # 才返回 scanned，若照单赋值会把已确认状态覆盖回去，前端就一直显示
        # 「已扫码请确认」，表现为手机确认了面板却没反应。
        if one["status"] == "confirmed":
            return {
                "status": "confirmed",
                "cookie": one["cookie"],
                "seed": one["seed"],
                "expires_in": 0,
                "debug": debug,
            }

        # 手机确认的三种信号（任一命中即视为已登录）：
        #   1. status 是 confirmed/confirm/success/ok 及其变体
        #   2. passport 给了 redirect_url
        #   3. 本次轮询新下发了 sessionid（之前没有、现在有）
        fresh_sid = bool(new.get("sessionid")) or (
            not had_sessionid and bool(one["cookies"].get("sessionid"))
        )
        def _is_confirmed() -> bool:
            return (
                "confirm" in state.lower() or state.lower() in ("success", "ok")
                or bool(redirect) or fresh_sid
            )

        if not _is_confirmed():
            raw_tag = f"[passport: status={state or '空'}, code={code}]"
            if state == "new":
                one["status"] = "waiting"
                msg = "等待扫码（请用手机豆包 App 扫描）"
            elif state == "scanned":
                one["status"] = "scanned"
                msg = "已扫码，请在手机上点「确认登录」"
            elif state == "expired":
                one["status"] = "expired"
                msg = "二维码已过期，请点「换一张」"
            else:
                msg = f"扫码状态：{state or '未知'}"
            return {
                "status": one["status"], "expires_in": remaining,
                "message": f"{msg} {raw_tag}", "debug": debug,
            }

        # ---- 已确认：导出 Cookie ----
        # confirmed 响应自身往往已带 sessionid；redirect 只是补充。跟进失败不再
        # 否定已确认的登录态。
        hop_error = ""
        if redirect:
            try:
                _, _, hop = _get(redirect, one["cookies"], follow=True, timeout=20)
                one["cookies"].update(hop)
            except Exception as exc:  # noqa: BLE001 - redirect 失败不否定已确认的登录态
                hop_error = str(exc)
        if not one["cookies"].get("sessionid"):
            # 再试一次 csrf_token 接口：有些账号的 sessionid 只在 passport 域的
            # 后续响应里下发，确认当下不一定立刻出现在 Cookie 罐里。
            try:
                _, _, extra = _get(
                    f"{BASE}/passport/safe/csrf_token/?aid={AID}",
                    one["cookies"],
                    headers={"x-tt-passport-csrf-token": csrf},
                )
                one["cookies"].update(extra)
            except Exception:  # noqa: BLE001
                pass
        debug["sessionid"] = bool(one["cookies"].get("sessionid"))

        pairs = [(k, v) for k, v in one["cookies"].items() if k and v]
        one["cookie"] = ck.build(pairs)
        one["status"] = "confirmed"
        if not one["cookies"].get("sessionid"):
            # 没拿到 sessionid 也让按钮可用、结果可见：让用户能保存并在面板上
            # 继续验证，而不是卡死在 scanned。诊断字段会说明缺 sessionid。
            got = ",".join(sorted(one["cookies"])) or "无"
            detail = f"；redirect 异常：{hop_error}" if hop_error else ""
            msg = f"已确认，但 passport 未下发 sessionid（已有：{got}{detail}），可先保存再校验"
        else:
            msg = "登录成功，点「保存」或「续期」生效"
        return {
            "status": "confirmed",
            "cookie": one["cookie"],
            "seed": one["seed"],
            "expires_in": 0,
            "message": msg,
            "debug": debug,
        }


def refresh(sid: str) -> dict:
    """换一张二维码。沿用同一会话的 Cookie 与 seed（续期时设备号不变）。"""
    with _LOCK:
        one = _find(sid)
        if one["status"] == "confirmed":
            raise ValueError("这个会话已经登录成功，不用换码")
        csrf = _csrf(one["cookies"])
        one["csrf"] = csrf
        token, uri, ttl = _qrcode(one["cookies"], csrf)
        one.update({
            "token": token, "qrcode": uri, "ttl": ttl, "qr_at": time.time(),
            "status": "waiting", "verify_at": None, "security_code": None,
        })
        return {"id": sid, "qrcode": uri, "status": "waiting", "expires_in": ttl, "auth_url": AUTH_URL}


def cancel(sid: str) -> dict:
    with _LOCK:
        _SESSIONS.pop(sid, None)
    return {"ok": True}


def save_cookie(cookie: str, seed: str, index: int | None = None) -> dict:
    """把 Cookie 应用到豆包账号池：index=None 新增，否则续期指定账号。

    扫码登录与浏览器登录共用这一份落盘逻辑，避免两条通道写出不同格式。
    """
    from . import runner, store

    data = store.load()
    accounts = store.accounts(data, "DOUBAO")
    if index is None:
        accounts.append({"cookie": cookie, "seed": seed})
        where = "新增"
    else:
        if not isinstance(index, int) or index < 0 or index >= len(accounts):
            raise ValueError("账号序号不存在")
        # 只动 cookie/seed 两个键，其它账号与其它字段原样保留 —— 多账号隔离的关键。
        accounts[index]["cookie"] = cookie
        accounts[index]["seed"] = seed
        where = f"第 {index + 1} 个账号续期"
    store.set_accounts(data, "DOUBAO", accounts)
    store.write(data)
    runner.clear_site_status("DOUBAO", "登录态已更新，等待重新校验")
    return {"ok": True, "count": len(accounts), "where": where}


def apply_cookie(sid: str, index: int | None = None) -> dict:
    """把已确认会话的 Cookie 应用到豆包账号：index=None 新增，否则续期指定账号。"""
    with _LOCK:
        one = _SESSIONS.get(sid)
        if not one or one.get("status") != "confirmed":
            raise ValueError("扫码还没完成，请先确认登录")
        cookie, seed = one["cookie"], one["seed"]
    return save_cookie(cookie, seed, index)
