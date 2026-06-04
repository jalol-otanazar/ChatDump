import json
import re
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()

# ─── HTTP fetch — impersonates real Chrome TLS fingerprint ───────────────────
# This bypasses Cloudflare without needing any browser installed.

def fetch_page(url: str) -> str:
    resp = cffi_requests.get(
        url,
        impersonate="chrome124",   # spoof Chrome 124 TLS fingerprint
        timeout=20,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
        },
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


# ─── Strategy 1: __NEXT_DATA__ JSON (Next.js SSR) ────────────────────────────
# Claude.ai SSR-embeds the full conversation as JSON in every share page.
# This is the most reliable extraction path.

def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _parse_message_list(raw: list) -> list[dict]:
    result = []
    for m in raw:
        sender = m.get("sender") or m.get("role") or ""
        role = "user" if sender in ("human", "user") else "assistant"
        text = m.get("text", "")
        if not text:
            content = m.get("content", "")
            if isinstance(content, list):
                text = "\n".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            elif isinstance(content, str):
                text = content
        if text.strip():
            result.append({"role": role, "content": text.strip()})
    return result


def extract_from_next_data(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag or not tag.string:
        return []
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return []

    candidates = [
        _dig(data, "props", "pageProps", "shareData", "conversation", "chat_messages"),
        _dig(data, "props", "pageProps", "conversation", "chat_messages"),
        _dig(data, "props", "pageProps", "shareData", "chat_messages"),
        _dig(data, "props", "pageProps", "messages"),
    ]
    for msgs in candidates:
        if isinstance(msgs, list) and msgs:
            return _parse_message_list(msgs)
    return []


# ─── Strategy 2: inline JSON blob search ─────────────────────────────────────
# Some platforms inject conversation JSON elsewhere in the page.

def extract_from_json_blobs(html: str) -> list[dict]:
    # Find large JSON objects that look like message arrays
    pattern = re.compile(r'\[\s*\{[^{}]*"(?:role|sender)"[^{}]*\}', re.DOTALL)
    for match in pattern.finditer(html):
        try:
            # Try to grab a full JSON array starting at this position
            start = match.start()
            bracket_depth = 0
            end = start
            for i, ch in enumerate(html[start:], start):
                if ch == '[':
                    bracket_depth += 1
                elif ch == ']':
                    bracket_depth -= 1
                    if bracket_depth == 0:
                        end = i + 1
                        break
            blob = json.loads(html[start:end])
            if isinstance(blob, list) and blob:
                result = _parse_message_list(blob)
                if len(result) >= 2:
                    return result
        except Exception:
            continue
    return []


# ─── Strategy 3: DOM selectors via BeautifulSoup ─────────────────────────────

SELECTOR_PAIRS = [
    # Claude.ai
    ("[data-testid='human-turn']",  "[data-testid='ai-turn']"),
    (".human-turn",                  ".ai-turn"),
    # ChatGPT
    ("[data-message-author-role='user']", "[data-message-author-role='assistant']"),
    # Generic
    ("[class*='userMessage']",       "[class*='assistantMessage']"),
    ("[class*='user-message']",      "[class*='model-response']"),
]


def extract_from_dom(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    for user_sel, ai_sel in SELECTOR_PAIRS:
        users = soup.select(user_sel)
        ais   = soup.select(ai_sel)
        if not users and not ais:
            continue
        # Merge and sort by document order
        tagged = (
            [(el, "user")      for el in users] +
            [(el, "assistant") for el in ais]
        )
        # Sort by position in the document
        all_els = list(soup.descendants)
        def order(item):
            try:
                return all_els.index(item[0])
            except ValueError:
                return 9999
        tagged.sort(key=order)
        result = [
            {"role": role, "content": el.get_text("\n", strip=True)}
            for el, role in tagged
            if el.get_text(strip=True)
        ]
        if len(result) >= 2:
            return result
    return []


# ─── Strategy 4: index-based alternation ─────────────────────────────────────

INDEX_SELECTORS = [
    ".font-claude-message, [data-testid='templated-cell']",
]


def extract_index_alternation(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    for sel in [
        {"class": "font-claude-message"},
    ]:
        els = soup.find_all(attrs=sel)
        if len(els) >= 2:
            result = []
            for i, el in enumerate(els):
                text = el.get_text("\n", strip=True)
                if text:
                    result.append({
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": text,
                    })
            if len(result) >= 2:
                return result
    return []


# ─── Strategy 5: text heuristic ──────────────────────────────────────────────

ROLE_MAP = {
    "you": "user", "human": "user", "user": "user",
    "claude": "assistant", "chatgpt": "assistant", "gpt": "assistant",
    "gemini": "assistant", "assistant": "assistant", "ai": "assistant",
    "model": "assistant",
}


def extract_text_heuristic(text: str) -> list[dict]:
    lines = text.splitlines()
    messages, current_role, buf = [], None, []

    def flush():
        nonlocal buf
        content = "\n".join(buf).strip()
        if current_role and content:
            messages.append({"role": current_role, "content": content})
        buf = []

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        matched = None
        for label, role in ROLE_MAP.items():
            if lower.startswith(label + ":") or lower == label:
                rest = stripped[len(label):].lstrip(": ").strip()
                matched = (role, rest)
                break
        if matched:
            flush()
            current_role = matched[0]
            if matched[1]:
                buf.append(matched[1])
        elif current_role is not None:
            buf.append(line)

    flush()
    return messages if len(messages) >= 2 else []


# ─── Main pipeline ────────────────────────────────────────────────────────────

def scrape(url: str) -> list[dict]:
    html = fetch_page(url)  # raises on HTTP error

    return (
        extract_from_next_data(html)
        or extract_from_json_blobs(html)
        or extract_from_dom(html)
        or extract_index_alternation(html)
        or extract_text_heuristic(BeautifulSoup(html, "lxml").get_text("\n"))
        or []
    )


# ─── Formatters ───────────────────────────────────────────────────────────────

def format_txt(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = "USER" if m["role"] == "user" else "ASSISTANT"
        lines.append(f"[{role}]")
        lines.append(m["content"])
        lines.append("")
    return "\n".join(lines).strip()


def format_md(messages: list[dict]) -> str:
    lines = []
    for i, m in enumerate(messages):
        lines.append("### 🧑 User" if m["role"] == "user" else "### 🤖 Assistant")
        lines.append("")
        lines.append(m["content"])
        lines.append("")
        if i < len(messages) - 1:
            lines.append("---")
            lines.append("")
    return "\n".join(lines).strip()


def build_response(messages: list[dict], fmt: str) -> Response:
    if fmt == "json":
        content  = json.dumps(messages, indent=2, ensure_ascii=False)
        mime     = "application/json"
        filename = "chat_export.json"
    elif fmt == "md":
        content  = format_md(messages)
        mime     = "text/markdown"
        filename = "chat_export.md"
    else:
        content  = format_txt(messages)
        mime     = "text/plain"
        filename = "chat_export.txt"

    return Response(
        content=content.encode("utf-8"),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Endpoints ────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    url: str
    format: str = "txt"


class ParseRequest(BaseModel):
    raw_text: str
    format: str = "txt"


@app.post("/export")
def export_chat(req: ExportRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with https://")
    try:
        messages = scrape(url)
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch page: {e}")

    if not messages:
        raise HTTPException(422, (
            "Page loaded but no conversation found. "
            "Try the Manual Paste tab instead."
        ))
    return build_response(messages, req.format)


@app.post("/parse")
def parse_chat(req: ParseRequest):
    messages = extract_text_heuristic(req.raw_text)
    if not messages:
        raise HTTPException(422, "Could not detect conversation structure in the pasted text.")
    return build_response(messages, req.format)


# ─── Serve frontend ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
