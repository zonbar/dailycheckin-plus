"""微博超话签到。

走 m.weibo.cn 的移动端 H5 接口（不是 weibo.com 网页版）：

* 网页版 session 有7 天硬性失效，而且 `/ajax/super/...` 一堆接口要 `XSRF-TOKEN`；
* 移动端是独立登录体系，`SUB` cookie 的存活期明显更长，长期挂机脚本基本都走这套。

接口只有两个，流程是「查登录态 → 拉关注的超话列表 → 调按钮自带的 scheme 签到」：

1. ``GET /api/config``                     —— 判断是否登录，顺带拿到 ``st``（签到要带）
2. ``GET /api/container/getIndex``          —— ``containerid=100803_-_followsuper`` 拉关注列表，
   用 ``since_id`` 翻页。每个超话卡片带一个按钮，按钮的 ``scheme`` 就是签到接口地址
3. ``GET <按钮 scheme>&st=<st>``            —— 签到

为什么直接用按钮的 scheme、而不是自己拼签到 URL：按钮 scheme 里的参数（``pageid``、
``scheme_type``、``source`` 等）是服务端下发的，跟着走最省事，也最不容易被改版打挂。

异常返回码（实测）：

* ``ok=1``                     —— 签到成功
* ``errno=402003``             —— 频率限制/状态未就绪，隔一下重试
* ``result=402004``+``scheme`` —— 触发风控，需要人工点验证链接
* ``ok=0`` 且含「已签到」        —— 今天已经签过了
"""

import json
import os
import random
import re
import time
import urllib.parse
from typing import Optional

import requests

from dailycheckin import CheckIn

M_WEIBO_BASE = "https://m.weibo.cn"
CONFIG_URL = f"{M_WEIBO_BASE}/api/config"
CONTAINER_URL = f"{M_WEIBO_BASE}/api/container/getIndex"
# 「我关注的超话」列表页的 containerid
FOLLOWED_SUPER_CONTAINER = "100803_-_followsuper"

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 按钮文案 -> 是否已完成。超话卡片上就这几个值：
# 「签到」= 今天还没签，「已签」= 已签过，「明日再来」= 今日次数用完了。
DONE_BUTTONS = {"已签", "已签到", "已簽", "已簽到", "明日再来"}


