"""
Shared document extraction core for trade documents.
Used by every document extractor (invoice, packing, BL, GD, GD View, Item Details,
Final GD, FI, insurance, LC, contract).

Tiered pipeline: Gemini Flash vision (cheap/fast) -> Claude Sonnet vision (fallback,
opt-in via EXTRACTION_CLAUDE_FALLBACK). A free rule-based text-parser tier is planned
(see EXTRACTION_TEXT_PARSER_ENABLED) but not implemented yet — extract_document() no-ops
that tier if enabled without the parser module present, rather than failing the request.

Handles:
  - Image (JPG/PNG/WEBP) and multi-page PDF input (PDF -> PNG pages via PyMuPDF)
  - Sending all pages to Claude/Gemini in one request
  - Robust JSON parsing: markdown fences, prose around the JSON, literal newlines
    inside strings, and responses TRUNCATED by the output-token cap (the usual cause
    of "Unterminated string ..." on big line-item tables like a combined CI+PL).
"""

import base64
import json
import logging
import os
import re
from pathlib import Path

import anthropic
import httpx

logger = logging.getLogger("uvicorn")

DEFAULT_MODEL = "claude-sonnet-4-6"
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"
# Line-item tables (a combined Commercial Invoice + Packing List can carry 60+ coil rows)
# blow well past 4k output tokens; the response then stops mid-string and the JSON is
# unparseable. Give the model real headroom, and retry with more if it still runs out.
DEFAULT_MAX_TOKENS = 16384
MAX_TOKENS_CEILING = 32768

# Anthropic rejects any single image larger than 10 MB. Keep a margin so high-DPI
# scans (a 2x PNG of an A4 scan can be ~20 MB) never trip the limit.
MAX_IMAGE_BYTES = 4_500_000

# Shown to the user when extraction fails. The document is still saved — the upload
# flow falls back to manual entry, so this must read as a soft outcome, not a crash.
EXTRACTION_FAILED_MESSAGE = (
    "Document uploaded, but automatic extraction failed. "
    "Please review or enter the details manually."
)


class ExtractionError(Exception):
    """Extraction could not produce usable data.

    `user_message` is safe to show in the UI; `detail` carries the raw parser/API error
    and is for server logs only — never put it in an HTTP response body.
    """

    def __init__(self, user_message: str = EXTRACTION_FAILED_MESSAGE, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail or user_message


def safe_extract(extractor, *args, doc_label: str = "", **kwargs) -> tuple[dict, str | None]:
    """Run an extractor without ever raising. Returns (data, failure_message).

    failure_message is None on success, otherwise a clean, user-safe sentence. The raw
    cause is logged server-side. Upload endpoints use this so a bad extraction degrades to
    manual entry instead of failing the upload and losing the document.
    """
    try:
        return extractor(*args, **kwargs), None
    except ExtractionError as e:
        logger.error(f"Extraction failed ({doc_label}): {e.detail}")
        return {}, e.user_message
    except Exception as e:
        logger.error(f"Extraction failed ({doc_label}): {type(e).__name__}: {e}", exc_info=True)
        return {}, EXTRACTION_FAILED_MESSAGE


def _render_page(page, zoom: float, fmt: str = "png") -> bytes:
    import fitz  # pymupdf
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes(fmt)


def _pdf_to_page_images(file_path: str, max_pages: int | None = None) -> list[tuple[bytes, str]]:
    """Convert each PDF page to an image (2x scale for OCR accuracy), returning
    (bytes, media_type) per page. Pages that exceed MAX_IMAGE_BYTES at 2x are
    progressively downscaled; if a lossless PNG is still too large (photographic
    scans), they fall back to JPEG which compresses far better.
    max_pages caps how many pages are sent (e.g. a SWIFT LC can be 100+ pages of
    amendments/repeats — the caller uploads only the first detail pages)."""
    import fitz  # pymupdf

    doc = fitz.open(file_path)
    images = []
    for i, page in enumerate(doc):
        if max_pages and i >= max_pages:
            break
        zoom = 2.0
        data = _render_page(page, zoom, "png")
        media = "image/png"
        while len(data) > MAX_IMAGE_BYTES and zoom > 0.75:
            zoom *= 0.75
            data = _render_page(page, zoom, "png")
        if len(data) > MAX_IMAGE_BYTES:
            zoom = 2.0
            data = _render_page(page, zoom, "jpeg")
            media = "image/jpeg"
            while len(data) > MAX_IMAGE_BYTES and zoom > 0.4:
                zoom *= 0.75
                data = _render_page(page, zoom, "jpeg")
        images.append((data, media))
        logger.info(f"  page {i + 1}: {media}, zoom={zoom:.2f}, {len(data) // 1024} KB")
    total = doc.page_count
    doc.close()
    logger.info(f"PDF converted: {len(images)} of {total} page(s) from {Path(file_path).name}")
    return images


def build_content_blocks(file_path: str, max_pages: int | None = None) -> list[dict]:
    """Build Claude message content blocks from an image or PDF file."""
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        pages = _pdf_to_page_images(file_path, max_pages=max_pages)
        blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.standard_b64encode(img).decode("utf-8"),
                },
            }
            for img, media in pages
        ]
        if len(pages) > 1:
            blocks.append({
                "type": "text",
                "text": f"The above {len(pages)} images are consecutive pages of the SAME document. "
                        f"Combine information across all pages.",
            })
        return blocks

    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    with open(file_path, "rb") as f:
        raw = f.read()
    # Oversized raster uploads hit the same 10 MB/image limit — downscale to JPEG.
    if len(raw) > MAX_IMAGE_BYTES:
        import fitz  # pymupdf
        doc = fitz.open(file_path)
        page = doc[0]
        zoom = 1.0
        raw = _render_page(page, zoom, "jpeg")
        media_type = "image/jpeg"
        while len(raw) > MAX_IMAGE_BYTES and zoom > 0.2:
            zoom *= 0.75
            raw = _render_page(page, zoom, "jpeg")
        doc.close()
    data = base64.standard_b64encode(raw).decode("utf-8")
    return [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}]


