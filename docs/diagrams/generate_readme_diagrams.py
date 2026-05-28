from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


TITLE = font(34, True)
SUBTITLE = font(18)
LABEL = font(18, True)
BODY = font(15)
SMALL = font(13)

INK = "#1f2937"
MUTED = "#5f6b7a"
LINE = "#93a4b8"
BG = "#f7f9fc"
CARD = "#ffffff"
BLUE = "#2563eb"
GREEN = "#059669"
ORANGE = "#d97706"
RED = "#dc2626"
PURPLE = "#7c3aed"
TEAL = "#0891b2"


def wrap(draw: ImageDraw.ImageDraw, text: str, width: int, fnt: ImageFont.ImageFont) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textbbox((0, 0), trial, font=fnt)[2] <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def card(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], title: str, body: str, color: str) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=16, fill=CARD, outline="#d8e0ea", width=2)
    draw.rectangle((x1, y1, x1 + 8, y2), fill=color)
    draw.text((x1 + 24, y1 + 18), title, fill=INK, font=LABEL)
    y = y1 + 48
    for line in wrap(draw, body, x2 - x1 - 48, BODY):
        draw.text((x1 + 24, y), line, fill=MUTED, font=BODY)
        y += 21


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], color: str = LINE) -> None:
    draw.line((start, end), fill=color, width=3)
    x1, y1 = start
    x2, y2 = end
    if abs(x2 - x1) >= abs(y2 - y1):
        direction = 1 if x2 >= x1 else -1
        head = [(x2, y2), (x2 - direction * 12, y2 - 7), (x2 - direction * 12, y2 + 7)]
    else:
        direction = 1 if y2 >= y1 else -1
        head = [(x2, y2), (x2 - 7, y2 - direction * 12), (x2 + 7, y2 - direction * 12)]
    draw.polygon(head, fill=color)


