"""上下文工程用例：占位符索引/还原、残留拦截、结果投递策略。

这三块防线对应三类线上问题：

- 长 URL 进上下文被模型截断或重写 → 索引成整组标记，工具前还原；
- 还原不干净，占位符进了 SQL → 静默查空，比报错更难排查；
- 大结果直接进上下文 → 撑爆窗口，模型还会复述行内容。
"""
from __future__ import annotations

import contextcore as X

# ---------------------------------------------------------------- 索引


def test_index_urls_collapses_to_one_marker():
    text = "查一下 https://cdn.example.com/a.png 的创意标签"
    new_text, urls = X.index_urls(text)

    assert urls == ["https://cdn.example.com/a.png"]
    assert X.URL_MARKER in new_text
    assert "https://cdn.example.com/a.png" not in new_text


def test_index_urls_keeps_one_mapping_for_multiple_urls():
    """多个 URL 落在同一组：标记可能出现多处，但映射只有一条 → 还原成同一组值。"""
    text = "https://a.example.com/1.png 和 https://b.example.com/2.jpg 这两个素材"
    new_text, urls = X.index_urls(text)

    assert len(urls) == 2
    mapping = X.group_mapping(X.URL_MARKER, urls)
    assert list(mapping) == [X.URL_MARKER]
    assert mapping[X.URL_MARKER] == "https://a.example.com/1.png,https://b.example.com/2.jpg"
    # 任一处标记都还原成整组，模型不会因为"第几个"指代错位
    assert new_text.count(X.URL_MARKER) >= 1
    assert X.restore_urls(new_text, mapping).count("https://a.example.com/1.png") >= 1


def test_index_urls_collapses_comma_separated_markers():
    """逗号/顿号分隔的多个 URL 被压成一个标记（原句里并排贴链接的常见形态）。"""
    text = "https://a.example.com/1.png,https://b.example.com/2.jpg"
    new_text, urls = X.index_urls(text)

    assert len(urls) == 2
    assert new_text.count(X.URL_MARKER) == 1


def test_index_urls_keeps_query_comma_intact():
    """query string 里的逗号属于 URL 本身，不能被当成分隔符切断。"""
    text = "https://cdn.example.com/a.png?w=100,h=200 这个图的分类"
    _, urls = X.index_urls(text)

    assert urls == ["https://cdn.example.com/a.png?w=100,h=200"]


def test_index_urls_skips_generic_object_storage_links():
    """非图片的对象存储链接不索引（它可能是结果文件，不是素材）。"""
    text = "结果在 https://bucket.oss-cn-beijing.aliyuncs.com/report.csv"
    new_text, urls = X.index_urls(text)

    assert urls == []
    assert new_text == text


def test_index_creatives_matches_adjacent_to_chinese():
    """创意 ID 紧贴中文时也要匹配（不能用 \\b）。"""
    text = "native-260-8fa2e6176eee11e5e8de3255983aafd3这个创意对应的素材"
    new_text, creatives = X.index_creatives(text)

    assert creatives == ["native-260-8fa2e6176eee11e5e8de3255983aafd3"]
    assert X.CREATIVE_MARKER in new_text


def test_group_mapping_joins_values():
    assert X.group_mapping(X.URL_MARKER, ["a", "b", "a"]) == {X.URL_MARKER: "a,b"}
    assert X.group_mapping(X.URL_MARKER, []) == {}


# ---------------------------------------------------------------- 还原


def test_restore_accepts_marker_variants():
    """模型会写变体：去方括号、改大小写、多空格。变体还原不了就会静默查空。"""
    mapping = {X.URL_MARKER: "https://cdn.example.com/a.png"}
    for variant in ("[__URL_LIST__]", "__url_list__", "[ __URL_LIST__ ]", "__URL_LIST___"):
        assert X.restore_urls(f"landing={variant}", mapping) == "landing=https://cdn.example.com/a.png"


def test_restore_without_mapping_keeps_text():
    text = f"landing={X.URL_MARKER}"
    assert X.restore_urls(text, None) == text
    assert X.restore_urls(text, {}) == text


def test_leftover_detection_catches_old_numbered_form():
    """老会话里的 `[__URL__3]` 与模型臆造的标记都要被判成残留。"""
    assert X.has_url_marker("[__URL__3]") is True
    assert X.has_url_marker(X.URL_MARKER) is True
    assert X.has_url_marker("https://cdn.example.com/a.png") is False
    assert X.has_creative_marker(["ok", "[__CREATIVE__2]"]) is True


# ---------------------------------------------------------------- 还原参数


def test_restore_args_only_touches_whitelisted_params():
    args = {"url": X.URL_MARKER, "note": X.URL_MARKER}
    restored, leftover = X.restore_args(
        "to_material_md5", args, {X.URL_MARKER: "https://cdn.example.com/a.png"}
    )

    assert restored["url"] == "https://cdn.example.com/a.png"
    # 不在白名单里的参数原样保留，避免把普通字符串改掉
    assert restored["note"] == X.URL_MARKER
    assert leftover == {}


def test_restore_args_reports_leftover_when_mapping_missing():
    """映射缺失时宁可拦下，也不要让占位符进 SQL 静默查空。"""
    restored, leftover = X.restore_args("exposure_detail", {"landing": X.URL_MARKER})

    assert restored["landing"] == X.URL_MARKER
    assert leftover == {"landing": X.URL_MARKER}


def test_restore_args_handles_creative_list_values():
    args = {"creative_md5": [X.CREATIVE_MARKER, "native-260-abc123456789"]}
    restored, _ = X.restore_args(
        "exposure_metrics", args, creative_mapping={X.CREATIVE_MARKER: "native-260-x123456789"}
    )

    assert restored["creative_md5"] == ["native-260-x123456789", "native-260-abc123456789"]


# ---------------------------------------------------------------- 结果投递


def test_deliver_policy_inlines_small_results():
    policy = X.deliver_policy(20)
    assert policy == {"oss_link": True, "inline_html": True, "rows": 20}


def test_deliver_policy_skips_html_for_large_results():
    policy = X.deliver_policy(500_000)
    assert policy["oss_link"] is True
    assert policy["inline_html"] is False
