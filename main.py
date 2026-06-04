import asyncio
import json
import re
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth

app = FastAPI()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─── Claude.ai specific selectors (as of 2025) ───────────────────────────────
# Share pages render with these classes/testids
CLAUDE_USER_SELECTORS = [
    "div[data-testid='human-turn']",
    ".human-turn",
    "[class*='human-turn']",
    "div[data-testid='templated-cell']",  # older format
]

CLAUDE_AI_SELECTORS = [
    "div[data-testid='ai-turn']",
    ".ai-turn",
    "[class*='ai-turn']",
    ".font-claude-message",              # suggested by community
]

# Combined wait selector — if any of these exist, page has rendered
WAIT_SELECTOR = (
    "div[data-testid='human-turn'], "
    "div[data-testid='ai-turn'], "
    ".font-claude-message, "
    ".human-turn, "
    ".ai-turn, "
    "div[data-testid='templated-cell']"
)


class ScrapeRequest(BaseModel):
    url: str
    format: str = "txt"


# ─── Strategy 1: __NEXT_DATA__ JSON blob ─────────────────────────────────────

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


async def _try_next_data(page) -> list[dict]:
    try:
        raw = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent"
        )
        if not raw:
            return []
        data = json.loads(raw)
        candidates = [
            _dig(data, "props", "pageProps", "shareData", "conversation", "chat_messages"),
            _dig(data, "props", "pageProps", "conversation", "chat_messages"),
            _dig(data, "props", "pageProps", "shareData", "chat_messages"),
            _dig(data, "props", "pageProps", "messages"),
        ]
        for msgs in candidates:
            if isinstance(msgs, list) and msgs:
                return _parse_message_list(msgs)
    except Exception:
        pass
    return []


# ─── Strategy 2: Claude.ai DOM — paired user + ai selectors ──────────────────

async def _try_claude_dom(page) -> list[dict]:
    """
    Try each (user_sel, ai_sel) pair.  For each pair, grab all matching
    elements, tag them with their role, sort by DOM position, return.
    """
    pairs = list(zip(CLAUDE_USER_SELECTORS, CLAUDE_AI_SELECTORS))
    # Also try combined with index-based alternation (fallback within this strategy)
    for user_sel, ai_sel in pairs:
        try:
            result = await page.evaluate(
                f"""
                () => {{
                    const users = [...document.querySelectorAll('{user_sel}')];
                    const ais   = [...document.querySelectorAll('{ai_sel}')];
                    if (!users.length && !ais.length) return null;

                    // Tag each with role + its DOM order
                    const all = [
                        ...users.map(el => ({{ role: 'user',      el }})),
                        ...ais.map(el   => ({{ role: 'assistant', el }}))
                    ];
                    // Sort by DOM position
                    all.sort((a, b) => {{
                        const pos = a.el.compareDocumentPosition(b.el);
                        return pos & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
                    }});
                    return all
                        .map(x => ({{ role: x.role, content: x.el.innerText.trim() }}))
                        .filter(m => m.content.length > 0);
                }}
                """
            )
            if result and len(result) >= 2:
                return result
        except Exception:
            continue
    return []


# ─── Strategy 3: Index-based alternation (any combined selector) ─────────────

async def _try_index_alternation(page) -> list[dict]:
    """
    Query all message-like elements in DOM order, alternate user/assistant.
    Works when user+ai elements can't be distinguished by selector alone.
    """
    combined_selectors = [
        ".font-claude-message, div[data-testid='templated-cell']",
        "[data-testid='human-turn'], [data-testid='ai-turn']",
        ".human-turn, .ai-turn",
        "[class*='human-turn'], [class*='ai-turn']",
    ]
    for sel in combined_selectors:
        try:
            result = await page.evaluate(
                f"""
                () => {{
                    const els = [...document.querySelectorAll('{sel}')];
                    if (els.length < 2) return null;
                    return els.map((el, i) => ({{
                        role: i % 2 === 0 ? 'user' : 'assistant',
                        content: el.innerText.trim()
                    }})).filter(m => m.content.length > 0);
                }}
                """
            )
            if result and len(result) >= 2:
                return result
        except Exception:
            continue
    return []


# ─── Strategy 4: Text heuristic ──────────────────────────────────────────────

ROLE_MAP = {
    "you": "user", "human": "user", "user": "user",
    "claude": "assistant", "chatgpt": "assistant", "gpt": "assistant",
    "gemini": "assistant", "assistant": "assistant", "ai": "assistant",
    "model": "assistant",
}


async def _try_text_heuristic(page) -> list[dict]:
    try:
        body_text = await page.evaluate("() => document.body.innerText")
        if not body_text:
            return []
        lines = body_text.splitlines()
        messages, current_role, buf = [], None, []

        def flush():
            nonlocal buf
            text = "\n".join(buf).strip()
            if current_role and text:
                messages.append({"role": current_role, "content": text})
            buf = []

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()
            matched = None
            for label, role in ROLE_MAP.items():
                if lower.startswith(label + ":") or lower.startswith(label + " said:"):
                    matched = (role, stripped[len(label):].lstrip(": ").strip())
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
    except Exception:
        pass
    return []


# ─── Main scraper ─────────────────────────────────────────────────────────────

async def scrape(url: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-infobars",
                "--window-size=1280,900",
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        page = await ctx.new_page()

        # Apply stealth patches to bypass bot detection / Cloudflare
        await stealth(page)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for actual message content to appear (up to 15s)
            try:
                await page.wait_for_selector(WAIT_SELECTOR, timeout=15_000)
            except PlaywrightTimeout:
                pass  # May still have content via __NEXT_DATA__

            # Extra breathing room for React hydration
            await page.wait_for_timeout(2000)

        except PlaywrightTimeout:
            pass

        # Run strategies in order of reliability
        msgs = (
            await _try_next_data(page)
            or await _try_claude_dom(page)
            or await _try_index_alternation(page)
            or await _try_text_heuristic(page)
        )

        await browser.close()
        return msgs or []


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


# ─── API endpoint ─────────────────────────────────────────────────────────────

@app.post("/export")
async def export_chat(req: ScrapeRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL — must start with https://")

    messages = await scrape(url)

    if not messages:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not extract conversation. The page may be behind a login, "
                "protected by Cloudflare, or uses an unsupported layout. "
                "Try the manual paste option."
            )
        )

    if req.format == "json":
        content = json.dumps(messages, indent=2, ensure_ascii=False)
        media_type = "application/json"
        filename = "chat_export.json"
    elif req.format == "md":
        content = format_md(messages)
        media_type = "text/markdown"
        filename = "chat_export.md"
    else:
        content = format_txt(messages)
        media_type = "text/plain"
        filename = "chat_export.txt"

    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ─── Serve frontend ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")


# ─── Manual paste endpoint ────────────────────────────────────────────────────

class ParseRequest(BaseModel):
    raw_text: str
    format: str = "txt"


def parse_raw_text(text: str) -> list[dict]:
    """Parse raw copied page text using role-label heuristic."""
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
    return messages


@app.post("/parse")
async def parse_chat(req: ParseRequest):
    messages = parse_raw_text(req.raw_text)

    if not messages:
        raise HTTPException(
            status_code=422,
            detail="Could not detect conversation structure in the pasted text."
        )

    if req.format == "json":
        content = json.dumps(messages, indent=2, ensure_ascii=False)
        media_type = "application/json"
        filename = "chat_export.json"
    elif req.format == "md":
        content = format_md(messages)
        media_type = "text/markdown"
        filename = "chat_export.md"
    else:
        content = format_txt(messages)
        media_type = "text/plain"
        filename = "chat_export.txt"

    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
