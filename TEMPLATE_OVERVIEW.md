[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/deepseek-parser)

DeepSeek Document Parser — upload a PDF, Word, PowerPoint, spreadsheet, ebook, or image and get clean markdown back with one HTTP call. Parsing runs entirely on DeepSeek's servers (Files API recognition purposes + the standalone OCR tool), so the container ships zero parsing dependencies: no Tesseract, no pymupdf, no LibreOffice, no model weights. One service, no volume, one deploy-time variable: your DeepSeek API key. Files are write-only upstream — the API returns extracted content only, never the original file.

**Endpoints.** `POST /parse` takes a multipart upload plus a `mode` form field: `rich` (default; layout-heavy PDFs/Word/PPT with figures preserved), `text` (plain text only, free purpose), or `file` (XLSX/CSV/EPUB/text rendered as markdown tables and text). `POST /ocr` passes through to DeepSeek's OCR tool for page-level text with inline ref/box coordinate markers and a per-page `page_count`. `GET /health` is the healthcheck probe (no external calls).

**Cost — billed to your own DeepSeek account.** Rich-layout parsing is $0.225 per 1,000 pages; the plain-text purpose is free; deepseek-flash extraction tokens are $0.30 per 1M input / $1.20 per 1M output at peak (01:00-04:00 and 06:00-10:00 UTC Mon-Fri), half off-peak. A typical 2-page parse costs well under $0.001, and every response carries `usage` so spend is visible per call.

**The one variable.** The deploy form asks for `DEEPSEEK_API_KEY` — your key from platform.deepseek.com. The service boots healthy without it (healthcheck passes) and any parse or OCR call returns a clear 401-shaped error until it is set. Optional `MAX_UPLOAD_MB` (default 25) caps uploads below the upstream limits (100 MiB documents, 64 MiB images, 32 MiB spreadsheets/ebooks).

**How this differs from self-hosted parsers.** Marketplace alternatives like MinerU (50% deploy health) and Docling OCR (43 deploys) bundle a full local parsing stack — model weights, GPU-friendly runtimes, and multi-GB images that need babysitting when document formats change. This template delegates parsing to DeepSeek's servers: the image stays tiny, there is nothing to retrain or upgrade, and parsing quality improves upstream without a redeploy. You trade the upstream per-page fee for zero maintenance, and files remain write-only (extracted text out, originals stay with you).

**Error handling.** A uniform `{error: {message, type, code}}` envelope covers a missing key (401), an upstream-rejected key (401), uploads over the deployment cap (413), formats not accepted for the chosen mode (400 with the allowed list), provider content-safety blocks (422), model refusals (422), rate limits (429 with `Retry-After`), and upstream failures after automatic retries (502). HTTP 200 responses that carry a body-level `error` are detected and surfaced as 502 instead of silently returning empty content.

# Deploy and Host

## About Hosting

You are hosting a single small FastAPI service (uvicorn on python:3.12-slim, pinned dependencies — fastapi, uvicorn, httpx, python-multipart — runs as a non-root user) that makes two upstream calls per parse: a multipart upload to api.deepseek.com/files (parsing happens at upload time; the upload returns only after parsing succeeds) and one Chat Completions extraction against the returned file_id. It is stateless: no database, no volume, nothing to back up — restarts and redeploys are always safe. The container idles at well under 100 MB RAM. Configuration: required `DEEPSEEK_API_KEY` (your key from platform.deepseek.com — rich parsing bills $0.225 per 1,000 pages to your account), optional `MAX_UPLOAD_MB` (default 25) and `DEEPSEEK_MODEL` (default deepseek-flash). Extraction is synchronous with a 300-second cap — keep documents reasonably sized for API use.

## Why Deploy

- Zero parsing dependencies: no Tesseract, no pymupdf, no LibreOffice, no model weights in the image — parsing quality is DeepSeek's problem, not your ops backlog.
- One service, one variable, no volume: deploy in under a minute and bill usage to your own DeepSeek key with `usage` on every response.
- Three purposeful modes: rich (figures preserved in layout), text (free purpose), and file (spreadsheets and ebooks to markdown tables) — the right cost/quality tradeoff per call.
- Page-level OCR included: `/ocr` returns per-page text with raw coordinate markers and `page_count` for scanned documents and images.
- Production-shaped errors: uniform envelope, 413/400 limits enforcement with the allowed-format list, 429s with `Retry-After`, automatic retries with backoff, and safety outcomes surfaced as clean 422s.

## Common Use Cases

- PDF-to-markdown pipelines for RAG: point your ingestion job at `/parse` and feed the markdown straight into embeddings or a knowledge base.
- Spreadsheet and ebook extraction: XLSX/CSV/EPUB uploads render as markdown tables and text for LLM consumption without office suites in your stack.
- Scanned-document OCR: send scans or photos to `/ocr` for per-page text with coordinate markers for downstream layout-aware processing.
- Batch document conversion: automate archives of reports and decks into a markdown corpus, with token/usage accounting per call.
- API-first document intelligence for agents: give tool-using agents a single parse endpoint instead of shipping them a parsing library each.

## Dependencies for

### Deployment Dependencies

- A DeepSeek API key from platform.deepseek.com — the one required deploy-form variable (`DEEPSEEK_API_KEY`). All parsing and token usage bills to your DeepSeek account: rich-layout parsing is $0.225 per 1,000 pages, the plain-text purpose is free, deepseek-flash tokens are $0.30/1M input and $1.20/1M output at peak (01:00-04:00, 06:00-10:00 UTC Mon-Fri), half off-peak.
- Outbound internet access from the service to api.deepseek.com (default on Railway; no inbound ports beyond the auto-provisioned HTTPS domain).
- Nothing else: no database, no volume, no parsing binaries, no GPU. The image pins fastapi, uvicorn, httpx, and python-multipart; python:3.12-slim is the base. Files are write-only upstream — extracted content is returned, never the original file.
