"""
DeepSeek Document Parser API — PDF/Word/PPT/images to markdown.

A thin HTTP wrapper over DeepSeek's server-side document stack:
  * POST /files  (purpose=text_recognition | rich_layout_recognition | user_data)
    -> documents parse AT UPLOAD TIME; upload returns only after parsing succeeds
  * POST /chat/completions with a {"type":"file","file_id":...} content block
    -> the extraction step (markdown out)
  * POST /v1/tools/ocr passthrough for page-level text + ref/box coordinates

There is NO parsing stack in this container: no Tesseract, no pymupdf, no
LibreOffice. DeepSeek parses server-side and bills the deployer's own key
(rich-layout parsing $0.225 per 1,000 pages; plain-text purpose free).
Files are WRITE-ONLY upstream: there is no download endpoint anywhere in this
API — callers get extracted content only.

Endpoints:
    GET  /health   liveness probe (no external calls)
    POST /parse    multipart file + mode text|rich|file -> markdown
    POST /ocr      multipart file + purpose -> pages[].text (raw coords inline)

No user-configurable upstream URLs anywhere: api.deepseek.com is a constant.
"""

import asyncio
import mimetypes
import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------- config

DEEPSEEK_BASE = "https://api.deepseek.com"  # constant — never derived from user input
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

MAX_RETRIES = 2
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
UPLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)   # docs: upload <=120 s for PDF/Word/PPT
CHAT_TIMEOUT = httpx.Timeout(300.0, connect=15.0)     # extraction is the slow step

# mode -> (purpose, allowed extensions, upstream MiB limit)
_DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx",
             ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_DATA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
              ".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".yaml", ".yml",
              ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".sh", ".sql",
              ".xlsx", ".xls", ".epub", ".mobi"}
MODES = {
    "text": ("text_recognition", _DOC_EXTS, 100),          # text only, no figures
    "rich": ("rich_layout_recognition", _DOC_EXTS, 100),   # text + figures, layout preserved
    "file": ("user_data", _DATA_EXTS, 32),                 # images / text / spreadsheets / ebooks
}
OCR_PURPOSES = ("text_recognition", "rich_layout_recognition")

EXTRACT_PROMPT = (
    "Extract the complete content of this document as clean markdown. "
    "Preserve the reading order, headings, lists, and tables (render tables as "
    "markdown tables). Do not summarize and do not add commentary — output only "
    "the extracted content."
)

app = FastAPI(
    title="DeepSeek Document Parser API",
    description="PDF/Word/PPT/images to markdown via DeepSeek's server-side parsing. Zero parsing dependencies.",
    version="1.0.0",
)


def _api_key() -> Optional[str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return key or None


def error_response(status: int, message: str, type_: str = "api_error",
                   code: Optional[str] = None, retry_after: Optional[int] = None) -> JSONResponse:
    """Uniform error envelope: {error: {message, type, code}} (+ Retry-After)."""
    headers = {}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": code}},
        headers=headers,
    )


def missing_key_response() -> JSONResponse:
    return error_response(
        401,
        "DEEPSEEK_API_KEY is not set on this deployment. Add it under Variables "
        "(your key from platform.deepseek.com), then redeploy.",
        "invalid_request_error",
        "missing_api_key",
    )


def map_upstream_error(status: int, body: Optional[dict]) -> JSONResponse:
    """Map DeepSeek errors to the documented client envelope."""
    err = body.get("error") if isinstance(body, dict) else None
    upstream_msg = ""
    if isinstance(err, dict):
        upstream_msg = err.get("message") or ""
    elif isinstance(err, str):
        upstream_msg = err
    elif isinstance(body, dict):
        upstream_msg = str(body.get("message") or "")

    if status == 401:
        return error_response(401, upstream_msg or "Upstream rejected the configured DEEPSEEK_API_KEY.",
                              "invalid_request_error", "invalid_api_key")
    if status == 400 and "risk" in upstream_msg.lower():
        return error_response(422, upstream_msg or "The request was blocked by the provider's content safety review.",
                              "content_blocked", "content_exists_risk")
    if status == 400:
        return error_response(400, upstream_msg or "Upstream rejected the request.", "invalid_request_error", "bad_request")
    if status == 429:
        return error_response(429, upstream_msg or "Upstream rate limit reached, retries exhausted.",
                              "rate_limit_error", "rate_limited", retry_after=5)
    if status in RETRYABLE_STATUS:
        return error_response(502, upstream_msg or f"Upstream unavailable (HTTP {status}), retries exhausted.",
                              "api_error", "upstream_unavailable")
    return error_response(502, upstream_msg or f"Upstream returned HTTP {status}.", "api_error", "upstream_error")


