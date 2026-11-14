"""
DeepSeek Research — cited deep-research reports over DeepSeek's server-side
web search with thinking enabled.

Flow:
    GET  /                        topic form (+ recent reports)
    POST /research                validates, locks, stores a running row, spawns
                                  the background generation, 302 -> /reports/<id>
    GET  /reports/<id>            progress page while running; rendered cited
                                  markdown once done
    GET  /reports/<id>/stream     SSE live progress (phases: searching with real
                                  tool-call counts -> thinking -> writing)
    GET  /reports/<id>/export.md  raw markdown download
    GET  /health                  liveness probe (dotless, no external calls)

Mechanics: ONE streaming Chat Completions call with the deepseek_web_search
server tool + thinking enabled + safety_check high (public-facing duty,
default ON — documented in README/listing). The server consumes the SSE
stream (no data: [DONE] reliance — parses until stream end, tolerating
keep-alive comment lines), counts tool calls for the progress UI, passes
nothing back (single turn — server-tool blocks stay upstream), accumulates
the final text plus citations from BOTH shapes (final-chunk
delta.server_tool_calls[].calls[].sources[] and any message annotations),
and stores the report in SQLite on the /data volume.

Cost/time honesty: deep reports run minutes and ~5-20 searches
(~$0.75 per 1,000 search/fetch uses + token usage — billed to the deployer's
DeepSeek account). One generation at a time (in-process lock, single uvicorn
worker; 429 with a friendly message when busy). MAX_REPORTS FIFO retention.
max_tokens is bounded (default 12,000) because thinking consumes the output
budget; a finish_reason=length report is stored but flagged as truncated.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

# ---------------------------------------------------------------- config

DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
SAFETY_CHECK_LEVEL = "high"  # provider-side safety review, default ON for a public-facing app
MAX_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "12000"))
MAX_REPORTS = int(os.environ.get("MAX_REPORTS", "100"))
MAX_TOPIC_LEN = 2000

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or (
    "/data" if os.path.isdir("/data") else os.path.join(_APP_DIR, "..", "data")
)
DB_PATH = os.path.join(DATA_DIR, "reports.db")

APP_ACCESS_PASSWORD = os.environ.get("APP_ACCESS_PASSWORD", "").strip()

# List honesty: list prices at peak (01:00-04:00 + 06:00-10:00 UTC Mon-Fri).
SEARCH_PRICE_PER_1K = 0.75   # per 1,000 search/fetch uses
IN_PRICE_PER_M = 0.30        # flash input, per 1M tokens
OUT_PRICE_PER_M = 1.20       # flash output, per 1M tokens

UPSTREAM_TIMEOUT = httpx.Timeout(900.0, connect=15.0)
REPORT_PROMPT = (
    "Write a thorough, well-structured research report in Markdown on the topic below.\n\n"
    "Requirements:\n"
    "- Begin with a single H1 title that names the topic.\n"
    "- Structure: an executive summary, key findings with H2/H3 sections, "
    "a short analysis, and a conclusion.\n"
    "- Cite sources inline where claims are made, and end with a 'Sources' section "
    "listing every source used with title and URL.\n"
    "- Prefer primary or high-quality sources; note publication dates where recency matters.\n"
    "- Stay factual and neutral; flag uncertainty or disagreement between sources.\n"
    "- Keep the report focused: roughly 800-1500 words unless the topic demands more.\n\n"
    "Topic: {topic}"
)

# ---------------------------------------------------------------- storage


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL,
                error TEXT,
                markdown TEXT,
                citations_json TEXT,
                usage_json TEXT,
                finish_reason TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        # A restart kills in-flight generations (in-process task); mark them honestly.
        conn.execute(
            "UPDATE reports SET status='failed', error='interrupted by service restart' "
            "WHERE status='running'"
        )


init_db()


def _db_get(report_id: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return dict(row) if row else None


def _prune_finished() -> None:
    """Keep at most MAX_REPORTS finished rows (in-flight exempt). Called after
    inserting a new row and again after a row reaches terminal state, so idle
    state never exceeds the cap."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM reports WHERE id IN ("
            "  SELECT id FROM reports WHERE status != 'running'"
            "  ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (MAX_REPORTS,),
        )


