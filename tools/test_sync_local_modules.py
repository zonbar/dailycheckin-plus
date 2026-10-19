#!/usr/bin/env python3
"""本地站点模块的同步保护自测（离线，纯标准库，不联网）。

要锁的是一件很具体、而且**后果很严重**的事：

`tools/sync_upstream.py` 的 `copy_tree()` 落地引擎时是先
`shutil.rmtree(dailycheckin/)` 再 copytree。也就是说，
本项目自己新增、但上游没有的站点目录（`dailycheckin/weibo/`），
**在下一次 `sync_upstream.py` 同步时会无声消失** —— 而且 `git status` 里只表现为
一个删除，没有任何报错提示，很容易被误提交上去。

所以这里直接拿真实的 rmtree + restore 流程做一次端到端演练，
确认 LOCAL_SITE_MODULES 登记过的目录能活下来，且上游的目录仍会被正常覆盖。

跑法：python3 tools/test_sync_local_modules.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL]'}]   {name}" + (f":: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)


# --------------------------------------------------------------------------- #
section("0) 常量与真实仓库状态")

import sync_upstream as su  # noqa: E402

check("LOCAL_SITE_MODULES 已登记 weibo", "weibo" in su.LOCAL_SITE_MODULES, str(su.LOCAL_SITE_MODULES))
check("dailycheckin/weibo/main.py 真实存在",
      os.path.isfile(os.path.join(ROOT, "dailycheckin", "weibo", "main.py")))
check("dailycheckin/weibo/__init__.py 真实存在",
      os.path.isfile(os.path.join(ROOT, "dailycheckin", "weibo", "__init__.py")))

# 登记的每个本地模块都必须在磁盘上真实存在（防止加了名字但忘了建目录）
for name in sorted(su.LOCAL_SITE_MODULES):
    check(f"登记的 {name}/ 在磁盘上存在", os.path.isdir(os.path.join(ROOT, "dailycheckin", name)))

# --------------------------------------------------------------------------- #
section("1) 端到端演练：rmtree 之后本地模块还能活下来")

with tempfile.TemporaryDirectory(prefix="dc-sync-test-") as tmp:
    backup = os.path.join(tmp, "_backup_engine")
    fake_upstream = os.path.join(tmp, "upstream_engine")

    # 造一个「同步前」的引擎目录：上游的模块 + 本地的 weibo
    os.makedirs(backup)
    for name in ("bilibili", "tieba", "weibo"):
        os.makedirs(os.path.join(backup, name))
        with open(os.path.join(backup, name, "main.py"), "w", encoding="utf-8") as f:
            f.write("MARKER = %r\n" % name)
    # 每个站点目录都必须有 __init__.py，缺了就不是一个可导入的包
    for name in ("bilibili", "tieba", "weibo"):
        open(os.path.join(backup, name, "__init__.py"), "w", encoding="utf-8").close()
    with open(os.path.join(backup, "weibo", "main.py"), "w", encoding="utf-8") as f:
        f.write("# 本地模块，upstream 没有\nMARKER = 'weibo-local'\n")

    # 造一个「上游」引擎目录：只有 bilibili/tieba，没有 weibo
    os.makedirs(fake_upstream)
    for name in ("bilibili", "tieba"):
        os.makedirs(os.path.join(fake_upstream, name))
        open(os.path.join(fake_upstream, name, "__init__.py"), "w", encoding="utf-8").close()
        with open(os.path.join(fake_upstream, name, "main.py"), "w", encoding="utf-8") as f:
            f.write("# upstream new version\nMARKER = %r\n" % name)

    # 把 su.ENGINE_DIR 临时指到演练目录
    real_engine_dir = su.ENGINE_DIR
    su.ENGINE_DIR = os.path.join(tmp, "dailycheckin")
    try:
        # 1) 复刻 copy_tree 的破坏性行为：整个目录被删掉重铺
        su.copy_tree(fake_upstream, su.ENGINE_DIR)
        check("rmtree 后 weibo 确实消失了（这正是要防的坑）",
              not os.path.isdir(os.path.join(su.ENGINE_DIR, "weibo")))

        # 2) 再跑保护逻辑
        restored = su.preserve_local_modules(backup)
        check("preserve_local_modules 恢复了 weibo", "weibo" in restored, str(restored))
        weibo_main = os.path.join(su.ENGINE_DIR, "weibo", "main.py")
        check("weibo/main.py 恢复成功", os.path.isfile(weibo_main))
        if os.path.isfile(weibo_main):
            content = open(weibo_main, encoding="utf-8").read()
            check("恢复的是本地内容（不是上游的）", "weibo-local" in content, content[:120])
            check("__init__.py 也一并恢复", os.path.isfile(os.path.join(su.ENGINE_DIR, "weibo", "__init__.py")))

        # 3) 上游的模块仍然是新版本（没被备份覆盖回去）
        bili = open(os.path.join(su.ENGINE_DIR, "bilibili", "main.py"), encoding="utf-8").read()
        check("上游模块仍是被同步后的新版本", "upstream new version" in bili, bili[:120])

        # 4) 权限归一化：源码 0644、目录 0755（同步脚本的既有约定）
        mode = os.stat(weibo_main).st_mode & 0o777
        check("恢复的文件权限是 0644", mode == 0o644, oct(mode))
        dmode = os.stat(os.path.join(su.ENGINE_DIR, "weibo")).st_mode & 0o777
        check("恢复的目录权限是 0755", dmode == 0o755, oct(dmode))

        # 5) 重复执行不报错（幂等）
        restored2 = su.preserve_local_modules(backup)
        check("重复执行仍然恢复 weibo", "weibo" in restored2, str(restored2))
        content2 = open(weibo_main, encoding="utf-8").read()
        check("重复执行后内容没变", "weibo-local" in content2)

        # 6) 备份里没有该模块时安静跳过，不抛异常
        empty_backup = os.path.join(tmp, "empty_backup")
        os.makedirs(empty_backup)
        restored3 = su.preserve_local_modules(empty_backup)
        check("备份里没有本地模块时返回空列表", restored3 == [], str(restored3))
    finally:
        su.ENGINE_DIR = real_engine_dir

# --------------------------------------------------------------------------- #
section("2) --check 比对不应把本地模块误报成「上游移除」")

local_sites = {
    "BILIBILI": "哔哩哔哩",
    "WEIBO": "微博超话",
    "TIEBA": "百度贴吧",
}
upstream_sites = {
    "BILIBILI": "哔哩哔哩",
    "TIEBA": "百度贴吧",
}
local_only = {k: v for k, v in local_sites.items() if k.lower() in su.LOCAL_SITE_MODULES}
for key in local_only:
    local_sites.pop(key, None)
check("本地模块被摘出比对集合", "WEIBO" not in local_sites, str(local_sites))
removed = sorted(set(local_sites) - set(upstream_sites))
check("比对结果里没有 WEIBO（不误报移除）", "WEIBO" not in removed, str(removed))

# --------------------------------------------------------------------------- #
section("3) 恢复出来的模块仍能被引擎发现")

sys.path.insert(0, ROOT)
try:
    from dailycheckin.configs import checkin_map

    check("引擎能加载 WEIBO", "WEIBO" in checkin_map, str(sorted(checkin_map)))
    check("类名正确", checkin_map.get("WEIBO", ("", None))[1].__name__ == "Weibo")
except Exception as exc:  # noqa: BLE001
    check("引擎能加载 WEIBO", False, f"{type(exc).__name__}: {exc}")

# --------------------------------------------------------------------------- #
section("4) 三条比对路径都不能把本地模块误报成「上游移除」")

# 上游清单里当然没有 weibo；本地有。直接比对必然得出「上游移除 WEIBO」。
upstream = {"BILIBILI": "哔哩哔哩", "TIEBA": "百度贴吧"}
local = {"BILIBILI": "哔哩哔哩", "TIEBA": "百度贴吧", "WEIBO": "微博超话"}

check("upstream_comparable 会摘掉本地模块",
      "WEIBO" not in su.upstream_comparable(local), str(su.upstream_comparable(local)))
check("上游站点原样保留",
      set(su.upstream_comparable(local)) == {"BILIBILI", "TIEBA"}, str(su.upstream_comparable(local)))

# 实际比对一次，确认报告里没有「移除 WEIBO」
import io  # noqa: E402
import contextlib  # noqa: E402

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    changed = su.report_sites(su.upstream_comparable(local), upstream)
report = buf.getvalue()
check("比对结果认为无变化", changed is False, report)
check("报告里没有 WEIBO 被移除", "WEIBO" not in report, report)

# 远端后来新增了 --dry-run 路径（会再调一次 report_sites），
# 那条路径当时漏了过滤，实跑会打印「上游移除站点 1 个：- WEIBO」。
# 这里锁住「所有传给 report_sites 的本地站点集合都得先过 upstream_comparable」。
import inspect  # noqa: E402

src = inspect.getsource(su)
callers = [ln.strip() for ln in src.splitlines() if "report_sites(" in ln and "def " not in ln]
print("report_sites 调用点：")
for line in callers:
    print("      " + line)
check(
    "没有任何一处把未过滤的 local_sites/old_sites 直接传给 report_sites",
    all("upstream_comparable(" in ln or "cmp_sites" in ln for ln in callers),
    str(callers),
)
check("--dry-run 路径用的是 cmp_sites", any("cmp_sites, new_sites" in ln for ln in callers), str(callers))
check("--check 路径用的是 upstream_comparable(local_sites)",
      any("upstream_comparable(local_sites)" in ln for ln in callers), str(callers))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 68)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)