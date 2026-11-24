"""ragcore —— 从「代码即规则」的规则库构建可检索知识库（纯函数、零 I/O）。

很多业务规则直接写在代码里，分散在 Python 字面量、YAML 配置和线上缓存三处。要让它们
可检索，有两个不能做的事：

- **不能 import 那份代码**：规则模块导入时的副作用（连缓存、拉词表、建自动机）会把
  「读一份数据」变成「启动半个服务」；
- **不能把表达式原样喂给模型**：`lambda` 加集合运算既可执行也不可读，模型数不清里面的词。

这里的做法是静态解析 + 编译成分块文本，全程只读字符串：

1. `extract_rules` / `extract_rules_from_mapping` —— 取规则，不执行
2. `parse_label_name` —— 从标签名解析出 应用 / 处置 / 可替换性
3. `analyze_match` —— 从布尔表达式推断每个词的角色（触发 / 扣除 / 否决 / 开关）
4. `compile_chunks` —— 编译成「代表词 + 词表续」的检索块，按字符预算切分
5. `document_frequency` —— 从规则反算每个词被多少个标签使用

`compile_chunks` 保证**词不丢失**：代表词之外的词全部落在续块里，`words` 字段可供断言。
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: 词角色。触发词是默认值，其余三类改变「命中之后怎么算」
TRIGGER = "trigger"
EXCLUDE = "exclude"  # 否定：命中即推翻该标签
GATE = "gate"  # 开关：只在条件成立时参与
NOT_SUFFICIENT = "not_sufficient"  # 扣除：命中只是不计入证据

#: 编译分块时写在块首的短标记，同时用作向量库的切片边界
MARKER = "##TAG##"

#: 标签名里的处置段候选，可按业务替换
DEFAULT_DISPOSITION_TOKENS: tuple[str, ...] = ("屏蔽", "敏感", "竞品", "定向")


# --------------------------------------------------------------------------- #
# 词表归一
# --------------------------------------------------------------------------- #


def normalize_words(value: Any) -> list[str]:
    """把 `text` 的三种写法（集合 / 列表 / 逗号串）统一成字符串列表。"""
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def dedupe(words: Iterable[str]) -> list[str]:
    """去重保序。"""
    seen: set[str] = set()
    result: list[str] = []
    for word in words:
        text = str(word).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


# --------------------------------------------------------------------------- #
# 1. 规则提取：只读字面量，绝不执行
# --------------------------------------------------------------------------- #


def _literal_words(node: ast.AST | None) -> list[str]:
    """取 Set / List / Tuple 里的字符串常量；lambda、并集、差集一律忽略。"""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    return []


def _match_names(match: Any) -> set[str]:
    """`match` 表达式里实际引用的变量名（lambda 参数不是 Name，不会污染结果）。"""
    node = match
    if isinstance(match, str):
        if not match.strip():
            return set()
        try:
            node = ast.parse(match, mode="eval").body
        except SyntaxError:
            return set()
    if not isinstance(node, ast.AST):
        return set()
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _uses_channel(names: set[str], prefix: str) -> bool:
    """`found_keyword` / `found_keywords` 两种写法都认。"""
    return any(name.startswith(prefix) for name in names)


def _rule_from_fields(fields: Mapping[str, ast.AST], source: str) -> dict[str, Any] | None:
    label_node = fields.get("label")
    if not (isinstance(label_node, ast.Constant) and isinstance(label_node.value, str)):
        return None
    label = label_node.value.strip()
    if not label:
        return None

    words = _literal_words(fields.get("text"))
    # keywords / products 只在 match 真的引用对应变量时才算这个词表的词；
    # 否则写在该字段但不参与判定，导出只会虚增词频。
    names = _match_names(fields.get("match"))
    for channel, prefix in (("keywords", "found_keyword"), ("products", "found_product")):
        if _uses_channel(names, prefix):
            words += _literal_words(fields.get(channel))

    match_text = ""
    match_node = fields.get("match")
    if isinstance(match_node, ast.AST):
        try:
            match_text = ast.unparse(match_node)
        except Exception:  # 反解析失败不影响提取
            match_text = ""
    return {"label": label, "words": dedupe(words), "match": match_text, "source": source}


def extract_rules(source: str) -> list[dict[str, Any]]:
    """从源码里取出 `rules = [...]` 并解析成记录，**不 import、不执行**。"""
    tree = ast.parse(source)
    records: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
            continue
        is_rules = any(
            isinstance(target, ast.Name) and target.id == "rules" for target in node.targets
        )
        if not is_rules:
            continue
        for element in node.value.elts:
            if not isinstance(element, ast.Dict):
                continue
            fields: dict[str, ast.AST] = {}
            for key_node, value_node in zip(element.keys, element.values, strict=False):
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    fields[key_node.value] = value_node
            record = _rule_from_fields(fields, "python")
            if record:
                records.append(record)
    return records


def extract_rules_from_mapping(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """配置形态的规则（已解析的 YAML / JSON）：`text` 支持集合、列表、逗号串。"""
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        words = normalize_words(item.get("text"))
        names = _match_names(item.get("match") or "")
        for channel, prefix in (("keywords", "found_keyword"), ("products", "found_product")):
            if _uses_channel(names, prefix):
                words += normalize_words(item.get(channel))
        records.append(
            {
                "label": label,
                "words": dedupe(words),
                "match": str(item.get("match") or ""),
                "source": "mapping",
            }
        )
    return records


# --------------------------------------------------------------------------- #
# 2. 标签名解析
# --------------------------------------------------------------------------- #


def parse_label_name(
    name: str,
    disposition_tokens: Sequence[str] = DEFAULT_DISPOSITION_TOKENS,
) -> dict[str, Any]:
    """`<应用>-<处置>[-<处置2>]-<可替换性>` → 四段结构。

    单段名视为自成一组：应用取自身，处置标为未标注。
    """
    parts = [part.strip() for part in re.split(r"[-－—]", name) if part.strip()]
    app = parts[0] if parts else name
    tail = parts[1:]

    disposition = ""
    for token in disposition_tokens:
        if any(token in part for part in tail):
            disposition = token
            break

    if any("不可替换" in part for part in tail):
        replaceable = "unreplaceable"
    elif any("可替换" in part for part in tail):
        replaceable = "replaceable"
    else:
        replaceable = "unknown"

    return {
        "app": app,
        "disposition": disposition or ("未标注" if not tail else "其他"),
        "replaceable": replaceable,
        "is_composite": bool(tail),
    }


# --------------------------------------------------------------------------- #
# 3. 词角色推断
# --------------------------------------------------------------------------- #


def _sets_within(node: ast.AST) -> set[str]:
    words: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Set):
            words |= {word for word in _literal_words(child)}
    return words


def _parse_lambda(match: Any) -> ast.Lambda | None:
    node = match
    if isinstance(match, str):
        text = match.strip()
        if not text:
            return None
        try:
            node = ast.parse(text, mode="eval").body
        except SyntaxError:
            return None
    return node if isinstance(node, ast.Lambda) else None


def analyze_match(match: Any) -> dict[str, Any]:
    """分析布尔 `match` 表达式，给出三类非触发词。

    只认三种结构：`not (...)`、`A - {...}`、三元 `if/else` 的条件部分。
    其它写法（如 `'x' not in found_text`）识别不了，`logic` 标为 `complex`，交人工复核。
    """
    result: dict[str, Any] = {
        "logic": "or",
        "exclude": [],
        "gate": [],
        "not_sufficient": [],
    }
    node = _parse_lambda(match)
    if node is None:
        return result

    body = node.body
    if not (isinstance(body, ast.Name) and body.id == "found_text"):
        result["logic"] = "complex"

    exclude: set[str] = set()
    gate: set[str] = set()
    not_sufficient: set[str] = set()
    for child in ast.walk(body):
        if isinstance(child, ast.UnaryOp) and isinstance(child.op, ast.Not):
            exclude |= _sets_within(child.operand)
        elif isinstance(child, ast.IfExp):
            gate |= _sets_within(child.test)
        elif (
            isinstance(child, ast.BinOp)
            and isinstance(child.op, ast.Sub)
            and isinstance(child.right, ast.Set)
        ):
            not_sufficient |= _sets_within(child.right)

    result["exclude"] = sorted(exclude)
    result["gate"] = sorted(gate)
    result["not_sufficient"] = sorted(not_sufficient)
    return result


def word_roles(rules: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """汇总成 `{word: {label: role}}`；触发词不落盘，缺省即触发。"""
    roles: dict[str, dict[str, str]] = {}
    for rule in rules:
        label = str(rule.get("label") or "")
        if not label:
            continue
        analysis = analyze_match(rule.get("match") or "")
        for word in analysis["exclude"]:
            roles.setdefault(word, {})[label] = EXCLUDE
        for word in analysis["gate"]:
            roles.setdefault(word, {})[label] = GATE
        for word in analysis["not_sufficient"]:
            roles.setdefault(word, {})[label] = NOT_SUFFICIENT
    return roles


# --------------------------------------------------------------------------- #
# 4. 分块编译
# --------------------------------------------------------------------------- #


def format_words(words: Sequence[str], limit: int) -> str:
    """前 `limit` 个词用顿号连接，超出部分标注总数。"""
    if not words:
        return "无"
    shown = list(words[:limit]) if limit else list(words)
    text = "、".join(shown)
    if limit and len(words) > len(shown):
        text += f" …（共 {len(words)} 个，仅列前 {len(shown)} 个）"
    return text


def split_by_chars(words: Sequence[str], limit: int) -> list[list[str]]:
    """把词装成若干块，每块拼起来（含顿号）不超过 `limit` 字。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for word in words:
        step = len(word) + 1
        if current and size + step > limit:
            blocks.append(current)
            current, size = [], 0
        current.append(word)
        size += step
    if current:
        blocks.append(current)
    return blocks