# ---------------------------------------------------------------- markdown (XSS-safe)

def _build_renderer() -> MarkdownIt:
    # "js-default": the markdown-it JS default preset — html DISABLED, so raw HTML in
    # model output is escaped, never executed. The default validateLink policy rejects
    # javascript:/vbscript:/file: (and non-image data:) hrefs.
    md = MarkdownIt("js-default", {"linkify": True, "breaks": True})

    def link_open(tokens, idx, options, env):
        token = tokens[idx]
        href = token.attrGet("href") or ""
        if href.startswith(("http://", "https://")):
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener noreferrer")
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["link_open"] = link_open
    return md


_MD = _build_renderer()


def render_report_html(md_text: str) -> str:
    return _MD.render(md_text or "")


# ---------------------------------------------------------------- citations / usage

def _citations_from_annotations(annotations: list) -> list:
    out, seen = [], set()
    for ann in annotations or []:
        if not isinstance(ann, dict):
            continue
        uc = ann.get("url_citation") or ann.get("web_search_result_location") or {}
        if not isinstance(uc, dict):
            continue
        url = uc.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": uc.get("title") or url, "url": url})
    return out


def _citations_from_tool_calls(records: list) -> list:
    """Streaming shape (verified live on the sibling build): sources ride in
    delta.server_tool_calls[].calls[].sources[] as {type:'url', url, title, snippet}."""
    out, seen = [], set()
    for stc in records or []:
        if not isinstance(stc, dict):
            continue
        for call in stc.get("calls") or []:
            if not isinstance(call, dict):
                continue
            for src in call.get("sources") or []:
                if not isinstance(src, dict) or src.get("type") != "url":
                    continue
                url = src.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                item = {"title": src.get("title") or url, "url": url}
                if src.get("snippet"):
                    item["cited_text"] = src["snippet"]
                out.append(item)
    return out


def _merge_citations(primary: list, fallback: list) -> list:
    out = list(primary)
    seen = {c.get("url") for c in primary if isinstance(c, dict)}
    for c in fallback or []:
        if isinstance(c, dict) and c.get("url") and c["url"] not in seen:
            out.append(c)
            seen.add(c["url"])
    return out


def _safe_citations(cits: list) -> list:
    """Only http(s) citation URLs reach the template (href XSS hygiene)."""
    return [
        c for c in cits
        if isinstance(c, dict) and isinstance(c.get("url"), str)
        and c["url"].startswith(("http://", "https://"))
    ]


def _usage_out(usage: Optional[dict], tool_calls: int) -> dict:
    u = usage or {}
    stu = u.get("server_tool_use") or {}
    out = {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
        "search_calls": stu.get("tool_search") or 0,
        "fetch_calls": stu.get("tool_fetch") or 0,
        "image_search_calls": stu.get("tool_image_search") or 0,
        "tool_calls": tool_calls,
    }
    if not (out["search_calls"] or out["fetch_calls"] or out["image_search_calls"]):
        out["search_calls"] = tool_calls  # best-effort fallback when counters are absent
    return out


def _estimate_cost(usage: dict) -> float:
    uses = (usage.get("search_calls") or 0) + (usage.get("fetch_calls") or 0) \
        + (usage.get("image_search_calls") or 0)
    tin = usage.get("input_tokens") or 0
    tout = usage.get("output_tokens") or 0
    return uses / 1000.0 * SEARCH_PRICE_PER_1K + tin / 1e6 * IN_PRICE_PER_M \
        + tout / 1e6 * OUT_PRICE_PER_M


def _extract_title(content: str, topic: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:150]
    return topic[:150]


# ---------------------------------------------------------------- progress state