class Weibo(CheckIn):
    name = "微博超话"

    def __init__(self, check_item: dict):
        self.check_item = check_item or {}

        self.topics = self._parse_topics(self.check_item.get("topics"))
        # 只签指定超话（默认周杰伦）；填 "all" 则签全部关注的超话
        self.sign_all = "all" in self.topics
        # 是否带上「关注 + 签到」之后的翻页检查（拉全量关注列表，耗时更久）
        self.max_pages = int(self.check_item.get("max_pages") or 5)
        self.timeout = int(self.check_item.get("timeout") or 15)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA_MOBILE,
                "Referer": f"{M_WEIBO_BASE}/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "MWeibo-Pwa": "1",
            }
        )
        proxy = (self.check_item.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        cookie = self.check_item.get("cookie")
        if not cookie:
            raise ValueError("必须提供微博 Cookie（至少包含 SUB）")
        # 只认 SUB —— 没有它 /api/config 一定回login=False
        if "SUB=" not in cookie:
            raise ValueError("Cookie 中未找到 SUB，请重新登录微博后复制")
        requests.utils.add_dict_to_cookiejar(self.session.cookies, self._parse_cookie(cookie))

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_cookie(cookie: str) -> dict:
        """把 `k=v; k2=v2` 解析成 dict。

        这里没有沿用引擎里那句 `split("=")[1]`，因为 SUB 的值本身可能含 `=`，
        那样会被截断。这里统一用 partition，只切第一个 `=`。
        """
        out = {}
        for part in (cookie or "").replace("\r\n", "\n").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            if key:
                out[key] = value.strip()
        return out

    @staticmethod
    def _parse_topics(raw) -> list:
        """支持 list / 逗号分隔字符串 / 空值。"""
        if not raw:
            return ["周杰伦"]
        if isinstance(raw, (list, tuple)):
            items = [str(one).strip() for one in raw]
        else:
            items = [one.strip() for one in str(raw).replace("，", ",").split(",")]
        items = [one for one in items if one]
        return items or ["周杰伦"]

    def _get_json(self, url: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(url, params=params, timeout=self.timeout)
        # 微博风控会直接给 432（空 body），这类情况抛出去比返回 None 更容易定位
        if resp.status_code == 432:
            raise ValueError("被微博风控拦截（HTTP 432），请稍后重试或更换出口 IP")
        if resp.status_code == 418:
            raise ValueError("请求过于频繁（HTTP 418），请降低签到频率")
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"微博接口返回了非 JSON 内容：{exc}") from exc

    # ------------------------------------------------------------------ #
    # 接口
    # ------------------------------------------------------------------ #
    def login_state(self) -> tuple:
        """返回 (是否登录, st)。st 是签到接口要带的验签参数。"""
        data = self._get_json(CONFIG_URL)
        inner = data.get("data") or {}
        return bool(inner.get("login")), inner.get("st")

    def followed_topics(self) -> list:
        """拉取关注的超话列表，自动处理 since_id 翻页。

        返回 [{name, id, scheme, done, level}]：
        * ``scheme`` 今日已签时为 None（按钮已经换成「已签」了，没有可调用的地址）
        * ``done``  True 表示今天不用再签
        """
        topics: list = []
        since_id = ""
        seen: set = set()

        for _ in range(max(1, self.max_pages)):
            params = {"containerid": FOLLOWED_SUPER_CONTAINER}
            if since_id:
                params["since_id"] = since_id
            payload = self._get_json(CONTAINER_URL, params=params)
            if payload.get("ok") != 1:
                # 未登录时这里 ok=0，交给上层统一报「cookie 失效」
                break
            data = payload.get("data") or {}

            for card in data.get("cards") or []:
                group = card.get("card_group") or [card]
                for item in group:
                    one = self._parse_topic_card(item)
                    if one and one["id"] not in seen:
                        seen.add(one["id"])
                        topics.append(one)

            since_id = (data.get("cardlistInfo") or {}).get("since_id", "")
            if not since_id:
                break
            time.sleep(1)

        return topics

    @staticmethod
    def _parse_topic_card(item: dict) -> Optional[dict]:
        """从一张超话卡片里抽出签到需要的信息。"""
        if not isinstance(item, dict) or item.get("card_type") != "8":
            return None
        buttons = item.get("buttons") or []
        scheme = (item.get("scheme") or "").strip()
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(scheme).query)
        topic_id = qs.get("containerid", [""])[0]
        name = item.get("title_sub") or ""
        if not name or not topic_id:
            return None

        button_name = buttons[0].get("name", "") if buttons else ""
        btn_scheme = buttons[0].get("scheme") if buttons else None
        done = button_name in DONE_BUTTONS or not btn_scheme
        return {
            "name": name,
            "id": topic_id,
            "scheme": None if done else btn_scheme,
            "done": done,
            "level": item.get("desc1") or "",
        }

    def checkin_topic(self, topic: dict, st: str) -> dict:
        """对单个超话签到。返回 {success, message, retry, verify_url}。"""
        url = topic["scheme"]
        if not url.startswith("http"):
            url = M_WEIBO_BASE + url
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}st={urllib.parse.quote(str(st or ''))}"

        resp = self._get_json(url)
        message = resp.get("msg") or resp.get("errmsg") or ""
        ok = resp.get("ok")
        errno = str(resp.get("errno", ""))
        result = str(resp.get("result", ""))

        # 触发风控：接口会回一个验证页面的 scheme，需要人工点一下
        if result == "402004" or errno == "402004":
            return {
                "success": False,
                "message": "触发微博验证，需点链接完成后再签到",
                "retry": False,
                "verify_url": resp.get("scheme") or "",
            }
        # 频率限制：不算失败，隔一下重试
        if errno == "402003" or result == "402003":
            return {"success": False, "message": "接口忙，稍后重试", "retry": True, "verify_url": ""}
        if ok == 1 or "成功" in str(message):
            return {"success": True, "message": message or "签到成功", "retry": False, "verify_url": ""}
        if "已签到" in str(message) or "已经签到" in str(message):
            return {"success": True, "message": "今日已签到", "retry": False, "verify_url": ""}
        return {
            "success": False,
            "message": message or json.dumps(resp, ensure_ascii=False)[:120],
            "retry": False,
            "verify_url": "",
        }

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> list:
        """返回 [{name, success, message}]，message 为空表示无问题。"""
        logged, st = self.login_state()
        if not logged:
            raise ValueError("微博 Cookie 已失效（/api/config 返回 login=False），请重新登录后复制 Cookie")

        topics = self.followed_topics()
        if not topics:
            raise ValueError("没有取到任何关注的超话，请确认账号确实关注了超话")

        if self.sign_all:
            targets = topics
            missing: list = []
        else:
            targets = [one for one in topics if self._match(one["name"])]
            # 这里不能用 self._match()：它会拿「全部配置的超话」去比，
            # 只要配置里任意一个能匹配上就返回 True，那样永远算不出「哪个没找到」。
            # 这里要拿「单个 want」逐个精确比。
            real_names = {self._clean_name(one["name"]) for one in topics}
            missing = [
                want
                for want in self.topics
                if want != "all" and self._clean_name(want) not in real_names
            ]
            if missing and not targets:
                # 一个都没签到 —— 当成失败抛出去，面板会标红
                raise ValueError(
                    "关注的超话里没有「%s」，请先在微博里关注该超话（当前关注了 %d 个）"
                    % ("、".join(missing), len(topics))
                )

        results: list = []
        for index, topic in enumerate(targets):
            entry = {"name": topic["name"], "success": True, "message": "", "level": topic["level"]}
            if topic["done"]:
                entry["message"] = "今日已签到"
                results.append(entry)
                continue
            try:
                resp = self.checkin_topic(topic, st)
                if resp["retry"]:
                    # st 可能过期，重取一次再试
                    _, st = self.login_state()
                    time.sleep(random.uniform(2, 4))
                    resp = self.checkin_topic(topic, st)
                entry["success"] = resp["success"]
                entry["message"] = resp["message"]
                if resp["verify_url"]:
                    entry["verify_url"] = resp["verify_url"]
            except Exception as exc:  # noqa: BLE001 - 单个超话失败不影响其它
                entry["success"] = False
                entry["message"] = str(exc)[:150]
            results.append(entry)
            # 最后一个不等待
            if index < len(targets) - 1:
                time.sleep(random.uniform(3, 6))

        # 部分超话没关注到时要说清楚，否则用户以为「配了 3 个就都签了」
        for want in missing:
            results.append(
                {"name": want, "success": False, "message": "未找到该超话，请确认已关注", "level": ""}
            )
        return results

    @staticmethod
    def _clean_name(name: str) -> str:
        """剥掉超话标题里的装饰：【】方括号、尾部「超话」、以及零宽/空白字符。

        微博上的超话标题不一定干净：「周杰伦」「【周杰伦】」「周杰伦超话」指的可能是同一个，
        但「周杰伦电影速报」是另一个超话，不能因为包含「周杰伦」就误签。
        """
        text = (name or "").strip()
        #去掉空白、连字符、以及零宽字符（BOM / ZWSP 混在标题里会导致对不上）
        text = re.sub(r"[\s\-\u200b-\u200f\ufeff]", "", text)
        text = text.strip("【】[]（）()")
        if text.endswith("超话"):
            text = text[: -len("超话")]
        return text.strip("【】[]（）()")

    def _match(self, real_name: str) -> bool:
        """精确匹配超话名（去掉装饰后全等）。

        刻意**不做子串包含**：否则「周杰伦」会命中「周杰伦电影速报」「周杰伦粉丝站」等一堆
        无关超话，把不该签的也签了。名字对不上时宁可漏签，交由 run() 报「没关注该超话」。
        """
        real_clean = self._clean_name(real_name)
        if not real_clean:
            return False
        for want in self.topics:
            if want == "all":
                continue
            if self._clean_name(want) == real_clean:
                return True
        return False

    def main(self) -> str:
        results = self.run()
        msg = [
            {"name": "账号信息", "value": self._nickname()},
        ]
        for one in results:
            flag = "✅" if one["success"] else "❌"
            title = one["name"]
            if one.get("level"):
                title = f"{title}（{one['level']}）"
            value = f"{flag} {one['message']}".strip()
            if one.get("verify_url"):
                value += f" 验证链接：{one['verify_url']}"
            msg.append({"name": title, "value": value})
        return "\n".join([f"{one.get('name')}: {one.get('value')}" for one in msg])

    def _nickname(self) -> str:
        """尝试取一下昵称，取不到就显示已登录（纯展示，不影响签到）。"""
        try:
            payload = self._get_json(f"{M_WEIBO_BASE}/api/container/getIndex", params={"containerid": "100803"})
            data = payload.get("data") or {}
            for card in data.get("cards") or []:
                user = card.get("user") or {}
                if user.get("screen_name"):
                    return user["screen_name"]
        except Exception:  # noqa: BLE001
            pass
        return "已登录"


if __name__ == "__main__":
    with open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
        encoding="utf-8",
    ) as f:
        datas = json.loads(f.read())
    _check_item = datas.get("WEIBO", [])[0]
    print(Weibo(check_item=_check_item).main())