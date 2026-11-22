"""首次启动把无头浏览器下载到持久化卷，之后一直复用。

镜像不再内置 chromium：基础镜像 + 系统库只有几十 MB，真正的浏览器
（chrome-headless-shell，约 120 MiB）在容器第一次启动时下载到
``$DC_HOME/data/browser/``。``data`` 是挂载卷，删容器不会删卷，
所以后续启动、换镜像版本都直接复用，不用重复下载。

设计要点：

* 默认版本固定、带 SHA256 校验，避免镜像源被污染或拿到不完整文件；
* 主源用国内可达的 npmmirror，失败自动回退 Google 官方源；
* 下载到临时目录、校验通过再原子替换，中途失败不破坏已有浏览器；
* 任何失败都只告警并返回 False，不阻塞面板启动 —— 手动 Cookie 仍可用；
* 支持 x86_64 / arm64 两种架构；已有系统浏览器或 ``DOUBAO_BROWSER``
  指向的浏览器时直接跳过下载。
"""

from __future__ import annotations

import hashlib
import os
import platform
import secrets
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import paths

__all__ = [
    "DEFAULT_VERSION", "installed_path", "ensure", "platform_key", "target_dir",
]

# 固定版本 + 两个架构的 zip sha256（从官方源实际下载后计算）
DEFAULT_VERSION = "158.0.8127.36"
_CHECKSUMS = {
    "linux64": "2d3a23017323c54fd457a678e4351f895c4076d8050e39d5aa6247f93448b73a",
    "linux-arm64": "176ee5c55486c22de43128871189061365ea28e881fc96016fd18e7cb59437c8",
}
_MIRRORS = (
    "https://cdn.npmmirror.com/binaries/chrome-for-testing/{v}/{p}/chrome-headless-shell-{p}.zip",
    "https://storage.googleapis.com/chrome-for-testing-public/{v}/{p}/chrome-headless-shell-{p}.zip",
)
_BIN = "chrome-headless-shell"
_ARCH_MAP = {
    "x86_64": "linux64", "amd64": "linux64",
    "aarch64": "linux-arm64", "arm64": "linux-arm64",
}
_TIMEOUT = 60            # 单次连接/读取超时（秒）
_CHUNK = 1 << 20         # 1 MiB


def version() -> str:
    return os.environ.get("DOUBAO_BROWSER_VERSION", "").strip() or DEFAULT_VERSION


def platform_key() -> str:
    machine = platform.machine().lower()
    key = _ARCH_MAP.get(machine)
    if not key:
        raise RuntimeError(f"不支持的 CPU 架构：{machine}（只支持 x86_64 / arm64）")
    return key


def target_dir() -> Path:
    """浏览器持久目录。默认在 data 卷里 —— 删容器不删卷就不丢。"""
    custom = os.environ.get("DC_BROWSER_DIR", "").strip()
    base = Path(custom) if custom else Path(paths.DATA_DIR) / "browser"
    return base / f"chrome-headless-shell-{platform_key()}"


def installed_path() -> Path | None:
    """已下载好的可执行文件路径；没有或不完整返回 None。"""
    try:
        exe = target_dir() / _BIN
    except RuntimeError:
        return None
    if exe.is_file() and os.access(exe, os.X_OK):
        return exe
    return None


def _system_browser() -> str:
    custom = os.environ.get("DOUBAO_BROWSER", "").strip()
    if custom:
        if os.path.isfile(custom) and os.access(custom, os.X_OK):
            return custom
        return ""
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path, log) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "dailycheckin-plus-browser/1"})
    downloaded = 0
    next_report = 16  # MiB
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp, dest.open("wb") as out:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            mib = downloaded / (1 << 20)
            if mib >= next_report:
                log(f"  已下载 {mib:.0f} MiB …")
                next_report += 16


def _fetch_zip(plat: str, ver: str, work: Path, log) -> Path:
    """依次尝试各镜像源，返回校验通过的 zip。"""
    expected = _CHECKSUMS.get(plat)
    if ver != DEFAULT_VERSION:
        expected = os.environ.get("DOUBAO_BROWSER_SHA256", "").strip() or None
        if expected is None:
            log(f"  注意：非默认版本 {ver} 且未提供 DOUBAO_BROWSER_SHA256，跳过校验")
    last_error: Exception | None = None
    for template in _MIRRORS:
        url = template.format(v=ver, p=plat)
        source = "npmmirror" if "npmmirror" in url else "官方源"
        zip_path = work / "download.zip"
        try:
            log(f"  从 {source} 下载 …")
            _download(url, zip_path, log)
            if expected:
                actual = _sha256(zip_path)
                if actual != expected:
                    raise RuntimeError(f"SHA256 不匹配：期望 {expected[:12]}…，实际 {actual[:12]}…")
            elif not zipfile.is_zipfile(zip_path):
                raise RuntimeError("下载内容不是有效的 zip")
            return zip_path
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            last_error = exc
            log(f"  {source} 失败：{exc}")
            zip_path.unlink(missing_ok=True)
    raise RuntimeError(f"所有镜像源都下载失败：{last_error}")


def _stage(zip_path: Path, work: Path, plat: str) -> Path:
    """解压并确认二进制存在且可执行，返回待安装的目录。"""
    unpack = work / "unpacked"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(unpack)
    staged = unpack / f"chrome-headless-shell-{plat}"
    exe = staged / _BIN
    if not exe.is_file():
        raise RuntimeError(f"压缩包里找不到 {_BIN}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return staged


def ensure(log=print, force: bool = False) -> tuple[bool, str]:
    """确保持久化卷里有可用的浏览器。失败不抛异常，交给面板降级。"""
    try:
        if _system_browser():
            return True, "已有系统浏览器，跳过下载"
        current = installed_path()
        if current and not force:
            return True, f"复用已下载的浏览器：{current}"

        plat = platform_key()
        ver = version()
        work = Path(paths.DATA_DIR) / "browser" / (".download-" + secrets.token_hex(4))
        work.mkdir(parents=True, exist_ok=True)
        final = target_dir()
        backup: Path | None = None
        try:
            zip_path = _fetch_zip(plat, ver, work, log)
            staged = _stage(zip_path, work, plat)
            if final.exists():
                backup = final.with_name(final.name + ".old-" + secrets.token_hex(3))
                final.rename(backup)
            try:
                final.parent.mkdir(parents=True, exist_ok=True)
                staged.rename(final)
            except Exception:
                if backup is not None and backup.exists():
                    backup.rename(final)
                raise
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            exe = final / _BIN
            return (exe.is_file() and os.access(exe, os.X_OK)), f"浏览器已就绪：{exe}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - 任何失败都不阻塞面板
        return False, str(exc)


def main() -> int:
    def log(msg: str) -> None:
        print(f"[浏览器] {msg}", flush=True)

    ok, message = ensure(log=log, force=os.environ.get("DOUBAO_BROWSER_FORCE") == "1")
    if ok:
        log(message)
    else:
        log(f"浏览器不可用：{message}")
        log("扫码登录入口不可用；仍可在站点配置里手动粘贴 Cookie。")
    # 恒返回 0：浏览器是可选能力，下载失败不能让容器启动失败。
    return 0


if __name__ == "__main__":
    sys.exit(main())