_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()
GEN_LOCK = threading.Lock()  # one deep report at a time (single uvicorn worker)
_tasks: set = set()


def _set_progress(report_id: str, **fields: Any) -> None:
    with _progress_lock:
        prog = _progress.setdefault(
            report_id,
            {"phase": "starting", "tool_calls": 0, "searches": 0, "fetches": 0,
             "queries": [], "thinking_head": "", "text_chars": 0, "done": False},
        )
        prog.update(fields)


# ---------------------------------------------------------------- generation

def _upstream_error_text(status: int, body: bytes) -> str:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or ""
    elif isinstance(err, str):
        msg = err
    else:
        msg = ""
    if status == 401 and not msg:
        msg = "Upstream rejected the configured DEEPSEEK_API_KEY."
    return "Upstream error (HTTP %s): %s" % (status, msg or repr(body[:200]))


async def generate_report(report_id: str, topic: str, max_tokens: int) -> None:
    """Runs as a background asyncio task; releases GEN_LOCK in finally."""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    try:
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set on this deployment — add it under "
                "Variables (your key from platform.deepseek.com), then redeploy."
            )
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": REPORT_PROMPT.format(topic=topic)}],
            "tools": [{"type": "deepseek_web_search"}],
            # thinking + search = deep search: multi-step server-side retrieval.
            # Deliberately ENABLED here (unlike extraction wrappers — thinking is
            # the point of a research app). max_tokens is bounded because thinking
            # consumes the same output budget.
            "thinking": {"type": "enabled"},
            "safety_check": {"level": SAFETY_CHECK_LEVEL},
            "stream": True,
            "max_tokens": max_tokens,
        }
        parts: list = []
        stc_records: list = []
        annotations: list = []
        usage: Optional[dict] = None
        finish_reason: Optional[str] = None
        refusal_text: Optional[str] = None
        seen_call_ids: set = set()

        _set_progress(report_id, phase="searching", started=True)
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{DEEPSEEK_BASE}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(_upstream_error_text(resp.status_code, body))
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # skips ': keep-alive' comments and empty lines
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break  # tolerated if present, never relied upon
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(
                            "Upstream reported an error mid-stream: "
                            + json.dumps(chunk["error"])[:300]
                        )
                    choices = chunk.get("choices") or [{}]
                    choice = choices[0] if choices else {}
                    delta = choice.get("delta") or {}

                    if delta.get("content"):
                        parts.append(delta["content"])
                        _set_progress(report_id, phase="writing",
                                      text_chars=sum(len(p) for p in parts))

                    rc = delta.get("reasoning_content")
                    if rc:
                        with _progress_lock:
                            prog = _progress[report_id]
                            head = (prog.get("thinking_head") or "") + rc
                            prog["thinking_head"] = head[-220:]
                            if prog.get("phase") != "writing":
                                prog["phase"] = "thinking"

                    if delta.get("refusal"):
                        refusal_text = delta["refusal"]

                    ann = delta.get("annotations")
                    if ann:
                        annotations.extend(ann if isinstance(ann, list) else [ann])

                    stc = delta.get("server_tool_calls")
                    if stc:
                        if not isinstance(stc, list):
                            stc = [stc]
                        stc_records.extend(stc)
                        new_calls, queries = 0, []
                        for block in stc:
                            for call in (block.get("calls") or []):
                                if not isinstance(call, dict):
                                    continue
                                cid = call.get("id")
                                if cid and cid in seen_call_ids:
                                    continue
                                if cid:
                                    seen_call_ids.add(cid)
                                new_calls += 1
                                inp = call.get("input") if isinstance(call.get("input"), dict) else {}
                                q = inp.get("queries") or call.get("queries")
                                if isinstance(q, list):
                                    queries.extend(str(x) for x in q[:2])
                        if new_calls:
                            with _progress_lock:
                                prog = _progress[report_id]
                                prog["tool_calls"] = prog.get("tool_calls", 0) + new_calls
                                if queries:
                                    prog["queries"] = (prog.get("queries") or [])[-2:] + queries[:2]
                                if prog.get("phase") == "starting":
                                    prog["phase"] = "searching"

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]

        content = "".join(parts)
        citations = _safe_citations(_merge_citations(
            _citations_from_annotations(annotations),
            _citations_from_tool_calls(stc_records),
        ))
        tool_calls = len(seen_call_ids) or sum(
            len(b.get("calls") or []) for b in stc_records
        )
        usage_out = _usage_out(usage, tool_calls)
        title = _extract_title(content, topic)

        if finish_reason == "content_filter" or refusal_text:
            status = "refused"
            error = refusal_text or "The request was declined by the provider's safety review."
            markdown, finish = None, finish_reason
        elif not content and finish_reason == "length":
            # Whole output budget burned on reasoning with zero report text.
            status = "failed"
            error = ("The model spent its entire output budget on reasoning without "
                     "producing report text — try a narrower topic.")
            markdown, finish = None, finish_reason
        elif content:
            status = "done"
            error = None
            markdown, finish = content, finish_reason
        else:
            status = "failed"
            error = "Upstream produced no report text (finish_reason=%s)." % finish_reason
            markdown, finish = None, finish_reason

        with _db() as conn:
            conn.execute(
                "UPDATE reports SET title=?, status=?, error=?, markdown=?, "
                "citations_json=?, usage_json=?, finish_reason=? WHERE id=?",
                (title, status, error, markdown, json.dumps(citations, ensure_ascii=False),
                 json.dumps(usage_out, ensure_ascii=False), finish, report_id),
            )
        _prune_finished()
        _set_progress(report_id, done=True, ok=(status == "done"), status=status, phase="done")

    except Exception as exc:  # noqa: BLE001 — one honest failure path
        err = str(exc)[:500]
        try:
            with _db() as conn:
                conn.execute(
                    "UPDATE reports SET status='failed', error=? WHERE id=?",
                    (err, report_id),
                )
            _prune_finished()
        except sqlite3.Error:
            pass
        _set_progress(report_id, done=True, ok=False, status="failed", error=err)
    finally:
        try:
            GEN_LOCK.release()
        except RuntimeError:
            pass