def compile_chunks(
    entries: Sequence[Mapping[str, Any]],
    *,
    max_words: int = 40,
    tail_chars: int = 380,
    marker: str = MARKER,
) -> list[dict[str, Any]]:
    """把标签档案编译成检索块：一条档案一个块，超长词表拆成续块。

    返回的每块带 `text`（给向量库）和 `words`（给断言），保证代表词之外的词
    全部落在续块里，不丢词。
    """
    chunks: list[dict[str, Any]] = []
    for entry in entries:
        label = str(entry.get("label") or "").strip()
        if not label:
            continue
        words = dedupe(entry.get("words") or [])
        app = str(entry.get("app") or "")
        disposition = str(entry.get("disposition") or "")

        lead = "｜".join(part for part in (label, app, disposition) if part)
        head_words = words[:max_words] if max_words else list(words)
        chunks.append(
            {
                "text": f"{marker}{lead}\n代表词：{format_words(head_words, 0)}",
                "label": label,
                "kind": "profile",
                "words": list(head_words),
            }
        )

        tail = words[max_words:] if max_words else []
        for index, block in enumerate(split_by_chars(tail, tail_chars), start=1):
            chunks.append(
                {
                    "text": f"{marker}{lead}｜词表续 {index}\n{'、'.join(block)}",
                    "label": label,
                    "kind": "tail",
                    "words": list(block),
                }
            )
    return chunks