# ── JSON response parsing ────────────────────────────────────────────────────
# Models occasionally wrap JSON in fences or prose, and a response cut off by the
# output-token cap is structurally incomplete. Each helper below peels one of those
# layers; parse_json_response() applies them in order of increasing desperation.

def _strip_fences(text: str) -> str:
    """Drop ```json ... ``` fences, including a fence left unterminated by truncation."""
    t = text.strip()
    if "```" not in t:
        return t
    fenced = re.search(r"```(?:json|JSON)?\s*(.*?)```", t, re.S)
    if fenced:
        return fenced.group(1).strip()
    # Opening fence with no closing one — the response was cut off inside the block.
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
    return re.sub(r"```\s*$", "", t).strip()


def _slice_to_json(text: str) -> str:
    """Trim any commentary before the JSON body ('Here is the data: {...}')."""
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    return text[start:] if start is not None else text


def _open_brackets(text: str) -> list[str]:
    """Closers still owed at the end of `text`, tracking JSON string/escape state."""
    stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack:
            stack.pop()
    return stack


def _repair_truncated(text: str) -> str | None:
    """Salvage a response cut off mid-value (the max_tokens case).

    Rewinds to the last point where the JSON was structurally complete — the end of the
    last finished nested object/array, or the last comma between finished values — then
    closes the still-open brackets. For a truncated invoice this keeps every complete
    line item and drops only the half-written one.
    """
    safe, stack, in_str, esc = 0, [], False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if stack:                 # closed a nested container; still inside the root
                safe = i + 1
        elif ch == "," and stack:
            safe = i                  # cut BEFORE the comma: everything up to here is whole

    if safe <= 0:
        return None
    head = text[:safe].rstrip().rstrip(",")
    closers = _open_brackets(head)
    if not closers:
        return None
    return head + "".join(reversed(closers))


def parse_json_response(raw: str) -> tuple[dict, bool]:
    """Parse a model response into a dict. Returns (data, partial).

    `partial` is True when the response had to be repaired (truncated mid-way), so the
    caller can tell the user some rows may be missing. Raises ExtractionError if no
    usable JSON can be recovered at all.
    """
    text = _slice_to_json(_strip_fences(raw))

    # strict=False tolerates literal newlines/tabs inside strings — common when a model
    # copies a multi-line address or goods description verbatim.
    for attempt in (lambda: json.loads(text), lambda: json.loads(text, strict=False)):
        try:
            return _as_dict(attempt()), False
        except (json.JSONDecodeError, ValueError):
            pass

    repaired = _repair_truncated(text)
    if repaired:
        try:
            data = _as_dict(json.loads(repaired, strict=False))
            logger.warning(
                "Extraction response was truncated/malformed and has been repaired; "
                "some trailing rows may be missing."
            )
            return data, True
        except (json.JSONDecodeError, ValueError):
            pass

    raise ExtractionError(detail=f"Could not parse extraction response. Raw response: {raw[:2000]}")


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    # Some responses come back as a single-element list wrapping the object.
    if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
        return obj[0]
    raise ValueError(f"Expected a JSON object, got {type(obj).__name__}")