def _spawn(report_id: str, topic: str, max_tokens: int) -> None:
    task = asyncio.get_running_loop().create_task(generate_report(report_id, topic, max_tokens))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- auth helper

def _auth_cookie_value() -> str:
    return hashlib.sha256(("research-auth:" + APP_ACCESS_PASSWORD).encode("utf-8")).hexdigest()[:32]


def _auth_ok(provided_pw: str, request: Request) -> bool:
    if not APP_ACCESS_PASSWORD:
        return True
    if provided_pw and provided_pw == APP_ACCESS_PASSWORD:
        return True
    return (request.cookies.get("research_auth") or "") == _auth_cookie_value()


# ---------------------------------------------------------------- app

app = FastAPI(
    title="DeepSeek Research",
    description="Cited deep-research reports from one topic, with live progress. "
                "Retrieval runs on DeepSeek's server-side web search.",
    version="1.0.0",
)
templates = Jinja2Templates(directory=os.path.join(_APP_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(_APP_DIR, "static")), name="static")


def _page(request: Request, status_code: int, title: str, detail: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "message.html",
        {"title": title, "detail": detail}, status_code=status_code,
    )


@app.get("/health")
async def health():
    with _db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
    return {
        "ok": True,
        "service": "deepseek-research",
        "key_configured": bool((os.environ.get("DEEPSEEK_API_KEY") or "").strip()),
        "reports": n,
        "max_reports": MAX_REPORTS,
    }


@app.get("/")
async def index(request: Request):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, topic, title, status, created_at FROM reports "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    return templates.TemplateResponse(request, "index.html", {
        "recent": [dict(r) for r in rows],
        "password_required": bool(APP_ACCESS_PASSWORD),
        "max_reports": MAX_REPORTS,
        "model": MODEL,
    })


