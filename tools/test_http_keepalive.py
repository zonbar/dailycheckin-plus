#!/usr/bin/env python3
"""HTTP keep-alive 回归自测（本地起服务，纯标准库）。

背景 bug：/api/backups/<name>/restore 是 POST 且前端带 `body: {}`，但这个
处理函数不读请求体。HTTP/1.1 keep-alive 下未读字节残留在 socket 上，浏览器
复用同一连接发下一条请求（恢复成功后自动刷新 /api/state）时，http.server
把残留字节当新请求行解析 → 501 Unsupported method。用户看到「恢复成功但
页面报 501」。

锁三件事：
1. 带 body 但不消费的 POST 之后，同一连接的下一条请求必须正常（不 501）。
2. 未定义的方法（DELETE/PATCH/OPTIONS）返回 JSON 404，而不是裸 501 HTML。
3. 正常读 body 的 POST（如 config/raw PUT）语义不被预读破坏。

跑法：python3 tools/test_http_keepalive.py
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f"  :: {detail}" if not cond else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="dc-keepalive-")
    port = 19087
    try:
        os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "data", "backups"), exist_ok=True)
        with open(os.path.join(tmp, "config", "config.json"), "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        with open(os.path.join(tmp, "data", "backups", "config-20260101-000000.json"), "w", encoding="utf-8") as f:
            f.write('{"a": 2, "restored": true}')

        os.environ.update(
            DC_HOME=tmp,
            PANEL_TOKEN="tok",
            PANEL_PORT=str(port),
            PANEL_HOST="127.0.0.1",
        )
        from panel import server

        threading.Thread(target=server.serve, daemon=True).start()
        for _ in range(40):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/api/health", headers={"X-Panel-Token": "tok"})
                c.getresponse().read()
                c.close()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)

        hdr = {"X-Panel-Token": "tok", "Content-Type": "application/json"}
        name = "config-20260101-000000.json"

        # 1) 同一 keep-alive 连接：恢复（不读 body）→ 刷新状态
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"/api/backups/{name}/restore", body="{}", headers=hdr)
        r1 = conn.getresponse()
        b1 = r1.read()
        check("恢复接口返回 200", r1.status == 200, f"{r1.status} {b1[:80]}")

        conn.request("GET", "/api/state", headers=hdr)
        r2 = conn.getresponse()
        b2 = r2.read()
        check("恢复后同连接刷新状态不是 501", r2.status == 200, f"{r2.status} {b2[:80]}")
        try:
            state = json.loads(b2)
            check("状态响应是合法 JSON", isinstance(state, dict) and "panel" in state)
        except Exception as exc:  # noqa: BLE001
            check("状态响应是合法 JSON", False, str(exc))
        conn.close()

        # 恢复确实把配置改成了备份内容（要在下面的 PUT 覆盖之前检查）
        with open(os.path.join(tmp, "config", "config.json"), encoding="utf-8") as f:
            restored = json.load(f)
        check("恢复后配置内容为备份值", restored.get("restored") is True, str(restored)[:80])

        # 2) 未定义方法返回 JSON 404，不是裸 501 HTML
        for method in ("DELETE", "PATCH", "OPTIONS"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, "/api/state", body="{}", headers=hdr)
            r = c.getresponse()
            body = r.read()
            ok = r.status == 404
            try:
                parsed = json.loads(body)
                ok = ok and "error" in parsed
            except Exception:  # noqa: BLE001
                ok = False
            check(f"{method} 返回 JSON 404（不是裸 501）", ok, f"{r.status} {body[:60]}")
            c.close()

        # 3) 正常读 body 的 PUT 没被预读破坏，且其后同连接继续正常
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        raw = json.dumps({"text": '{"a": 1, "kept": true}'})
        c.request("PUT", "/api/config/raw", body=raw, headers=hdr)
        r = c.getresponse()
        body = r.read()
        check("PUT /api/config/raw 正常（body 预读未破坏语义）", r.status == 200, f"{r.status} {body[:80]}")
        c.request("GET", "/api/health", headers=hdr)
        r2 = c.getresponse()
        r2.read()
        check("PUT 后同连接仍正常", r2.status == 200, str(r2.status))
        c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 66)
    print(f"通过 {len(passed)}  失败 {len(failed)}")
    for one in failed:
        print("  ✗", one)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