async def call_upstream(client: httpx.AsyncClient, key: str, url: str, *,
                        data: Optional[dict] = None, files: Optional[list] = None,
                        json_payload: Optional[dict] = None, timeout: httpx.Timeout):
    """POST to api.deepseek.com with retries on 408/429/5xx + network errors.

    Returns (status, body_dict_or_None, error_message_or_None).
    NOTE: `data` fields render before `files` in the multipart body, which is
    what the Files API requires for documents (purpose BEFORE file)."""
    last_network_error: Optional[str] = None
    last_status: Optional[int] = None
    last_body: Optional[dict] = None
    headers = {"Authorization": f"Bearer {key}"}
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
        try:
            resp = await client.post(url, headers=headers, data=data, files=files,
                                     json=json_payload, timeout=timeout)
        except httpx.RequestError as exc:
            last_network_error = f"Upstream connection failed: {exc.__class__.__name__}"
            continue
        last_status = resp.status_code
        try:
            last_body = resp.json()
        except ValueError:
            last_body = None
        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
            ra = resp.headers.get("retry-after")
            if ra and ra.strip().isdigit():
                await asyncio.sleep(min(int(ra), 30))
            continue
        return last_status, last_body, None
    if last_network_error and last_status is None:
        return None, None, last_network_error
    return last_status, last_body, None


def check_body_error(body: Optional[dict]) -> Optional[JSONResponse]:
    """DeepSeek can return HTTP 200 with an `error` object in the body —
    inspect the body, not just the status code."""
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return error_response(502, msg or "Upstream reported an error in a 200 response.",
                              "api_error", "upstream_body_error")
    return None


def sniff_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename, strict=False)
    return mime or "application/octet-stream"


def gate_file(file: UploadFile, mode: str) -> tuple:
    """Shared request gate. Returns (content_bytes, filename) or an error JSONResponse."""
    purpose, allowed, upstream_mb = MODES[mode]
    filename = (file.filename or "upload").strip() or "upload"
    if len(filename.encode("utf-8")) > 512:
        return error_response(400, "Filename exceeds the 512-byte upstream limit.",
                              "invalid_request_error", "filename_too_long")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed:
        allowed_list = ", ".join(sorted(allowed))
        return error_response(
            400,
            f"Format '{ext or 'unknown'}' is not accepted in mode '{mode}' (purpose {purpose}). "
            f"Allowed: {allowed_list}.",
            "invalid_request_error", "unsupported_format",
        )
    content = file.file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        return error_response(
            413,
            f"Upload exceeds MAX_UPLOAD_MB={MAX_UPLOAD_MB} "
            f"(got {len(content) / 1048576:.1f} MiB). Upstream accepts up to "
            f"{upstream_mb} MiB for this purpose, but this deployment caps uploads "
            f"at {MAX_UPLOAD_MB} MiB (edge proxy + RAM).",
            "invalid_request_error", "payload_too_large",
        )
    if not content:
        return error_response(400, "Empty upload.", "invalid_request_error", "empty_file")
    return content, filename


def file_metadata(body: dict) -> dict:
    meta = {
        "id": body.get("id"),
        "bytes": body.get("bytes"),
        "created_at": body.get("created_at"),
        "filename": body.get("filename"),
        "purpose": body.get("purpose"),
    }
    if body.get("expires_at") is not None:
        meta["expires_at"] = body.get("expires_at")
    return meta


# ---------------------------------------------------------------- routes