def render_chunks(chunks: Sequence[Mapping[str, Any]]) -> list[str]:
    """只取文本，交给向量库建库上传。"""
    return [str(chunk["text"]) for chunk in chunks]


# --------------------------------------------------------------------------- #
# 5. 频率统计
# --------------------------------------------------------------------------- #


def document_frequency(
    rules: Sequence[Mapping[str, Any]],
    roles: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, list[str]]:
    """`word -> 把它当触发词的标签列表`（扣除 / 否决 / 开关词不计入）。"""
    index: dict[str, set[str]] = defaultdict(set)
    for rule in rules:
        label = str(rule.get("label") or "")
        if not label:
            continue
        for word in rule.get("words") or []:
            role = (roles or {}).get(word, {}).get(label, TRIGGER)
            if role == TRIGGER:
                index[word].add(label)
    return {word: sorted(labels) for word, labels in index.items()}


def chunk_word_coverage(
    entries: Sequence[Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """校验编译结果没丢词：逐标签比对源词表与块内词的集合。"""
    expected = {str(e["label"]): set(dedupe(e.get("words") or [])) for e in entries}
    actual: dict[str, set[str]] = defaultdict(set)
    for chunk in chunks:
        actual[str(chunk["label"])].update(chunk.get("words") or [])
    missing = {label: sorted(words - actual.get(label, set())) for label, words in expected.items()}
    missing = {label: words for label, words in missing.items() if words}
    return {"missing": missing, "ok": not missing}


__all__ = [
    "TRIGGER",
    "EXCLUDE",
    "GATE",
    "NOT_SUFFICIENT",
    "MARKER",
    "DEFAULT_DISPOSITION_TOKENS",
    "normalize_words",
    "dedupe",
    "extract_rules",
    "extract_rules_from_mapping",
    "parse_label_name",
    "analyze_match",
    "word_roles",
    "format_words",
    "split_by_chars",
    "compile_chunks",
    "render_chunks",
    "document_frequency",
    "chunk_word_coverage",
]