@app.post("/research")
async def research(request: Request, topic: str = Form(""), access_password: str = Form("")):
    topic = (topic or "").strip()
    if not topic:
        return _page(request, 400, "No topic given", "Please enter a research topic.")
    if len(topic) > MAX_TOPIC_LEN:
        return _page(request, 400, "Topic too long", f"Keep the topic under {MAX_TOPIC_LEN} characters.")
    if not _auth_ok(access_password, request):
        return _page(request, 401, "Password required",
                     "This deployment gates report creation with APP_ACCESS_PASSWORD "
                     "(see the Variables tab). Reading existing report links stays open.")
    if not GEN_LOCK.acquire(blocking=False):
        return _page(request, 429, "One report at a time",
                     "A deep report is currently running (reports take a few minutes and "
                     "up to ~20 searches). Please try again shortly.")
    report_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            "INSERT INTO reports (id, topic, status, created_at) VALUES (?, ?, 'running', ?)",
            (report_id, topic, now),
        )
        # FIFO retention, in-flight reports exempt
        _prune_finished()
    _set_progress(report_id, phase="starting")
    _spawn(report_id, topic, MAX_TOKENS)
    resp = RedirectResponse(f"/reports/{report_id}", status_code=302)
    if APP_ACCESS_PASSWORD and access_password == APP_ACCESS_PASSWORD:
        resp.set_cookie("research_auth", _auth_cookie_value(), httponly=True, samesite="lax")
    return resp


@app.get("/reports/{report_id}")
async def report_page(request: Request, report_id: str):
    r = _db_get(report_id)
    if r is None:
        return _page(request, 404, "Report not found", "Check the link, or start a new report.")
    ctx: dict = {
        "report": r,
        "report_html": "",
        "citations": [],
        "usage": None,
        "cost": None,
        "truncated": False,
        "progress": None,
        "password_required": bool(APP_ACCESS_PASSWORD),
    }
    if r["status"] == "done":
        usage = json.loads(r["usage_json"] or "{}")
        ctx.update(
            report_html=render_report_html(r["markdown"] or ""),
            citations=json.loads(r["citations_json"] or "[]"),
            usage=usage,
            cost=_estimate_cost(usage),
            truncated=(r["finish_reason"] == "length"),
        )
    elif r["status"] == "running":
        ctx["progress"] = dict(_progress.get(report_id) or {})
    return templates.TemplateResponse(request, "report.html", ctx)


@app.get("/reports/{report_id}/stream")
async def report_stream(report_id: str):
    async def gen():
        last_payload = None
        last_keepalive = time.time()
        while True:
            prog = _progress.get(report_id)
            if prog is None:
                row = _db_get(report_id)
                if row is None:
                    yield "event: error\ndata: {\"message\": \"report not found\"}\n\n"
                    return
                if row["status"] != "running":
                    yield "event: done\ndata: %s\n\n" % json.dumps(
                        {"ok": row["status"] == "done", "status": row["status"]})
                    return
            else:
                payload = {k: prog.get(k) for k in (
                    "phase", "tool_calls", "searches", "fetches",
                    "thinking_head", "text_chars", "queries")}
                if payload != last_payload:
                    last_payload = payload
                    yield "event: phase\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
                if prog.get("done"):
                    yield "event: done\ndata: %s\n\n" % json.dumps(
                        {"ok": bool(prog.get("ok")), "status": prog.get("status"),
                         "error": prog.get("error")})
                    return
            if time.time() - last_keepalive > 15:
                last_keepalive = time.time()
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.7)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.get("/reports/{report_id}/export.md")
async def export_md(request: Request, report_id: str):
    r = _db_get(report_id)
    if r is None:
        return _page(request, 404, "Report not found", "No such report.")
    if r["status"] != "done" or not r["markdown"]:
        return _page(request, 409, "Not ready yet", "This report has not finished generating.")
    return PlainTextResponse(
        r["markdown"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="deepseek-report-{report_id}.md"'},
    )
