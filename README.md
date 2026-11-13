# DeepSeek Document Parser — PDF/Word to Markdown API

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/deepseek-parser)

Turn PDF, Word, PowerPoint, spreadsheet, ebook, and image uploads into clean markdown with **one HTTP call and zero parsing dependencies**. Parsing runs on DeepSeek's servers through the Files API (`text_recognition` / `rich_layout_recognition` / `user_data` purposes) plus the standalone OCR tool — this container ships **no Tesseract, no pymupdf, no LibreOffice, no model** to install or maintain. One service, one required variable (`DEEPSEEK_API_KEY`, your own key), no volume.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/parse` | Upload a file (`multipart/form-data`, field `file`) + `mode` form field -> `{markdown, file, usage, elapsed_ms}` |
| `POST` | `/ocr` | Passthrough to DeepSeek's OCR tool -> `{page_count, pages[].text, usage}` with raw ref/box coordinate markers |
| `GET` | `/health` | Liveness probe (no external calls) |

### Modes for `/parse`

| `mode` | Upstream `purpose` | Accepts | Best for |
|---|---|---|---|
| `rich` (default) | `rich_layout_recognition` | PDF, DOC/DOCX, PPT/PPTX, images | Layout-heavy docs — text **and** figures parsed together, placement markers preserved |
| `text` | `text_recognition` | PDF, DOC/DOCX, PPT/PPTX, images | Plain text extraction (figures not sent to the model; free purpose) |
| `file` | `user_data` | XLSX/XLS, TXT/MD/CSV/JSON/code, EPUB/MOBI, images | Spreadsheets and ebooks rendered as markdown tables/text |

### Examples

```bash
# Rich-layout parse of a PDF (default mode) -> markdown with figure placement
curl -F file=@report.pdf https://YOUR-DOMAIN.up.railway.app/parse

# Plain-text mode (free purpose) of the same PDF
curl -F file=@report.pdf -F mode=text https://YOUR-DOMAIN.up.railway.app/parse

# Spreadsheet -> markdown table
curl -F file=@sheet.xlsx -F mode=file https://YOUR-DOMAIN.up.railway.app/parse

# Page-level OCR of a scanned PDF or image (coords ride inline in the text)
curl -F file=@scan.png https://YOUR-DOMAIN.up.railway.app/ocr
```

### Response

```json
{
  "filename": "report.pdf",
  "mode": "rich",
  "file": {"id": "file-api-…", "bytes": 482133, "purpose": "rich_layout_recognition", "created_at": 1763000000},
  "markdown": "# Report\n\n## Revenue\n\n| Quarter | Value |\n|---|---|\n| Q1 | 1.2M |",
  "pages": null,
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 1536, "completion_tokens": 412, "total_tokens": 1948},
  "elapsed_ms": 8213
}
```

`/ocr` returns the raw upstream shape instead: `id`, `object: "ocr.result"`, `page_count`, `pages[].{index, text, finish_reason}` (coordinates appear inside `text` as `<｜ref｜>`/`<｜box｜>` markers normalized to 0-1000), and `usage`.

## Limits (enforced + disclosed)

| Limit | Value |
|---|---|
| Upload cap on this deployment | `MAX_UPLOAD_MB` (default 25 MiB) -> `413` with the cap named |
| Upstream document size | 100 MiB / 1,000 pages (PDF/Word/PPT) — larger files need self-hosting elsewhere |
| Upstream image size | 64 MiB |
| Upstream spreadsheet/text/ebook size | 32 MiB |
| Combined text per chat request | 8 MiB, max 20 documents |
| Filename | 512 bytes max |
| Upload time | 120 s for documents, 10 min for other purposes (parsing happens **at upload time** — the upload returns only after parsing succeeds) |
| Extraction timeout | 300 s per call (synchronous v1 — very large docs can take minutes; keep documents reasonably sized) |
| OCR concurrency | 15 concurrent requests per account |
| Account storage | 25 GiB / 10,000 files per DeepSeek account |

## Files are write-only

Upstream has **no download endpoint**: once parsed, a file cannot be fetched back. This API returns extracted content only — **keep your original files**; nothing here (or upstream) serves them again.

## Cost — billed to your own DeepSeek account

| Item | Price |
|---|---|
| Rich-layout parsing (`rich` mode, `rich_layout_recognition`) | $0.225 per 1,000 pages |
| Plain-text parsing (`text` mode, `text_recognition`) | free purpose; token usage only |
| OCR tool | usage reported per call (`usage` is reference-only for OCR) |
| Chat extraction tokens (deepseek-flash) | $0.30 / 1M input, $1.20 / 1M output at peak (01:00-04:00 + 06:00-10:00 UTC Mon-Fri); half off-peak; cache hits ~1/50th |

Every response carries `usage` (tokens per call) so spend is visible. A 2-page rich parse typically costs well under $0.001. Measured on Railway (small 2-page fixtures): `/parse` text 3.2 s, `/parse` rich 4.0 s, `/ocr` 2.3-2.8 s, XLSX via `mode=file` 3.2 s (in-app time; add ~1-4 s request overhead). Upload time adds ~30 s for a 26 MiB file through the edge proxy.

## The one variable

The deploy form asks for `DEEPSEEK_API_KEY` — your key from [platform.deepseek.com](https://platform.deepseek.com). All parsing and token usage bills to your account. The service **boots healthy without it** (healthcheck passes) and any parse/OCR call returns a clear 401-shaped error until it is set. Optional: `MAX_UPLOAD_MB` (default 25), `DEEPSEEK_MODEL` (default `deepseek-flash`).

## Error handling

A uniform `{error: {message, type, code}}` envelope covers: missing key (401 `missing_api_key`), upstream-rejected key (401 `invalid_api_key`), upload over the cap (413 `payload_too_large`), format not accepted for the chosen mode (400 `unsupported_format` with the allowed list), provider content-safety blocks (422 `content_exists_risk`), model refusals (422 with an explanation), rate limits (429 + `Retry-After`), and upstream failures after automatic retries (502). Transient upstream 408/429/5xx errors and network errors are retried with backoff before surfacing. DeepSeek can also return HTTP 200 with a body-level `error` — that is detected and surfaced as 502 `upstream_body_error`.

## License

MIT
