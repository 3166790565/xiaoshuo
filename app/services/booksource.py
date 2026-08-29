"""开源阅读（Legado）书源。

``legado/源.json`` 是导出的静态副本，方便离线导入；
``GET /legado/源.json`` 会按当前的 ``SITE_BASE_URL`` 与 ``API_TOKEN`` 实时生成，
从这个地址网络导入就不用手改地址了。
"""

from __future__ import annotations

from .. import config

_SEARCH_RULES = {
    "bookList": "$.data.list[*]",
    "name": "$.name",
    "author": "$.author",
    "intro": "$.intro",
    "kind": "$.kind",
    "wordCount": "$.wordCount",
    "coverUrl": "$.coverUrl",
    "bookUrl": "$.bookUrl",
}


def build(base_url: str | None = None, token: str | None = None) -> dict:
    base = (base_url or config.SITE_BASE_URL).rstrip("/")
    key = token if token is not None else config.API_TOKEN
    suffix = f"&token={key}" if key else ""

    return {
        "bookSourceName": config.SITE_NAME,
        "bookSourceType": 0,
        "bookSourceUrl": base,
        "bookSourceGroup": "自建书库",
        "bookSourceComment": (
            "自建小说站的只读 JSON 接口。\n"
            "换地址时把本源里所有 " + base + " 替换成实际可访问的地址；\n"
            "服务端设置了 API_TOKEN 的话，四个 url 都要带同一个 token 参数。"
        ),
        "enabled": True,
        "enabledExplore": True,
        "enabledCookieJar": False,
        "customOrder": 0,
        "weight": 0,
        "respondTime": 180000,
        "lastUpdateTime": 0,
        "searchUrl": f"{base}/api/search?key={{{{key}}}}&page={{{{page}}}}{suffix}",
        "exploreUrl": f"最近更新::{base}/api/search?key=&page={{{{page}}}}{suffix}",
        "ruleSearch": dict(_SEARCH_RULES),
        "ruleExplore": dict(_SEARCH_RULES),
        "ruleBookInfo": {
            "init": "$.data",
            "name": "$.name",
            "author": "$.author",
            "intro": "$.intro",
            "kind": "$.kind",
            "wordCount": "$.wordCount",
            "coverUrl": "$.coverUrl",
            "lastChapter": "$.latestChapterTitle",
            "tocUrl": "$.tocUrl",
        },
        "ruleToc": {
            "chapterList": "$.data.list[*]",
            "chapterName": "$.title",
            "chapterUrl": "$.url",
            "nextTocUrl": "$.data.nextUrl",
        },
        "ruleContent": {"content": "$.data.content"},
    }
