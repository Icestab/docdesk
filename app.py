#!/usr/bin/env python3
"""docdesk：本地一次性文档分析台。

设计约束：
  * 全程不落盘。上传的文档、抽取的文本、聊天记录只存在于本进程内存里。
  * 不写 __pycache__、不写临时文件、不打访问日志、不产生 core dump（由启动脚本 ulimit -c 0）。
  * 只通过 HTTP 与本机 llama-server 通信，模型服务只监听 127.0.0.1。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import extract
import retrieve

HERE = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(HERE, "ui.html")

FULL_LIMIT = 42000          # 全文塞进上下文的阈值（token）
HARD_LIMIT = 78000          # 再多就不塞了
RETRIEVE_BUDGET = 13000     # 检索模式喂给模型的资料 token
MAX_UPLOAD = 160 * 1024 * 1024
MAX_HISTORY = 8


SYSTEM_PROMPT = """你是本机离线的文档分析助手，运行在一台不联网的机器上。

工作规则：
1. 只依据本轮给出的《资料》回答。若用了资料外的常识，必须在该处标注"（资料外）"。
2. 每个事实性结论后面标注来源编号，如 [1] 或 [2][5]。编号只能来自资料，绝不能虚构。
3. 资料里没有的内容，直接回答"资料中未提及"，并说明还缺什么，不要猜测或用常识补全。
4. 数字、日期、金额、百分比、人名、地名、条款号、药名一律照抄原文，不做换算、不四舍五入、不翻译。
5. 默认用简体中文。结构用 Markdown（小标题、列表、表格）；除非用户要求，不要大段复述原文。
6. 今天是 {today}。资料里的相对时间（"上月""去年年底"）若能推出绝对时间，请用括号注明。
"""

SUMMARY_TASK = """请对下面这批资料做结构化摘要，供后续汇总使用：
1. 提取事实要点，每条一句话，尽量保留原文的数字、日期、金额、人名、条款号。
2. 长文档请覆盖开头、主体与结尾，不要只总结第一段。
3. 每条要点后用 [编号] 标注出处，编号必须来自本批资料，**每条最多标两个编号**，不要写编号区间。
4. 不要评论、不要建议、不要编造。资料没有的就跳过。
资料：
{context}"""

FINAL_TASK = """下面是关于同一批文件的分段笔记。请把它们合成为一份完整摘要：
- 开头 2-3 句话说明这批文件是什么、涉及谁、时间跨度。
- 之后按主题分节（若文件里天然有章节/条款，就按原文结构）。
- 保留全部关键数字、日期、金额、人名、条款编号，合并重复内容。
- 每个结论保留原有的 [编号] 引用，每条最多两个，不要写"[1]–[9]"这种区间。
- 最后列出"资料中未提及/无法确定"的疑点。
分段笔记：
{notes}"""


PRESETS = {
    "summary": "请给出这份材料的完整摘要：它是什么、涉及谁、什么时候、核心内容分几块、结论是什么。",
    "points": "把要点列成清单，每条一句话，保留所有关键数字、日期、人名、条款号，并在每条后标注来源编号。",
    "timeline": "按时间顺序整理出所有带日期的事件或节点，做成表格（时间 | 事件 | 来源）。日期不确定的单独列一节。",
    "risks": "找出材料中的风险、义务、违约责任、期限、金额陷阱和模糊表述，逐条列出并说明依据（标注来源）。",
    "numbers": "把材料中出现的所有关键数字、金额、比例、日期、期限抽成表格（项目 | 数值 | 上下文 | 来源），不要遗漏。",
    "qa": "如果我要就这份材料做决策，最需要先弄清楚的 8 个问题是什么？每个问题给出资料里已有的答案或缺口。",
    "actions": "列出材料要求我做的事：待办、截止日期、责任人、需要提交的材料，做成清单并标注来源。",
}


class Desk:
    """会话状态，全部在内存里。"""

    def __init__(self, idle_secs: float) -> None:
        self.docs: list[dict] = []
        self.chunks: list[dict] = []
        self.index = retrieve.Index()
        self.history: list[dict] = []
        self.lock = threading.Lock()
        self.last_seen = time.time()
        self.idle_secs = idle_secs
        self.started = time.time()

    # ---------------- 文档
    def add(self, name: str, data: bytes) -> dict:
        res = extract.extract(name, data)
        doc_id = secrets.token_hex(4)
        pages = res["pages"]
        tokens = sum(retrieve.est_tokens(p["text"]) for p in pages)
        meta = {
            "id": doc_id,
            "name": name,
            "kind": res["kind"],
            "chars": res["chars"],
            "tokens": tokens,
            "pages": len(pages),
            "scanned": res["scanned"],
            "ocr": len(res.get("ocr") or []),
        }
        with self.lock:
            self.docs.append(meta)
            self.chunks.extend(retrieve.chunk_pages(doc_id, name, pages))
            self.index.build(self.chunks)
            self.history = []
        return meta

    def total_tokens(self) -> int:
        return sum(d["tokens"] for d in self.docs)

    def reindex(self) -> None:
        self.index.build(self.chunks)

    def reset(self) -> None:
        with self.lock:
            self.docs, self.chunks, self.history = [], [], []

    def snapshot(self) -> dict:
        return {
            "docs": self.docs,
            "total_tokens": self.total_tokens(),
            "elapsed": int(time.time() - self.started),
            "idle_left": max(0, int(self.idle_secs - (time.time() - self.last_seen))),
        }


def _post(payload: dict, timeout: int = 900):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL, data=body, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def llm_stream(messages, max_tokens=1600, temperature=0.2):
    payload = {"model": "local", "messages": messages, "stream": True,
               "temperature": temperature, "top_p": 0.9,
               "max_tokens": max_tokens, "cache_prompt": True}
    try:
        resp = _post(payload)
    except urllib.error.URLError as exc:
        raise RuntimeError("模型服务连不上: %s" % exc) from exc
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body.startswith("["):
                break
            if not body:
                continue
            try:
                obj = json.loads(body)
            except ValueError:
                continue
            if obj.get("error"):
                raise RuntimeError(str(obj["error"])[:300])
            ch = (obj.get("choices") or [{}])[0]
            piece = (ch.get("delta") or {}).get("content") or ""
            if piece:
                yield piece


def llm_once(messages, max_tokens=1200, temperature=0.2):
    payload = {"model": "local", "messages": messages, "stream": False,
               "temperature": temperature, "top_p": 0.9,
               "max_tokens": max_tokens, "cache_prompt": True}
    resp = _post(payload)
    with resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return (data["choices"][0]["message"]["content"] or "").strip()


def pick_mode(desk, mode):
    total = desk.total_tokens()
    if mode == "full" and total > HARD_LIMIT:
        return "retrieve"
    if mode == "auto":
        return "full" if total <= FULL_LIMIT else "retrieve"
    return mode


def build_turn(desk, question, mode):
    mode = pick_mode(desk, mode)
    chunks = desk.chunks
    if mode == "full":
        idxs = list(range(len(chunks)))
    else:
        q = question
        if desk.history:
            q = desk.history[-1]["q"] + " " + question
        idxs = desk.index.search(q, budget=RETRIEVE_BUDGET)
    context, sources = retrieve.build_context(chunks, idxs)
    msgs = [{"role": "system",
             "content": SYSTEM_PROMPT.format(today=str(date.today()))}]
    for h in desk.history[-MAX_HISTORY:]:
        msgs.append({"role": "user", "content": h["q"]})
        msgs.append({"role": "assistant", "content": h["a"]})
    msgs.append({"role": "user",
                 "content": "[资料]（共 %d 条，引用编号只能用 1-%d）\n%s\n[资料结束]\n\n问题：%s"
                 % (len(sources), len(sources), context, question)})
    return msgs, sources, mode


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "docdesk"

    def log_message(self, *args):
        return

    def _authed(self):
        if not TOKEN:
            return True
        if (self.headers.get("Cookie") or "").find("dd=" + TOKEN) >= 0:
            return True
        if self.headers.get("X-Token") == TOKEN:
            return True
        return ("t=" + TOKEN) in (self.path or "")

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_UPLOAD:
            raise ValueError("请求体太大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _emit(self, obj):
        line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def do_GET(self):
        if not self._authed():
            self._json({"error": "未授权：链接需要带 ?t= 令牌"}, 403)
            return
        if self.path.startswith("/api/state"):
            self._json(DESK.snapshot())
            return
        if self.path.startswith("/healthz"):
            self._json({"ok": True, "docs": len(DESK.docs)})
            return
        self._serve_ui()

    def _serve_ui(self):
        try:
            with open(UI_PATH, "rb") as fh:
                raw = fh.read()
        except OSError:
            self._json({"error": "ui.html 不见了"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        if TOKEN:
            self.send_header("Set-Cookie",
                             "dd=%s; HttpOnly; SameSite=Strict; Path=/" % TOKEN)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if not self._authed():
            self._json({"error": "未授权"}, 403)
            return
        DESK.last_seen = time.time()
        try:
            body = self._body()
            path = self.path
            if path.startswith("/api/upload"):
                self._upload(body)
            elif path.startswith("/api/chat"):
                self._chat(body)
            elif path.startswith("/api/summary"):
                self._summary(body)
            elif path.startswith("/api/reset"):
                DESK.reset()
                self._json({"ok": True})
            elif path.startswith("/api/close"):
                self._json({"ok": True})
                request_stop()
            else:
                self._json({"error": "没有这个接口"}, 404)
        except extract.ExtractError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def _upload(self, body):
        name = (body.get("name") or "粘贴的文本").strip()[:120]
        b64 = body.get("b64") or ""
        if not b64:
            self._json({"error": "空文件"}, 400)
            return
        data = base64.b64decode(b64)
        meta = DESK.add(name, data)
        out = DESK.snapshot()
        out["added"] = meta
        self._json(out)

    def _chat(self, body):
        question = (body.get("question") or "").strip()
        preset = body.get("preset")
        if preset in PRESETS:
            question = PRESETS[preset] + ("\n\n补充要求：" + question if question else "")
        if not question:
            self._json({"error": "问题是空的"}, 400)
            return
        if not DESK.docs:
            self._json({"error": "还没有文档"}, 400)
            return
        try:
            msgs, sources, mode = build_turn(DESK, question,
                                             body.get("mode") or "auto")
        except Exception as exc:
            self._json({"error": str(exc)}, 500)
            return
        self._sse_open()
        self._emit({"type": "meta", "mode": mode, "sources": sources[:240],
                    "tokens": sum(s["tokens"] for s in sources)})
        parts = []
        t0 = time.time()
        try:
            mx = int(body.get("max_tokens") or 1600)
            for piece in llm_stream(msgs, max_tokens=mx):
                parts.append(piece)
                self._emit({"type": "delta", "text": piece})
            answer = "".join(parts).strip()
            DESK.history.append({"q": question, "a": answer})
            del DESK.history[:-MAX_HISTORY]
            self._emit({"type": "done", "chars": len(answer),
                        "secs": round(time.time() - t0, 1), "mode": mode})
        except Exception as exc:
            try:
                self._emit({"type": "error", "text": str(exc)})
            except Exception:
                pass

    def _summary(self, body):
        if not DESK.docs:
            self._json({"error": "还没有文档"}, 400)
            return
        extra = (body.get("question") or "").strip()
        chunks = DESK.chunks
        batches, cur, used, base = [], [], 0, 0
        for i, c in enumerate(chunks):
            if used + c["tokens"] > 11000 and cur:
                batches.append((base, cur))
                base, cur, used = i, [], 0
            cur.append(c)
            used += c["tokens"]
        if cur:
            batches.append((base, cur))
        self._sse_open()
        all_sources = [{"n": i + 1, "doc": c["doc"], "label": c["label"],
                        "tokens": c["tokens"], "preview": c["text"][:600]}
                       for i, c in enumerate(chunks)][:240]
        self._emit({"type": "meta", "mode": "summary",
                    "sources": all_sources, "tokens": DESK.total_tokens()})
        notes = []
        try:
            for k, (start, batch) in enumerate(batches, 1):
                ctx, _ = retrieve.build_context(
                    batch, list(range(len(batch))), start + 1)
                self._emit({"type": "stage",
                            "text": "正在阅读 %d/%d 段，约 %d 字" % (
                                k, len(batches), sum(len(c["text"]) for c in batch))})
                note = llm_once([
                    {"role": "system", "content": SYSTEM_PROMPT.format(today=str(date.today()))},
                    {"role": "user", "content": SUMMARY_TASK.format(context=ctx)},
                ], max_tokens=900)
                notes.append("### 片段 %d\n%s" % (k, note))
            joined = "\n\n".join(notes)
            if extra:
                joined += "\n\n用户特别关心：" + extra
            self._emit({"type": "stage", "text": "正在汇总成全文摘要…"})
            t0 = time.time()
            parts = []
            for piece in llm_stream([
                {"role": "system", "content": SYSTEM_PROMPT.format(today=str(date.today()))},
                {"role": "user", "content": FINAL_TASK.format(notes=joined)},
            ], max_tokens=2200):
                parts.append(piece)
                self._emit({"type": "delta", "text": piece})
            answer = "".join(parts).strip()
            DESK.history.append({"q": "全文摘要", "a": answer[:1200]})
            del DESK.history[:-MAX_HISTORY]
            self._emit({"type": "done", "chars": len(answer),
                        "secs": round(time.time() - t0, 1), "mode": "summary"})
        except Exception as exc:
            try:
                self._emit({"type": "error", "text": str(exc)})
            except Exception:
                pass


def request_stop():
    threading.Thread(target=SERVER.shutdown, daemon=True).start()


def main():
    global LLM_URL, TOKEN, DESK, SERVER
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--idle", type=float, default=30)
    ap.add_argument("--llm", default="http://127.0.0.1:8080/v1/chat/completions")
    ap.add_argument("--token", default="")
    args = ap.parse_args()
    LLM_URL = args.llm
    TOKEN = args.token or (secrets.token_urlsafe(12) if args.lan else "")
    DESK = Desk(args.idle * 60 if args.idle > 0 else 10 ** 9)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    SERVER = ThreadingHTTPServer((host, args.port), Handler)
    SERVER.daemon_threads = True

    def watch():
        while True:
            time.sleep(5)
            if time.time() - DESK.last_seen > DESK.idle_secs:
                SERVER.shutdown()
                return

    threading.Thread(target=watch, daemon=True).start()
    shown = "127.0.0.1"
    if args.lan:
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("10.255.255.255", 1))
            shown = sock.getsockname()[0]
            sock.close()
        except OSError:
            shown = "0.0.0.0"
    suffix = ("/?t=" + TOKEN) if TOKEN else "/"
    print("docdesk ready: http://%s:%d%s" % (shown, args.port, suffix), flush=True)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        DESK.reset()
        SERVER.server_close()
    os._exit(0)


if __name__ == "__main__":
    main()
