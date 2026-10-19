#!/usr/bin/env python3
"""微博超话模块的离线自测（不联网，纯标准库 + 构造响应）。

锁住几件容易在后续改动里被悄悄改坏的事：

1. **卡片解析**：只认`card_type == "8"`，名字取 `title_sub`，containerid 从 `scheme` 的
   查询串里取（不是从别的地方猜）。「已签 / 明日再来」要判定为 done 且 scheme 置 None。
2. **cookie 解析**：SUB 的值可能含 `=`，必须用 partition 而不是 `split("=")[1]`
   —— 后者会把值截断，导致明明有效的 cookie 被判失效。
3. **超话名匹配**：微博标题常带【】和「超话」后缀，要能宽松匹配上。
4. **结果解析**：ok=1 成功、已签到文案也算成功、402004 要给出验证链接、402003 要标记重试。
5. **失效判定**：login=False 时必须抛错，而不是静默返回「成功」。

跑法：python3 tools/test_weibo.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  [{'OK' if cond else 'FAIL]'}]   {name}" + (f":: {detail}" if not cond and detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)


# --------------------------------------------------------------------------- #
section("0) import 与站点注册")

from dailycheckin.configs import checkin_map  # noqa: E402

check("WEIBO 已被引擎自动发现", "WEIBO" in checkin_map, f"实际: {sorted(checkin_map)}")
check("中文名是「微博超话」", checkin_map.get("WEIBO", ("",))[0] == "微博超话")

from dailycheckin.weibo.main import Weibo  # noqa: E402

# --------------------------------------------------------------------------- #
section("1) cookie 解析：值里含 = 不能被截断")

sub_val = "_2A25JqY8VDeRhGedH6FoU8CjMwj2IHXVoabc=="
parsed = Weibo._parse_cookie(f"SUB={sub_val}; SUBP=003J5gHabc; _T_WM=xyz123")
check("SUB 完整保留（含结尾 ==）", parsed.get("SUB") == sub_val, f"实际: {parsed.get('SUB')!r}")
check("不会像 split('=')[1] 那样截断", parsed.get("SUB") != sub_val.split("=")[0])
check("其它字段也解析正常", parsed.get("SUBP") == "003J5gHabc" and parsed.get("_T_WM") == "xyz123")
check("空片段被忽略", Weibo._parse_cookie("a=1;;  ;b=2") == {"a": "1", "b": "2"})

# --------------------------------------------------------------------------- #
section("2) 构造微博响应 → 卡片解析")


def make_card(name, page_id, btn_name, level="等级LV.7", has_scheme=True):
    btn_scheme = (
        f"/api/2/page/button?request_url=http%3A%2F%2Fi.huati.weibo.com%2Fmobile"
        f"%2Fsuper%2Factive_checkin%3Fpageid%3D{page_id}&pageid={page_id}"
        if has_scheme
        else None
    )
    btn = {"type": "default", "name": btn_name}
    if btn_scheme:
        btn["scheme"] = btn_scheme
    return {
        "card_type": "8",
        "itemid": f"follow_super_{page_id}",
        "scheme": f"sinaweibo://pageinfo?containerid=100808{page_id}&extparam=x%23tabbar_follow%3D1",
        "title_sub": name,
        "desc1": level,
        "buttons": [btn],
    }


card_jay = make_card("周杰伦", "4665", "签到")
card_done = make_card("崩坏星穹铁道", "abc123", "已签", level="等级LV.3")
card_tomorrow = make_card("明日再说", "def456", "明日再来")

one = Weibo._parse_topic_card(card_jay)
check("正常解析出名字", one["name"] == "周杰伦", str(one))
check("containerid 从 scheme 查询串取出", one["id"] == "1008084665", f"实际: {one.get('id')}")
check("未签到时 scheme 保留", bool(one["scheme"]))
check("未签到时 done=False", one["done"] is False)
check("等级被保留", one["level"] == "等级LV.7", str(one.get("level")))

two = Weibo._parse_topic_card(card_done)
check("「已签」判定为 done", two["done"] is True)
check("「已签」时 scheme 置None（按钮已换掉）", two["scheme"] is None, str(two.get("scheme")))

three = Weibo._parse_topic_card(card_tomorrow)
check("「明日再来」判定为 done", three["done"] is True)

check("非 card_type=8 的卡片被忽略", Weibo._parse_topic_card({"card_type": "9", "title_sub": "x"}) is None)
check("缺 buttons 时不算已签且 scheme 为空", Weibo._parse_topic_card(
    {"card_type": "8", "title_sub": "空按钮", "scheme": "sinaweibo://pageinfo?containerid=1008081",
     "buttons": []})["scheme"] is None)
check("没有 title_sub 的卡片被忽略",
      Weibo._parse_topic_card({"card_type": "8", "scheme": "sinaweibo://pageinfo?containerid=1"}) is None)

# --------------------------------------------------------------------------- #
section("3) 超话名匹配（默认周杰伦）")

wb = Weibo.__new__(Weibo)
wb.topics = Weibo._parse_topics("")
check("默认话题是周杰伦", wb.topics == ["周杰伦"], str(wb.topics))
check("匹配「周杰伦」", wb._match("周杰伦") is True)
check("匹配带【】的标题", wb._match("【周杰伦】") is True)
check("匹配带「超话」后缀的标题", wb._match("周杰伦超话") is True)
check("匹配带空格的标题", wb._match("周 杰 伦") is True)
check("不误匹配其它超话", wb._match("林俊杰") is False)
check("不误匹配「周杰伦电影」", wb._match("周杰伦电影") is False)
check("不误匹配「周杰伦电影速报」", wb._match("周杰伦电影速报") is False)
check("不误匹配「周杰伦粉丝站」", wb._match("周杰伦粉丝站") is False)
check("不误匹配空名字", wb._match("") is False)

wb2 = Weibo.__new__(Weibo)
wb2.topics = Weibo._parse_topics("周杰伦， 林俊杰")
check("支持中文逗号分隔", wb2._match("林俊杰") is True)

wb3 = Weibo.__new__(Weibo)
wb3.topics = Weibo._parse_topics("all")
check("填 all 时 topics 含 all", "all" in wb3.topics)
check("all 模式下 match 不生效", wb3._match("周杰伦") is False)

# --------------------------------------------------------------------------- #
section("4) 签到返回码解析")

wb4 = Weibo.__new__(Weibo)
wb4.session = type("S", (), {"get": lambda self, *a, **k: None})()
wb4.timeout = 1

CASES = [
    ({"ok": 1, "msg": "签到成功"}, True, False, "成功"),
    ({"ok": 1, "msg": ""}, True, False, "无msg 也算成功"),
    ({"ok": 0, "msg": "你今天已经签到过了"}, True, False, "已签到文案算成功"),
    ({"result": "402004", "scheme": "https://passport.weibo.com/verify?x=1"}, False, False, "风控给验证链接"),
    ({"errno": "402003", "msg": "too frequent"}, False, True, "频率限制需重试"),
    ({"ok": 0, "msg": "网络错误"}, False, False, "其它失败"),
]

# 注意：赋给实例属性后不会自动绑定 self，所以这里形参从 url 开始
for payload, want_ok, want_retry, label in CASES:
    wb4._get_json = lambda url, params=None, _p=payload: _p
    r = Weibo.checkin_topic(wb4, {"scheme": "/api/2/page/button?pageid=1", "name": "周杰伦"}, "st123")
    check(f"「{label}」success={want_ok}", r["success"] is want_ok, str(r))
    check(f"「{label}」retry={want_retry}", r["retry"] is want_retry, str(r))

wb4._get_json = lambda url, params=None: {"result": "402004", "scheme": "https://verify.link"}
r = Weibo.checkin_topic(wb4, {"scheme": "/x"}, "st")
check("402004 会带出验证链接", r["verify_url"] == "https://verify.link", str(r))

# 签到 URL 要拼上 st，且相对路径要补全域名
captured = {}


def capture(url, params=None):
    captured["url"] = url
    return {"ok": 1, "msg": "签到成功"}


wb4._get_json = capture
Weibo.checkin_topic(wb4, {"scheme": "/api/2/page/button?pageid=1"}, "ST9")
check("相对 scheme 补全为 m.weibo.cn 域名", captured["url"].startswith("https://m.weibo.cn/api/2/page/button"), captured["url"])
check("签到 URL 带上了 st", "st=ST9" in captured["url"], captured["url"])

# --------------------------------------------------------------------------- #
section("5) 登录态失效必须抛错（不能静默当成功）")

wb5 = Weibo.__new__(Weibo)
wb5.topics = ["周杰伦"]
wb5.sign_all = False
wb5.max_pages = 1
wb5.timeout = 1
wb5._get_json = lambda url, params=None: {"data": {"login": False, "st": ""}}
raised = ""
try:
    Weibo.run(wb5)
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("login=False 时抛异常", bool(raised), "没有抛异常 —— 会静默通过")
check("异常文案提示 cookie 失效", "失效" in raised, raised)

wb5._get_json = lambda url, params=None: {"data": {"login": True, "st": "ST"}}
raised = ""
try:
    Weibo.run(wb5)
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("取不到超话时抛异常", bool(raised), "没有抛异常")
check("异常文案提示没关注超话", "超话" in raised, raised)

# --------------------------------------------------------------------------- #
section("6) 缺 cookie 时的构造期报错")

for bad, expect in (
    ({}, "必须提供"),
    ({"cookie": "SUBP=abc; _T_WM=x"}, "未找到 SUB"),
):
    msg = ""
    try:
        Weibo(check_item=bad)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
    check(f"{str(bad)[:40]} -> 报错含「{expect}」", expect in msg, msg)

# --------------------------------------------------------------------------- #
section("7) 端到端（打桩网络）：周杰伦已签 / 未签 两种路径")

wb7 = Weibo(check_item={"cookie": "SUB=_2A25abc==; SUBP=xyz", "topics": "周杰伦"})
wb7.checkin_topic = lambda topic, st: {"success": True, "message": "签到成功", "retry": False, "verify_url": ""}
# run() 第一步会打 /api/config 查登录态，这里直接打桩掉
wb7.login_state = lambda: (True, "ST")
wb7._nickname = lambda: "测试昵称"

state = {"page": 0}


def fake_followed(url=None, params=None):
    return [
        {"name": "崩坏星穹铁道", "id": "1", "scheme": "/s", "done": False, "level": "LV.7"},
        {"name": "周杰伦", "id": "2", "scheme": "/s", "done": True, "level": "LV.7"},
    ]


wb7.followed_topics = fake_followed
res = wb7.run()
check("只签指定超话（只返回周杰伦）", [r["name"] for r in res] == ["周杰伦"], str(res))
check("已签状态被识别", res[0]["success"] is True and res[0]["message"] == "今日已签到", str(res))

out = wb7.main()
check("main() 输出含账号信息行", out.splitlines()[0].startswith("账号信息:"), out)
check("main() 输出含周杰伦", "周杰伦" in out, out)

wb7.followed_topics = lambda: [
    {"name": "周杰伦", "id": "2", "scheme": "/s", "done": False, "level": "等级LV.7"},
]
res = wb7.run()
check("未签时会调用签到", res[0]["success"] is True and res[0]["message"] == "签到成功", str(res))
out = wb7.main()
check("main() 输出带✅", "✅" in out, out)

# 未关注周杰伦 -> 要明确报错，而不是静默成功
wb7.followed_topics = lambda: [
    {"name": "崩坏星穹铁道", "id": "1", "scheme": "/s", "done": False, "level": ""},
]
raised = ""
try:
    wb7.run()
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
check("没关注周杰伦时明确报错", "周杰伦" in raised and "关注" in raised, raised)

# 只关注了其中一个：另一个要出现在结果里，而不是静默丢掉
wb7.topics = ["周杰伦", "林俊杰"]
wb7.sign_all = False
wb7.followed_topics = lambda: [
    {"name": "周杰伦", "id": "2", "scheme": "/s", "done": False, "level": ""},
]
res = wb7.run()
names = [r["name"] for r in res]
check("匹配到的超话正常签到", "周杰伦" in names, str(names))
check("未关注的那个也出现在结果里", "林俊杰" in names, str(names))
notfound = [r for r in res if r["name"] == "林俊杰"][0]
check("未关注的标记为失败", notfound["success"] is False, str(notfound))
check("未关注的给出可读原因", "关注" in notfound["message"], str(notfound))
check("main() 输出里能看到未关注的超话", "林俊杰" in wb7.main())

# all 模式：全部签到，且不产生 missing
wb7.topics = ["all"]
wb7.sign_all = True
wb7.followed_topics = lambda: [
    {"name": "周杰伦", "id": "2", "scheme": "/s", "done": False, "level": ""},
    {"name": "林俊杰", "id": "3", "scheme": "/s", "done": True, "level": ""},
]
res = wb7.run()
check("all 模式签全部（2 个）", len(res) == 2, str(res))
check("all 模式没有多余提示项", all("未找到" not in r["message"] for r in res), str(res))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 68)
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败项：")
    for one in failed:
        print("  - " + one)
sys.exit(1 if failed else 0)