@app.get("/")
async def root():
    return {
        "service": "deepseek-parser-api",
        "endpoints": ["POST /parse", "POST /ocr", "GET /health"],
        "modes": {m: {"purpose": p, "upstream_limit_mib": mb} for m, (p, _e, mb) in MODES.items()},
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.get("/health")
async def health():
    # Dotless path, no external calls: safe as a Railway healthcheck probe.
    return {
        "ok": True,
        "service": "deepseek-parser-api",
        "key_configured": bool(_api_key()),
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.post("/parse")
async def parse(file: UploadFile = File(...), mode: str = Form("rich")):
    """Upload a document (parse happens server-side at upload time), then run a
    chat extraction against the returned file_id. Synchronous v1: small docs
    return in seconds; very large docs can take minutes (300 s cap, disclosed)."""
    key = _api_key()
    if not key:
        return missing_key_response()
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return error_response(
            400, f"Unknown mode '{mode}'. Allowed: text, rich, file.",
            "invalid_request_error", "unknown_mode",
        )
    gated = gate_file(file, mode)
    if isinstance(gated, JSONResponse):
        return gated
    content, filename = gated
    purpose = MODES[mode][0]
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        # 1. Upload — purpose field BEFORE file (required for documents).
        status, body, net_err = await call_upstream(
            client, key, f"{DEEPSEEK_BASE}/files",
            data={"purpose": purpose},
            files=[("file", (filename, content, sniff_mime(filename)))],
            timeout=UPLOAD_TIMEOUT,
        )
        if net_err:
            return error_response(502, net_err, "api_error", "upstream_unreachable")
        if body is None:
            return error_response(502, "Upstream returned a non-JSON response.", "api_error", "upstream_bad_response")
        if status != 200:
            return map_upstream_error(status, body)
        body_err = check_body_error(body)
        if body_err:
            return body_err
        file_id = body.get("id")
        if not file_id:
            return error_response(502, "Upload succeeded but no file_id was returned.",
                                  "api_error", "upstream_bad_response")
        meta = file_metadata(body)

        # 2. Extract — chat with the file block. (Not stored purposes like
        #    spreadsheets/text are parsed here; recognition purposes were
        #    already parsed at upload.)
        payload = {
            "model": MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACT_PROMPT},
                    {"type": "file", "file_id": file_id},
                ],
            }],
            "stream": False,
        }
        status, body, net_err = await call_upstream(
            client, key, f"{DEEPSEEK_BASE}/chat/completions",
            json_payload=payload, timeout=CHAT_TIMEOUT,
        )

    if net_err:
        return error_response(502, net_err, "api_error", "upstream_unreachable")
    if body is None:
        return error_response(502, "Upstream returned a non-JSON response.", "api_error", "upstream_bad_response")
    if status != 200:
        return map_upstream_error(status, body)
    body_err = check_body_error(body)
    if body_err:
        return body_err

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    markdown = message.get("content") or ""
    finish_reason = choice.get("finish_reason")
    if finish_reason == "content_filter":
        return JSONResponse(status_code=422, content={
            "refusal": True,
            "explanation": message.get("refusal") or "The request was declined by the provider's safety review.",
            "markdown": "",
        })
    if finish_reason == "length":
        markdown += "\n\n> [output truncated at the model's length limit]"

    return {
        "filename": filename,
        "mode": mode,
        "file": meta,
        "markdown": markdown,
        "pages": None,  # upstream does not report page count on /parse; use /ocr for page_count
        "finish_reason": finish_reason,
        "usage": body.get("usage") or {},
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


@app.post("/ocr")
async def ocr(file: UploadFile = File(...), purpose: str = Form("rich_layout_recognition")):
    """Passthrough to POST /v1/tools/ocr. The file is NOT stored in the Files
    API. Returns pages[].text (raw model output — ref/box coordinates ride
    inline as <｜ref｜>/<｜box｜> markers, normalized 0-1000) plus page_count
    and usage. Upstream: 15 concurrent OCR requests per account."""
    key = _api_key()
    if not key:
        return missing_key_response()
    purpose = (purpose or "").strip()
    if purpose not in OCR_PURPOSES:
        return error_response(
            400, f"Unknown purpose '{purpose}'. Allowed: {', '.join(OCR_PURPOSES)}.",
            "invalid_request_error", "unknown_purpose",
        )
    gated = gate_file(file, "rich")
    if isinstance(gated, JSONResponse):
        return gated
    content, filename = gated
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        status, body, net_err = await call_upstream(
            client, key, f"{DEEPSEEK_BASE}/v1/tools/ocr",
            data={"purpose": purpose},
            files=[("file", (filename, content, sniff_mime(filename)))],
            timeout=UPLOAD_TIMEOUT,
        )

    if net_err:
        return error_response(502, net_err, "api_error", "upstream_unreachable")
    if body is None:
        return error_response(502, "Upstream returned a non-JSON response.", "api_error", "upstream_bad_response")
    if status != 200:
        return map_upstream_error(status, body)
    body_err = check_body_error(body)
    if body_err:
        return body_err
    body.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    return body
