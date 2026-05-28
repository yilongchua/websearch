from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import get_config_value

_LINK_PATTERN = re.compile(r"https?://")
_ALPHA_PATTERN = re.compile(r"[a-zA-Z]")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_WORD_PATTERN = re.compile(r"[a-zA-Z0-9]+")
_MD_LINK_PATTERN = re.compile(r"\[[^\]]+\]\([^)]+\)")

_ERROR_MARKERS = (
    "technical difficulties",
    "exception: forbidden",
    "access denied",
    "forbidden",
    "captcha",
    "cloudflare",
    "please enable javascript",
    "service unavailable",
    "error 403",
    "error 404",
    "error 500",
)

_BASE_BOILERPLATE_MARKERS = (
    "skip to content",
    "all rights reserved",
    "privacy policy",
    "terms of use",
    "cookie policy",
    "sign in",
    "subscribe",
    "newsletter",
    "facebook",
    "instagram",
    "youtube",
)


@lru_cache(maxsize=1)
def _load_cleanup_prompt() -> str:
    prompt_path = Path(str(get_config_value("cleanup.prompt_path", "/app/prompt/body_cleanup_prompt.j2")))
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _extract_prompt_markers(prompt_text: str) -> tuple[str, ...]:
    markers: set[str] = set()
    for raw_line in prompt_text.splitlines():
        line = raw_line.strip().lower()
        if not line:
            continue
        if "remove " not in line:
            continue

        after_remove = line.split("remove ", 1)[-1]
        after_remove = after_remove.replace(" and ", ",")
        for token in re.split(r"[,\.;]", after_remove):
            value = re.sub(r"[^a-z0-9\-\s]", "", token).strip()
            if len(value) < 4:
                continue
            markers.add(value)

    return tuple(sorted(markers))


def _active_boilerplate_markers() -> tuple[str, ...]:
    prompt_markers = _extract_prompt_markers(_load_cleanup_prompt())
    return tuple(dict.fromkeys([*list(_BASE_BOILERPLATE_MARKERS), *list(prompt_markers)]))


def _strip_markdown_links(text: str) -> str:
    return _MARKDOWN_LINK_PATTERN.sub(r"\1", text)


def _strip_html(text: str) -> str:
    value = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    value = re.sub(r"(?is)<style.*?>.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_extracted_content(text: str, *, max_chars: int | None = None) -> str:
    limit = max_chars or int(get_config_value("cleanup.max_chars", 20000))
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return ""

    prompt_markers = _active_boilerplate_markers()
    lines = normalized.split("\n")
    cleaned_lines: list[str] = []

    for raw_line in lines:
        line = _strip_markdown_links(raw_line).strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1]:
                cleaned_lines.append("")
            continue

        line_lower = line.lower()
        link_count = len(_LINK_PATTERN.findall(line))
        alpha_count = len(_ALPHA_PATTERN.findall(line))
        token_count = len(line.split())

        if any(marker in line_lower for marker in prompt_markers) and len(line) < 220:
            continue
        if line.startswith("* [") or line.startswith("- ["):
            continue
        if re.match(r"^[\|\-\s]+$", line):
            continue
        if link_count >= 3 and alpha_count < 120:
            continue
        if ("|" in line or " · " in line) and token_count > 8 and "." not in line and ":" not in line:
            continue
        if token_count <= 2 and not line.startswith("#"):
            continue

        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return ""

    if cleaned.count("<") > 30 and cleaned.count(">") > 30:
        cleaned = _strip_html(cleaned)

    return cleaned[:limit]


def assess_content_quality(text: str) -> dict[str, Any]:
    original = str(text or "")
    cleaned = clean_extracted_content(original)
    lowered = cleaned.lower()

    error_like = any(marker in lowered for marker in _ERROR_MARKERS)
    links = len(_MD_LINK_PATTERN.findall(original)) + len(_LINK_PATTERN.findall(original))
    words = _WORD_PATTERN.findall(cleaned)
    chars = len(cleaned)
    paragraphs = [chunk.strip() for chunk in re.split(r"\n\s*\n", cleaned) if chunk.strip()]
    paragraph_count = len(paragraphs)
    nav_like_lines = sum(1 for line in cleaned.splitlines() if any(marker in line.lower() for marker in _active_boilerplate_markers()))
    total_lines = max(1, len([line for line in cleaned.splitlines() if line.strip()]))
    nav_line_ratio = nav_like_lines / total_lines
    link_density = links / max(1, len(words))

    score = 0.0
    reasons: list[str] = []

    if chars >= 1500:
        score += 0.35
        reasons.append("sufficient_content_length")
    elif chars >= 700:
        score += 0.2
        reasons.append("moderate_content_length")
    else:
        reasons.append("content_too_short_after_cleanup")

    if paragraph_count >= 5:
        score += 0.25
        reasons.append("strong_paragraph_structure")
    elif paragraph_count >= 3:
        score += 0.15
        reasons.append("moderate_paragraph_structure")
    else:
        reasons.append("weak_paragraph_structure")

    if link_density <= 0.08:
        score += 0.2
        reasons.append("low_link_density")
    elif link_density <= 0.18:
        score += 0.1
        reasons.append("acceptable_link_density")
    elif link_density <= 0.55:
        reasons.append("high_link_density")
    else:
        score -= 0.15
        reasons.append("very_high_link_density")

    if nav_line_ratio <= 0.12:
        score += 0.2
        reasons.append("low_navigation_noise")
    elif nav_line_ratio <= 0.25:
        score += 0.1
        reasons.append("moderate_navigation_noise")
    else:
        reasons.append("high_navigation_noise")

    if error_like:
        score -= 0.6
        reasons.append("error_or_block_page_detected")

    score = max(0.0, min(1.0, score))
    return {
        "quality_score": round(score, 4),
        "quality_reasons": reasons,
        "cleaned_text": cleaned,
        "chars": chars,
        "paragraph_count": paragraph_count,
        "link_density": round(link_density, 4),
        "nav_line_ratio": round(nav_line_ratio, 4),
        "error_like": error_like,
        "cleanup_prompt_used": bool(_load_cleanup_prompt()),
    }