def extract_with_claude(file_path: str, prompt: str, api_key: str,
                        model: str = DEFAULT_MODEL,
                        max_tokens: int = DEFAULT_MAX_TOKENS,
                        max_pages: int | None = None) -> dict:
    """
    Send a document (image/PDF) + extraction prompt to Claude, return the parsed dict.
    max_pages caps PDF pages sent (None = all).

    The returned dict carries `_extraction_partial: True` when the response had to be
    repaired after truncation. Raises ExtractionError (user-safe message, raw detail for
    logs) on any failure; callers should save the document anyway and fall back to
    manual entry.
    """
    if not api_key:
        raise ExtractionError("AI extraction is not configured on this server.",
                              detail="ANTHROPIC_API_KEY is not set.")
    if not os.path.exists(file_path):
        raise ExtractionError("The uploaded document could not be read.",
                              detail=f"Document not found: {file_path}")

    try:
        content = build_content_blocks(file_path, max_pages=max_pages)
    except Exception as e:
        raise ExtractionError("The uploaded document could not be read. It may be corrupt "
                              "or password-protected.", detail=f"{type(e).__name__}: {e}")
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic(api_key=api_key)
    name = Path(file_path).name

    raw = ""
    budget = max_tokens
    # One retry with a bigger budget: a document whose table overruns the cap produces a
    # response cut off mid-string, which no amount of parsing can fully recover.
    for attempt in (1, 2):
        try:
            response = client.messages.create(
                model=model, max_tokens=budget,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e:
            logger.error(f"Claude API error extracting {name}: {type(e).__name__}: {e}")
            raise ExtractionError(detail=f"Claude API error: {type(e).__name__}: {e}")

        raw = response.content[0].text.strip() if response.content else ""
        if response.stop_reason != "max_tokens":
            break
        if attempt == 2 or budget >= MAX_TOKENS_CEILING:
            logger.warning(
                f"Extraction of {name} still hit the {budget}-token output cap; "
                f"parsing what came back (trailing rows may be dropped)."
            )
            break
        budget = min(budget * 2, MAX_TOKENS_CEILING)
        logger.warning(f"Extraction of {name} hit the output-token cap "
                       f"({len(raw)} chars); retrying with max_tokens={budget}.")

    if not raw:
        raise ExtractionError(detail=f"Claude returned an empty response for {name}.")

    try:
        data, partial = parse_json_response(raw)
    except ExtractionError as e:
        logger.error(f"Extraction parse failed for {name}: {e.detail}")
        raise
    if partial:
        data["_extraction_partial"] = True
        logger.warning(f"Extraction of {name} was recovered from a truncated response.")
    return data


def _gemini_parts(file_path: str, prompt: str, max_pages: int | None = None) -> list[dict]:
    """Build Gemini content parts — prefer native PDF for text PDFs, images for scans."""
    from infrastructure.document_ai.document_profile import is_text_pdf

    ext = Path(file_path).suffix.lower()
    parts: list[dict] = []

    if ext == ".pdf" and is_text_pdf(file_path, max_pages=max_pages):
        with open(file_path, "rb") as f:
            raw = f.read()
        parts.append({
            "inline_data": {
                "mime_type": "application/pdf",
                "data": base64.standard_b64encode(raw).decode("utf-8"),
            }
        })
    else:
        for img, media in _pdf_to_page_images(file_path, max_pages=max_pages) if ext == ".pdf" else []:
            parts.append({
                "inline_data": {
                    "mime_type": media,
                    "data": base64.standard_b64encode(img).decode("utf-8"),
                }
            })
        if ext != ".pdf":
            blocks = build_content_blocks(file_path, max_pages=max_pages)
            for b in blocks:
                if b.get("type") == "image":
                    src = b["source"]
                    parts.append({
                        "inline_data": {
                            "mime_type": src["media_type"],
                            "data": src["data"],
                        }
                    })
    parts.append({"text": prompt + "\n\nReturn ONLY valid JSON matching the schema in the prompt."})
    return parts


def extract_with_gemini(
    file_path: str,
    prompt: str,
    api_key: str,
    model: str = GEMINI_DEFAULT_MODEL,
    max_pages: int | None = None,
) -> dict:
    """Gemini Flash vision — fast/cheap for scans and messy layouts."""
    if not api_key:
        raise ExtractionError("Gemini extraction is not configured.",
                              detail="GEMINI_API_KEY is not set.")
    if not os.path.exists(file_path):
        raise ExtractionError("The uploaded document could not be read.",
                              detail=f"Document not found: {file_path}")

    name = Path(file_path).name
    parts = _gemini_parts(file_path, prompt, max_pages=max_pages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
        },
    }
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, params={"key": api_key}, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as e:
        logger.error(f"Gemini API error extracting {name}: {e}")
        raise ExtractionError(detail=f"Gemini API error: {e}")

    candidates = body.get("candidates") or []
    if not candidates:
        raise ExtractionError(detail=f"Gemini returned no candidates for {name}.")
    parts_out = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(p.get("text", "") for p in parts_out).strip()
    if not raw:
        raise ExtractionError(detail=f"Gemini returned empty response for {name}.")

    try:
        data, partial = parse_json_response(raw)
    except ExtractionError:
        data, partial = parse_json_response(json.dumps(json.loads(raw)))
    data["_extraction_method"] = f"gemini:{model}"
    if partial:
        data["_extraction_partial"] = True
    logger.info(f"Gemini extraction OK — {name}")
    return data