def canvas(title: str, subtitle: str, size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(img)
    draw.text((48, 34), title, fill=INK, font=TITLE)
    draw.text((50, 78), subtitle, fill=MUTED, font=SUBTITLE)
    return img, draw


def save(img: Image.Image, name: str) -> None:
    img.save(OUT / name, "PNG", optimize=True)


def how_it_works() -> None:
    img, draw = canvas(
        "How Websearch Works",
        "A single local service turns a query into ranked, extracted, JSON-ready web context.",
        (1500, 880),
    )
    boxes = [
        ((70, 160, 330, 300), "Agent or App", "Calls REST /search or MCP websearch.search with a plain query.", BLUE),
        ((430, 160, 690, 300), "FastAPI Boundary", "Applies concurrency limits, queue timeout, and total request timeout.", PURPLE),
        ((790, 160, 1050, 300), "SearXNG", "Local metasearch returns titles, URLs, and snippets. No external API key.", GREEN),
        ((1150, 160, 1410, 300), "Candidate URLs", "Top results are deduplicated and normalized before extraction.", TEAL),
        ((250, 430, 510, 590), "Extraction", "For top K URLs: HTTP fast path, Crawl4AI CLI, then optional library fallback.", ORANGE),
        ((610, 430, 870, 590), "Cleanup and Scoring", "Boilerplate removal, quality scoring, and best-content selection.", GREEN),
        ((970, 430, 1230, 590), "JSON Response", "Always returns structured JSON with results and optional extracted_content.", BLUE),
        ((970, 680, 1230, 810), "Output and Events", "Optional result.md/result.json package plus NDJSON operational events.", PURPLE),
    ]
    for args in boxes:
        card(draw, *args)
    arrow(draw, (330, 230), (430, 230))
    arrow(draw, (690, 230), (790, 230))
    arrow(draw, (1050, 230), (1150, 230))
    arrow(draw, (1280, 300), (510, 430))
    arrow(draw, (510, 510), (610, 510))
    arrow(draw, (870, 510), (970, 510))
    arrow(draw, (1100, 590), (1100, 680))
    save(img, "how-it-works.png")


def deployment() -> None:
    img, draw = canvas(
        "Deployment Topology",
        "Start with one container; scale out behind nginx when agents begin calling concurrently.",
        (1600, 930),
    )
    card(draw, (70, 170, 350, 315), "Clients", "Agents, backend services, scripts, and humans using curl.", BLUE)
    card(draw, (455, 170, 735, 315), "nginx :9000", "Load balances requests and exposes one stable local endpoint.", TEAL)
    arrow(draw, (350, 242), (455, 242))

    for i, y in enumerate([105, 310, 515], start=1):
        card(draw, (880, y, 1220, y + 145), f"websearch replica {i}", "FastAPI + local SearXNG + Crawl4AI in one deployable unit.", GREEN)
        arrow(draw, (735, 242), (855, y + 72))

    card(draw, (910, 735, 1220, 875), "Shared Output", "Packages and event logs are written to ./output.", PURPLE)
    card(draw, (1265, 735, 1535, 875), "dashboard_logs", "Reads events and emits JSON snapshots to Docker logs.", ORANGE)
    arrow(draw, (1050, 660), (1050, 735))
    arrow(draw, (1220, 805), (1265, 805))

    draw.rounded_rectangle((870, 80, 1235, 690), radius=24, outline="#cbd5e1", width=3)
    draw.text((80, 830), "Scale with:", fill=INK, font=LABEL)
    draw.text((80, 862), "docker compose -f docker-compose.multi.yml up -d --scale websearch=5", fill=MUTED, font=SUBTITLE)
    save(img, "deployment-topology.png")


def integration() -> None:
    img, draw = canvas(
        "Integration Paths",
        "Use the same local search engine through REST, MCP JSON-RPC, or a CLI wrapper.",
        (1500, 880),
    )
    card(draw, (80, 170, 380, 340), "REST API", "POST /search with query, write_markdown_package, and package_name.", BLUE)
    card(draw, (80, 405, 380, 575), "MCP JSON-RPC", "POST /mcp, call tools/list, then tools/call websearch.search.", PURPLE)
    card(draw, (80, 640, 380, 790), "CLI", "python main.py search --query \"...\" for shell workflows.", ORANGE)
    card(draw, (565, 350, 895, 530), "Websearch Service", "One implementation path: admission control, SearXNG, extraction, cleanup, events.", GREEN)
    card(draw, (1080, 260, 1400, 420), "Agent Context", "Structured JSON content suitable for tool responses and citations.", TEAL)
    card(draw, (1080, 515, 1400, 675), "Artifacts", "Optional result.json and result.md for audit, debugging, and handoff.", PURPLE)

    arrow(draw, (380, 255), (565, 405))
    arrow(draw, (380, 490), (565, 440))
    arrow(draw, (380, 715), (565, 475))
    arrow(draw, (895, 410), (1080, 340))
    arrow(draw, (895, 475), (1080, 595))
    save(img, "integration-paths.png")


def timing() -> None:
    img, draw = canvas(
        "Tool Call Timing Budget",
        "Actual latency depends on search engines and target sites; defaults cap normal calls at 120 seconds.",
        (1500, 900),
    )
    rows = [
        ("Queue wait", "0-2s default", "If all local slots are busy, the API returns 503 after queue_timeout_seconds.", BLUE),
        ("SearXNG search", "0.5-30s typical cap", "Local metasearch request; retryable failures can add configured backoff.", GREEN),
        ("HTTP fast path", "1-8s common", "Direct page fetch for top K URLs; good pages can skip crawler work.", TEAL),
        ("Crawl4AI CLI", "10-60s cap", "Used when fast path quality is low or unavailable.", ORANGE),
        ("Library fallback", "up to 2 x 45s", "Only after CLI failure when fallback is enabled.", RED),
        ("Total API call", "120s default cap", "request_timeout_seconds bounds the complete /search or MCP tool call.", PURPLE),
    ]
    y = 155
    for name, budget, note, color in rows:
        draw.rounded_rectangle((80, y, 1420, y + 95), radius=16, fill=CARD, outline="#d8e0ea", width=2)
        draw.rectangle((80, y, 90, y + 95), fill=color)
        draw.text((115, y + 18), name, fill=INK, font=LABEL)
        draw.text((430, y + 18), budget, fill=color, font=LABEL)
        draw.text((430, y + 50), note, fill=MUTED, font=BODY)
        y += 115
    draw.text((82, 845), "Rule of thumb: healthy cached-ish searches often finish in 5-20s; blocked/slow pages hit the configured timeout envelope.", fill=INK, font=SUBTITLE)
    save(img, "timing-budget.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    how_it_works()
    deployment()
    integration()
    timing()
