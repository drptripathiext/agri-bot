---
title: AgriBot
emoji: 🌾
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# 🌾 AgriBot — Telegram study bot for agriculture exam aspirants

Answers questions from **your own notes, PDFs and books** (ICAR NET / ARS / SRF / JRF,
Agricultural Extension). Runs entirely on free services.

| Piece | What it uses | Cost |
|---|---|---|
| Chat | Telegram Bot API | Free |
| Brain | Google Gemini free tier (Groq / OpenRouter fallback) | Free |
| Search | Built-in BM25 over 14,851 passages — no vector DB, no embeddings bill | Free |
| Hosting | Render (free web service) | Free |

## Setup

Set these as **Space secrets** (Settings → Variables and secrets):

| Secret | Required | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `GEMINI_API_KEY` | ✅ | https://aistudio.google.com/apikey |
| `GROQ_API_KEY` | optional | https://console.groq.com/keys |
| `OPENROUTER_API_KEY` | optional | https://openrouter.ai/keys |
| `BOT_NAME` | optional | display name, default `AgriBot` |
| `ADMIN_ID` | optional | your Telegram numeric id (skips rate limit) |
| `RATE_PER_HOUR` | optional | questions per user per hour, default `25` |
| `ALLOW_OUTSIDE_NOTES` | optional | `1` = answer beyond notes with a warning, `0` = notes only |
| `SHOW_SOURCES` | optional | `0` (default) = never name the source book, `1` = show 📚 line |
| `WEBSITE` | optional | study portal link, default `https://www.agriextprep.co.in` |
| `PROMO_EVERY` | optional | promo (group / website) every N questions, default `5` |
| `DETAIL_TOKENS` | optional | max tokens for descriptive answers, default `3000` |

Tip: `GEMINI_API_KEY` accepts **comma-separated keys** — add a key from a second
Google account to double your free daily quota.

## Commands

```
/ask <question>   answer from the notes
/quiz <topic>     5 MCQ practice questions
/website          syllabus, notes, mock tests, mini quiz, test series
/lang auto|hi|en|hinglish
/sources          list of books/notes loaded
/stats            usage
/help
```

Ask normally for:
* **PYQs** — `previous year questions on ATMA`
* **Mains** — `ARS mains questions on unit 1`, or paste a mains question for a model answer
* **Detailed notes** — add `in detail` / `descriptive` / `short note on` to any question
* **Strategy** — `how to prepare for ASRB NET?`

In a group the bot stays quiet unless you use `/ask`, @mention it, or reply to its message.

## Rebuilding the knowledge base

Full rebuild:

```bash
python build_index.py --src "/path/to/your/notes" --out kb
```

Or add just the new files, without rebuilding everything:

```bash
python add_to_index.py --kb kb --add "new file 1.pdf" "new file 2.docx"
```

Then re-upload the three files in `kb/` to GitHub — Render redeploys on commit.

Needs `poppler-utils` (for `pdftotext`), and `tesseract-ocr` + `python-docx`
only if you have scanned PDFs / Word files.