def _try_text_parse(file_path: str, doc_type: str, min_confidence: float) -> dict | None:
    """Rule-based text-parser tier — not implemented yet. Returns None (falls through to
    Gemini/Claude) rather than raising, so flipping EXTRACTION_TEXT_PARSER_ENABLED=True
    before the parser module exists degrades gracefully instead of breaking uploads."""
    try:
        from infrastructure.document_ai.text_parsers.router import try_text_parse
    except ImportError:
        logger.debug("EXTRACTION_TEXT_PARSER_ENABLED is set but no text-parser module is installed; skipping tier.")
        return None
    return try_text_parse(file_path, doc_type, min_confidence=min_confidence)


def extract_document(
    file_path: str,
    prompt: str,
    api_key: str,
    doc_type: str = "generic",
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_pages: int | None = None,
    gemini_api_key: str | None = None,
    text_parser_enabled: bool = False,
    gemini_enabled: bool = True,
    claude_fallback: bool = True,
    text_min_confidence: float = 0.45,
) -> dict:
    """
    Tiered extraction: (optional text parser) -> Gemini Flash -> Claude Sonnet.
    api_key is the Anthropic key (backward compatible with existing extractors).
    """
    name = Path(file_path).name

    if text_parser_enabled and doc_type != "generic":
        parsed = _try_text_parse(file_path, doc_type, text_min_confidence)
        if parsed:
            logger.info(
                f"Text parser hit — {name} doc_type={doc_type} "
                f"conf={parsed.get('_extraction_confidence')}"
            )
            return parsed

    gkey = gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_enabled and gkey:
        from config.settings import settings as _settings
        gemini_model = getattr(_settings, "GEMINI_MODEL", None) or GEMINI_DEFAULT_MODEL
        try:
            return extract_with_gemini(
                file_path, prompt, gkey, model=gemini_model, max_pages=max_pages,
            )
        except ExtractionError as e:
            logger.warning(f"Gemini failed for {name}, falling back: {e.detail}")

    if claude_fallback and api_key:
        data = extract_with_claude(
            file_path, prompt, api_key,
            model=model, max_tokens=max_tokens, max_pages=max_pages,
        )
        data["_extraction_method"] = f"claude:{model}"
        return data

    detail = "No text parser, Gemini, or Anthropic key available."
    if gemini_enabled and gkey:
        detail = "Gemini extraction failed and Claude fallback is disabled."
    elif not gkey and not claude_fallback:
        detail = "Gemini API key is not set and Claude fallback is disabled."

    raise ExtractionError(
        "AI extraction is not configured on this server.",
        detail=detail,
    )


def extraction_available() -> bool:
    """True if any extraction tier can run (text parser, Gemini, or Claude)."""
    from config.settings import settings
    return bool(
        settings.EXTRACTION_TEXT_PARSER_ENABLED
        or settings.GEMINI_API_KEY
        or settings.ANTHROPIC_API_KEY
    )


def extract_with_tiers(
    file_path: str,
    prompt: str,
    api_key: str,
    doc_type: str,
    max_pages: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Convenience wrapper — reads tier flags from settings. This is the entry point
    extractors should call instead of extract_with_claude() directly.

    Also records a real AI usage event (+ api_calls) for the current metering org.
    """
    from config.settings import settings
    from core.platform_metering import meter_extraction

    try:
        data = extract_document(
            file_path,
            prompt,
            api_key,
            doc_type=doc_type,
            model=model,
            max_tokens=max_tokens,
            max_pages=max_pages,
            gemini_api_key=settings.GEMINI_API_KEY,
            text_parser_enabled=settings.EXTRACTION_TEXT_PARSER_ENABLED,
            gemini_enabled=settings.EXTRACTION_GEMINI_ENABLED,
            claude_fallback=settings.EXTRACTION_CLAUDE_FALLBACK,
            text_min_confidence=settings.EXTRACTION_TEXT_MIN_CONFIDENCE,
        )
        method = None
        if isinstance(data, dict):
            method = data.get("_extraction_method")
        meter_extraction(doc_type=doc_type, success=True, model=str(method) if method else None)
        return data
    except Exception:
        meter_extraction(doc_type=doc_type, success=False, model=None)
        raise
