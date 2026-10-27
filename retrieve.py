"""切块 + BM25 检索（纯内存，无外部依赖、无向量模型）。

中文用字 bigram 切词，英文/数字用词切分。对个人文档（几十到几千页）足够。
"""

from __future__ import annotations

import math
import re
from collections import Counter

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
_CJK_RUN = re.compile(f"[{CJK}]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9_\-\.]*")
_CJK_CHAR = re.compile(f"[{CJK}]")


def est_tokens(text: str) -> int:
    """粗略 token 估计：中文按 1 字 1 token，其余按 3.5 字符 1 token。"""
    if not text:
        return 0
    cjk = len(_CJK_CHAR.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 3.5) + 1


def tokenize(text: str) -> list[str]:
    low = text.lower()
    toks = _WORD.findall(low)
    for run in _CJK_RUN.findall(low):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def chunk_pages(doc_id: str, name: str, pages: list[dict],
                target: int = 900, overlap: int = 120) -> list[dict]:
    """把页块合并成 ~target token 的块，保留页码标签。"""
    chunks: list[dict] = []
    cur: list[str] = []
    cur_tokens = 0
    labels: list[str] = []

    def flush():
        nonlocal cur, cur_tokens, labels
        if not cur:
            return
        text = "\n".join(cur).strip()
        if text:
            lab = labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"
            chunks.append({
                "doc_id": doc_id,
                "doc": name,
                "label": lab,
                "text": text,
                "tokens": est_tokens(text),
            })
        cur, cur_tokens, labels = [], 0, []

    for page in pages:
        text = page["text"]
        if not text:
            continue
        paras = [p for p in text.split("\n") if p.strip()]
        for para in paras:
            t = est_tokens(para)
            if cur_tokens + t > target and cur_tokens > target * 0.4:
                flush()
            cur.append(para)
            cur_tokens += t
            if not labels or labels[-1] != page["label"]:
                labels.append(page["label"])
        if cur_tokens >= target:
            flush()

    flush()
    # 页数很少时避免产生一堆碎块
    return chunks


class Index:
    """BM25 倒排索引，随文档变化整体重建。"""

    K1 = 1.2
    B = 0.75

    def __init__(self) -> None:
        self.chunks: list[dict] = []
        self._tfs: list[Counter] = []
        self._lens: list[int] = []
        self._df: Counter = Counter()
        self._avg = 0.0

    def build(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self._tfs = []
        self._lens = []
        self._df = Counter()
        for c in chunks:
            tf = Counter(tokenize(c["text"]))
            self._tfs.append(tf)
            self._lens.append(sum(tf.values()))
            self._df.update(tf.keys())
        self._avg = (sum(self._lens) / len(self._lens)) if self._lens else 0.0

    def search(self, query: str, budget: int = 14000, top_k: int = 40) -> list[int]:
        if not self.chunks:
            return []
        q = [t for t in tokenize(query) if len(t) > 0]
        if not q:
            return list(range(min(top_k, len(self.chunks))))
        n = len(self.chunks)
        scores: list[float] = []
        for i, tf in enumerate(self._tfs):
            s = 0.0
            dl = self._lens[i] or 1
            for term in q:
                f = tf.get(term)
                if not f:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                s += idf * (f * (self.K1 + 1)) / (
                    f + self.K1 * (1 - self.B + self.B * dl / (self._avg or 1))
                )
            scores.append(s)
        order = sorted(range(n), key=lambda i: scores[i], reverse=True)[:top_k]
        picked, used = [], 0
        for i in order:
            if scores[i] <= 0:
                break
            t = self.chunks[i]["tokens"]
            if used + t > budget and picked:
                continue
            picked.append(i)
            used += t
            if used >= budget:
                break
        # 按文档/页序返回，读起来更连贯
        picked.sort()
        return picked


def build_context(chunks: list[dict], idxs: list[int],
                  start: int = 1) -> tuple[str, list[dict]]:
    """编号资料块，返回 (context 文本, sources)。编号从 start 开始。"""
    parts, sources = [], []
    for n, i in enumerate(idxs, start):
        c = chunks[i]
        parts.append(f"[{n}] 《{c['doc']}》 {c['label']}\n{c['text']}")
        sources.append({
            "n": n,
            "doc": c["doc"],
            "label": c["label"],
            "tokens": c["tokens"],
            "preview": c["text"][:1200],
        })
    return "\n\n".join(parts), sources
