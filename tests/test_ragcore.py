"""ragcore 的回归用例：静态解析、角色推断、分块不丢词、df 口径。

样本全部是合成数据，不含任何真实标签或词表。
"""
from __future__ import annotations

import textwrap

import ragcore as R

SOURCE = textwrap.dedent(
    """
    # 这段源码只被静态解析，永远不会被 import 或执行
    dangerous()  # 副作用：解析时不应触发

    rules = [
        {
            "label": "示例应用-屏蔽-竞品",
            "text": {"示例词A", "示例词B"},
            "match": lambda found_text: bool(found_text & {"示例词A", "示例词B"}),
        },
        {
            "label": "示例应用-敏感",
            "text": ["示例词C"],
            "keywords": ["示例词D"],
            "match": lambda found_text, found_keyword: "示例词C" in found_text
            or found_keyword & {"示例词D"} and not (found_text & {"示例反例"}),
        },
        {
            "label": "示例词库",
            "text": {"示例词E"},
            "keywords": ["不该被导出"],
            "match": lambda found_text: "示例词E" in found_text,
        },
    ]
    """
)


# ---------------------------------------------------------------- 规则提取


def test_extract_rules_reads_only_literals():
    rules = R.extract_rules(SOURCE)
    labels = [rule["label"] for rule in rules]
    assert labels == ["示例应用-屏蔽-竞品", "示例应用-敏感", "示例词库"]
    # 集合字面量的迭代顺序不保证，按集合比较
    assert set(rules[0]["words"]) == {"示例词A", "示例词B"}


def test_extract_rules_does_not_execute_source():
    # 源码里有 dangerous() 调用；解析不应执行它（执行会 NameError）
    assert len(R.extract_rules(SOURCE)) == 3


def test_keywords_exported_only_when_match_uses_that_channel():
    rules = {rule["label"]: rule for rule in R.extract_rules(SOURCE)}
    # match 引用了 found_keyword，keywords 才算该标签的词
    assert "示例词D" in rules["示例应用-敏感"]["words"]
    # match 没用 found_keyword，keywords 里的词不该被导出（否则虚增词频）
    assert "不该被导出" not in rules["示例词库"]["words"]


def test_extract_rules_from_mapping_accepts_three_forms():
    rules = R.extract_rules_from_mapping(
        [
            {"label": "甲", "text": {"a", "b"}},
            {"label": "乙", "text": ["c", "d"]},
            {"label": "丙", "text": "e, f，g"},
        ]
    )
    assert set(rules[0]["words"]) == {"a", "b"}
    assert rules[1]["words"] == ["c", "d"]
    assert rules[2]["words"] == ["e", "f", "g"]


# ---------------------------------------------------------------- 标签名解析


def test_parse_label_name_multi_segment():
    parsed = R.parse_label_name("示例应用-屏蔽-竞品-不可替换")
    assert parsed == {
        "app": "示例应用",
        "disposition": "屏蔽",
        "replaceable": "unreplaceable",
        "is_composite": True,
    }


def test_parse_label_name_single_segment_is_its_own_group():
    parsed = R.parse_label_name("通用词库")
    assert parsed["app"] == "通用词库"
    assert parsed["disposition"] == "未标注"
    assert parsed["replaceable"] == "unknown"
    assert parsed["is_composite"] is False


# ---------------------------------------------------------------- 词角色


def test_analyze_match_extracts_exclude():
    analysis = R.analyze_match(
        "lambda found_text: bool(found_text & {'命中'}) and not (found_text & {'反例'})"
    )
    assert analysis["exclude"] == ["反例"]
    assert analysis["gate"] == []
    assert analysis["not_sufficient"] == []


def test_analyze_match_extracts_gate_from_ternary():
    analysis = R.analyze_match(
        "lambda found_text: bool(found_text & {'命中'}) if found_text & {'开关'} else False"
    )
    assert analysis["gate"] == ["开关"]


def test_analyze_match_extracts_not_sufficient_from_subtraction():
    analysis = R.analyze_match(
        "lambda found_text: bool((found_text - {'扣除'}) & {'命中'})"
    )
    assert analysis["not_sufficient"] == ["扣除"]


def test_analyze_match_marks_complex_when_body_is_not_found_text():
    analysis = R.analyze_match("lambda found_text: len(found_text) > 3")
    assert analysis["logic"] == "complex"


def test_word_roles_maps_each_role_to_its_label():
    rules = R.extract_rules(SOURCE)
    roles = R.word_roles(rules)
    assert roles["示例反例"]["示例应用-敏感"] == R.EXCLUDE


# ---------------------------------------------------------------- 分块编译


def test_split_by_chars_respects_budget():
    blocks = R.split_by_chars(["甲甲", "乙乙", "丙丙", "丁丁"], limit=6)
    assert all(len("、".join(block)) <= 6 for block in blocks)
    assert sum(len(block) for block in blocks) == 4


def test_compile_chunks_keeps_every_word():
    words = [f"词{i:03d}" for i in range(50)]
    entries = [{"label": "示例应用-屏蔽", "app": "示例应用", "disposition": "屏蔽", "words": words}]
    chunks = R.compile_chunks(entries, max_words=40, tail_chars=380)

    assert chunks[0]["kind"] == "profile"
    assert len(chunks[0]["words"]) == 40
    assert R.chunk_word_coverage(entries, chunks)["ok"] is True

    # 代表词之外的 10 个词必须全部落在续块里
    tail_words = [word for chunk in chunks if chunk["kind"] == "tail" for word in chunk["words"]]
    assert tail_words == words[40:]


def test_compile_chunks_splits_long_tail_into_multiple_blocks():
    words = [f"词{i:03d}" for i in range(20)]
    entries = [{"label": "示例应用-屏蔽", "words": words}]
    chunks = R.compile_chunks(entries, max_words=5, tail_chars=10)

    tails = [chunk for chunk in chunks if chunk["kind"] == "tail"]
    assert len(tails) > 1
    assert R.chunk_word_coverage(entries, chunks)["ok"] is True


def test_chunks_start_with_marker_for_chunking():
    entries = [{"label": "示例应用-屏蔽", "words": ["示例词A"]}]
    texts = R.render_chunks(R.compile_chunks(entries))
    assert all(text.startswith(R.MARKER) for text in texts)
    assert "示例词A" in texts[0]


# ---------------------------------------------------------------- 频率统计


def test_document_frequency_counts_only_trigger_roles():
    rules = [
        {"label": "甲", "words": ["共用词", "独特词"]},
        {"label": "乙", "words": ["共用词", "反例词"]},
    ]
    roles = {"反例词": {"乙": R.EXCLUDE}}
    index = R.document_frequency(rules, roles)

    assert index["共用词"] == ["乙", "甲"] or index["共用词"] == ["甲", "乙"]
    assert len(index["共用词"]) == 2
    # 只作反例出现，不算占用
    assert "反例词" not in index


def test_format_words_truncates_with_total():
    text = R.format_words([f"词{i}" for i in range(5)], limit=2)
    assert "共 5 个" in text
    assert text.startswith("词0、词1")
