# ChatDump — AI Chat Exporter

Paste any shared AI chat link → get a clean `.txt`, `.md`, or `.json` file.

Works with **Claude.ai**, **ChatGPT**, **Gemini**, **Perplexity**, and more.

---

## How it Works

Uses a real headless Chromium browser (via Playwright) to open the link exactly like a human would, then extracts the conversation using three layered strategies:
1. **Next.js `__NEXT_DATA__`** — reads raw JSON embedded in the page (most reliable for Claude.ai)
2. **DOM selectors** — targets known HTML elements across platforms
3. **Text heuristic** — falls back to role-label parsing from visible page text

---

## Quick Start (Local)

```bash
# 1. Clone or unzip the project
cd chat-exporter

# 2. Create a virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# 4. Run the server
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** in your browser.

---

## Deploy with Docker

```bash
docker build -t chatdump .
docker run -p 8000:8000 chatdump
```

---

## Deploy to Railway / Render (free tier)

### Railway
1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Railway auto-detects the Dockerfile — click Deploy
4. Done. You get a public URL.

### Render
1. Push to GitHub
2. Go to [render.com](https://render.com) → New → Web Service → connect repo
3. Select **Docker** as runtime
4. Click Deploy
5. Done.

---

## API

### `POST /export`

**Body:**
```json
{
  "url": "https://claude.ai/share/...",
  "format": "txt"
}
```

**Formats:** `txt`, `md`, `json`

**Response:** File download with `Content-Disposition: attachment`

---

## Output Formats

### `.txt` (best for pasting into other LLMs)
```
[USER]
Hello, what is quantum entanglement?

[ASSISTANT]
Quantum entanglement is a phenomenon where...
```

### `.md` (readable in any markdown viewer)
```markdown
### 🧑 User
Hello, what is quantum entanglement?

---

### 🤖 Assistant
Quantum entanglement is a phenomenon where...
```

### `.json` (for programmatic use)
```json
[
  {"role": "user", "content": "Hello, what is quantum entanglement?"},
  {"role": "assistant", "content": "Quantum entanglement is..."}
]
```

---

## Notes

- Claude.ai share pages load with JavaScript. The headless browser waits for full render before extracting.
- No data is stored. Files are generated on the fly and streamed back directly.
- Each export spins up a fresh browser context and closes it immediately after.
