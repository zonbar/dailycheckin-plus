"""IDE 类签到公共规格：账号种子、设备指纹派生、跨进程账号锁。

只依赖标准库。引擎里的 Trae / Qoder / MiniMax Code / Copilot 国际版模块
共用这里的规则；不同账号用不同种子派生设备号，避免多账号互相串线。

设备派生算法与参考实现逐字节一致：SHA-256(salt + ":" + seed + uint32BE(i))
串接后截取所需字节。设备号必须稳定，随机生成会让服务端按设备维度风控拒签。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
import time
import uuid
from contextlib import contextmanager

try:  # Windows / 精简环境没有 fcntl，退化成不加锁，功能不受影响
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

LOCK_TIMEOUT = 120


def seed_of(account: dict, *keys: str) -> str:
    """显式 seed 优先；否则按该站点的身份字段生成稳定种子。"""
    one = account or {}
    seed = str(one.get("seed") or "").strip()
    if seed:
        return seed
    raw = "|".join(str(one.get(k) or "") for k in keys if k)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def seeded_bytes(seed: str, salt: str, nbytes: int) -> bytes:
    """SHA-256(salt:seed || uint32BE(i)) 串接，取前 nbytes 字节。"""
    base = (salt + ":" + str(seed or "")).encode("utf-8")
    out = bytearray()
    index = 0
    while len(out) < nbytes:
        out.extend(hashlib.sha256(base + struct.pack(">I", index)).digest())
        index += 1
    return bytes(out[: max(0, nbytes)])


def digits(seed: str, salt: str, n: int) -> str:
    return "".join(str(b % 10) for b in seeded_bytes(seed, salt, n))


def hex_string(seed: str, salt: str, n: int) -> str:
    raw = seeded_bytes(seed, salt, (n + 1) // 2)
    return raw.hex()[:n]


def uuid_from_seed(seed: str, salt: str) -> str:
    """按种子生成标准 UUID v4（版本位/变体位按 RFC 4122 置位）。"""
    b = bytearray(seeded_bytes(seed, salt, 16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    raw = bytes(b)
    return "{}-{}-{}-{}-{}".format(
        raw[0:4].hex(), raw[4:6].hex(), raw[6:8].hex(),
        raw[8:10].hex(), raw[10:16].hex(),
    )


def request_id() -> str:
    return str(uuid.uuid4())


def jwt_payload(jwt: str) -> dict:
    """解 JWT payload（不校验签名，只用来取 user id / 过期时间）。"""
    parts = str(jwt or "").strip().removeprefix("Cloud-IDE-JWT ").split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(seg.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 - 不是 JWT 就当没有 payload
        return {}


def jwt_user_id(jwt: str) -> str:
    """Trae JWT 的 user id 可能在 data.id / auth_id / sub，按顺序取。"""
    payload = jwt_payload(jwt)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("id", "user_id", "uid"):
            value = str(data.get(key) or "").strip()
            if value and value != "0":
                return value
    for key in ("auth_id", "user_id", "uid", "sub"):
        value = str(payload.get(key) or "").strip()
        if value and value != "0":
            return value
    return ""


def trae_device(account: dict, seed: str) -> dict:
    """按账号生成 Trae 设备三件套；账号显式写入的值优先。"""
    one = account or {}
    return {
        "device_id": str(one.get("device_id") or "").strip() or digits(seed, "devid", 15),
        "session_id": str(one.get("session_id") or "").strip() or hex_string(seed, "sess", 64),
        "market_user_id": str(one.get("market_user_id") or "").strip()
        or uuid_from_seed(seed, "market"),
    }


def qoder_device(account: dict, seed: str) -> dict:
    """Qoder 机器指纹；显式配置的真实指纹优先，否则按账号确定性派生。"""
    one = account or {}
    key = str(one.get("email") or one.get("user_id") or seed or "").strip().lower()
    machine_id = str(one.get("machine_id") or "").strip() or hex_string(seed, "qd-machine", 32)
    token = str(one.get("machine_token") or "").strip()
    if not token:
        token = base64.urlsafe_b64encode(
            hashlib.sha256(("qoder-token:" + key + ":" + machine_id).encode("utf-8")).digest()
        ).decode("ascii").rstrip("=")
    code = str(one.get("machine_code") or "").strip() or hashlib.sha256(
        ("qoder-code:" + key).encode("utf-8")
    ).hexdigest()[:32]
    return {
        "client_type": str(one.get("client_type") or "10").strip(),
        "machine_os": str(one.get("machine_os") or "x86_64_windows").strip(),
        "machine_hostname": str(one.get("machine_hostname") or "").strip()
        or hex_string(seed, "qd-host", 16),
        "machine_id": machine_id,
        "machine_token": token,
        "machine_code": code,
        "machine_type": str(one.get("machine_type") or "1").strip(),
        "version": str(one.get("version") or "1.0.0").strip(),
    }


def _lock_dir() -> str:
    home = os.environ.get("DC_HOME", "").strip()
    base = (
        os.path.join(home, "data", "locks")
        if home
        else os.path.join(tempfile.gettempdir(), "dailycheckin-ide-locks")
    )
    os.makedirs(base, exist_ok=True)
    return base


@contextmanager
def account_lock(site: str, seed: str, timeout: float = LOCK_TIMEOUT):
    """同一站点同一账号串行签到，避免手动点击与定时任务重复打写接口。"""
    digest = hashlib.sha256((site + ":" + str(seed or "")).encode("utf-8")).hexdigest()
    path = os.path.join(_lock_dir(), digest[:32] + ".lock")
    with open(path, "a+") as fh:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("同一账号的签到正在进行中，请稍后再试")
                    time.sleep(0.1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
