import asyncio
import json
import re
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

app = FastAPI()

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' });
window.chrome = { runtime: {} };
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class ScrapeRequest(BaseModel):
    url: str
    format: str = "txt"  # txt, md, json


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


DOM_SELECTOR_PAIRS = [
    ('[data-testid="human-turn"]',       '[data-testid="ai-turn"]'),
    ('.human-turn',                       '.ai-turn'),
    ('[class*="human-turn"]',             '[class*="ai-turn"]'),
    ('[data-message-author-role="user"]', '[data-message-author-role="assistant"]'),
    ('[class*="user-message"]',           '[class*="model-response"]'),
    ('[class*="userMessage"]',            '[class*="assistantMessage"]'),
    ('.font-user-message',                '.font-claude-message'),
]


async def _try_dom_selectors(page) -> list[dict]:
    for user_sel, asst_sel in DOM_SELECTOR_PAIRS:
        try:
            result = await page.evaluate(
                f"""
                () => {{
                    const els = [...document.querySelectorAll('{user_sel},{asst_sel}')];
                    if (!els.length) return null;
                    return els.map(el => ({{
                        role: el.matches('{user_sel}') ? 'user' : 'assistant',
                        content: el.innerText.trim()
                    }})).filter(m => m.content.length > 0);
                }}
                """
            )
            if result and len(result) >= 2:
                return result
        except Exception:
            pass
    return []


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


async def scrape(url: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            pass

        msgs = (
            await _try_next_data(page)
            or await _try_dom_selectors(page)
            or await _try_text_heuristic(page)
        )

        await browser.close()
        return msgs or []


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
        if m["role"] == "user":
            lines.append("### 🧑 User")
        else:
            lines.append("### 🤖 Assistant")
        lines.append("")
        lines.append(m["content"])
        lines.append("")
        if i < len(messages) - 1:
            lines.append("---")
            lines.append("")
    return "\n".join(lines).strip()


@app.post("/export")
async def export_chat(req: ScrapeRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    messages = await scrape(url)

    if not messages:
        raise HTTPException(
            status_code=422,
            detail="Could not extract conversation. The page may require login or uses an unsupported layout."
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


# Serve frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")
