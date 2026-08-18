#!/usr/bin/env python3
"""Build the static site and the one-page menu flyer from the SSoT YAML file."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "site.yml"
TEMPLATE_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DIST_DIR = ROOT / "dist"
PDF_PATH = DIST_DIR / "menu.pdf"
FONT_DIR = ROOT / "fonts"

EXPECTED_GRID = {"side_dish": (7, 3), "bento": (11, 3)}
PRICE_KINDS = {"amount", "surcharge", "discount"}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MENU_NAME_PADDING = 9.0
MENU_PRICE_LEFT_PADDING = 6.0
MENU_PRICE_RIGHT_PADDING = 9.0
MENU_WIDTH_BUFFER = 1.0


class BuildError(RuntimeError):
    """An actionable content or build validation failure."""


class EmbeddedFontCanvas(Canvas):
    """Start every PDF page with the bundled font, avoiding Helvetica resources."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["initialFontName"] = "NotoSansJP-Regular"
        super().__init__(*args, **kwargs)


def load_data() -> dict[str, Any]:
    try:
        with DATA_PATH.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise BuildError(f"SSoT not found: {DATA_PATH}") from exc
    if not isinstance(data, dict):
        raise BuildError("data/site.yml must contain a mapping at its root")
    return data


def validate_data(data: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[list[str]]]]:
    site = data.get("site")
    products = data.get("products")
    grid = data.get("pdf_grid")
    if not isinstance(site, dict) or not isinstance(products, list) or not isinstance(grid, dict):
        raise BuildError("site, products, and pdf_grid are required in data/site.yml")

    revision_date = str(site.get("revision_date", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", revision_date):
        raise BuildError("site.revision_date must be YYYY-MM-DD")
    if not site.get("base_url", "").endswith("/"):
        raise BuildError("site.base_url must end with /")
    if not isinstance(site.get("navigation"), list) or not site["navigation"]:
        raise BuildError("site.navigation must not be empty")
    structured_data = site.get("structured_data")
    if (
        not isinstance(structured_data, dict)
        or not isinstance(structured_data.get("description"), str)
        or not isinstance(structured_data.get("serves_cuisine"), str)
        or not isinstance(structured_data.get("price_range"), str)
    ):
        raise BuildError("site.structured_data.description/serves_cuisine/price_range are required strings")
    address = site.get("address")
    if not isinstance(address, dict) or not isinstance(address.get("country"), str):
        raise BuildError("site.address.country is required as a string")
    hours = site.get("hours")
    if (
        not isinstance(hours, list)
        or len(hours) != 2
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("start"), str)
            or not isinstance(item.get("end"), str)
            for item in hours
        )
    ):
        raise BuildError("site.hours must contain start/end mappings")
    flyer = site.get("flyer")
    if (
        not isinstance(flyer, dict)
        or not isinstance(flyer.get("sold_out_notice"), str)
        or not flyer["sold_out_notice"]
        or not isinstance(flyer.get("salmon_note"), str)
        or not isinstance(flyer.get("tax_note"), str)
    ):
        raise BuildError("site.flyer.sold_out_notice, salmon_note, and tax_note are required strings")
    menu_sections = data.get("menu_sections")
    if (
        not isinstance(menu_sections, list)
        or [item.get("id") for item in menu_sections if isinstance(item, dict)] != ["side_dish", "bento"]
        or any(not isinstance(item, dict) or not isinstance(item.get("label"), str) or not item["label"] for item in menu_sections)
    ):
        raise BuildError("menu_sections must define side_dish and bento in that order")

    by_id: dict[str, dict[str, Any]] = {}
    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            raise BuildError(f"products[{index}] must be a mapping")
        product_id = product.get("id")
        if not isinstance(product_id, str) or not ID_PATTERN.fullmatch(product_id):
            raise BuildError(f"products[{index}] has an invalid id: {product_id!r}")
        if product_id in by_id:
            raise BuildError(f"duplicate product id: {product_id}")
        if product.get("section") not in EXPECTED_GRID:
            raise BuildError(f"{product_id}: section must be side_dish or bento")
        for key in ("name", "description"):
            if not isinstance(product.get(key), str):
                raise BuildError(f"{product_id}: {key} must be a string")
        if "flyer_name" in product and not isinstance(product["flyer_name"], str):
            raise BuildError(f"{product_id}: flyer_name must be a string when provided")
        if "unit" not in product:
            raise BuildError(f"{product_id}: unit must be present (use null when not applicable)")
        price = product.get("price")
        if not isinstance(price, dict) or not isinstance(price.get("amount"), int):
            raise BuildError(f"{product_id}: price.amount must be an integer")
        if price.get("amount") < 0 or price.get("kind") not in PRICE_KINDS:
            raise BuildError(f"{product_id}: price must have a non-negative amount and valid kind")
        if not isinstance(product.get("salmon_included"), bool):
            raise BuildError(f"{product_id}: salmon_included must be true or false")
        by_id[product_id] = product

    if len(by_id) != 54:
        raise BuildError(f"expected 54 products, found {len(by_id)}")

    normalized_grid: dict[str, list[list[str]]] = {}
    for section, (expected_rows, expected_columns) in EXPECTED_GRID.items():
        section_grid = grid.get(section)
        if not isinstance(section_grid, list) or len(section_grid) != expected_rows:
            raise BuildError(f"pdf_grid.{section} must have {expected_rows} rows")
        rows: list[list[str]] = []
        seen: list[str] = []
        for row_number, row in enumerate(section_grid, start=1):
            if not isinstance(row, list) or len(row) != expected_columns:
                raise BuildError(f"pdf_grid.{section} row {row_number} must have {expected_columns} product IDs")
            for product_id in row:
                if product_id not in by_id:
                    raise BuildError(f"pdf_grid.{section} references unknown product id: {product_id}")
                if by_id[product_id]["section"] != section:
                    raise BuildError(f"pdf_grid.{section} references product in another section: {product_id}")
                seen.append(product_id)
            rows.append(row)
        if len(seen) != len(set(seen)):
            duplicates = sorted({item for item in seen if seen.count(item) > 1})
            raise BuildError(f"pdf_grid.{section} contains duplicate product IDs: {duplicates}")
        expected = {product_id for product_id, product in by_id.items() if product["section"] == section}
        if set(seen) != expected:
            missing = sorted(expected - set(seen))
            unlisted = sorted(set(seen) - expected)
            raise BuildError(f"pdf_grid.{section} mismatch; missing={missing}, unlisted={unlisted}")
        normalized_grid[section] = rows

    if set(grid) != set(EXPECTED_GRID):
        extra = sorted(set(grid) - set(EXPECTED_GRID))
        raise BuildError(f"pdf_grid contains unknown sections: {extra}")
    return by_id, normalized_grid


def price_label(product: dict[str, Any]) -> str:
    price = product["price"]
    sign = {"amount": "", "surcharge": "+", "discount": "-"}[price["kind"]]
    return f"{sign}{price['amount']}円"


def flyer_name(product: dict[str, Any]) -> str:
    return product.get("flyer_name") or product["name"]


def revision_display(site: dict[str, Any]) -> str:
    revision = dt.date.fromisoformat(site["revision_date"])
    # The current SSoT date is in the Reiwa era. Keep this conversion here so
    # the printed Japanese date cannot drift from site.revision_date.
    reiwa_start = dt.date(2019, 5, 1)
    if revision >= reiwa_start:
        return f"令和{revision.year - 2018}年{revision.month}月{revision.day}日"
    return revision.isoformat()


def hours_display(site: dict[str, Any]) -> list[str]:
    return [f"{item['start']} ~ {item['end']}" for item in site["hours"]]


def japanese_time(value: str) -> str:
    match = re.fullmatch(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", value)
    if not match:
        raise BuildError(f"invalid time value: {value!r}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise BuildError(f"invalid time value: {value!r}")
    return f"{hour}時" + (f"{minute}分" if minute else "")


def flyer_hours_display(site: dict[str, Any]) -> str:
    morning, evening = site["hours"]
    return (
        f"{japanese_time(morning['start'])}から{japanese_time(morning['end'])}、"
        f"{japanese_time(evening['start'])}から{japanese_time(evening['end'])}"
    )


def enrich_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for product in products:
        item = dict(product)
        item["price_label"] = price_label(item)
        item["flyer_label"] = f"{flyer_name(item)}★" if item["salmon_included"] else flyer_name(item)
        enriched.append(item)
    return enriched


def nl2br(value: str) -> Markup:
    return Markup("<br>".join(html.escape(part) for part in str(value).splitlines()))


def render_html(data: dict[str, Any], products: list[dict[str, Any]]) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        keep_trailing_newline=True,
    )
    env.filters["nl2br"] = nl2br
    site = data["site"]
    structured_data = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": site["author"],
        "description": site["structured_data"]["description"],
        "address": {
            "@type": "PostalAddress",
            "streetAddress": site["address"]["street"],
            "addressLocality": site["address"]["locality"],
            "addressRegion": site["address"]["region"],
            "postalCode": site["address"]["postal_code"],
            "addressCountry": site["address"]["country"],
        },
        "telephone": site["phone"]["display"],
        "servesCuisine": site["structured_data"]["serves_cuisine"],
        "priceRange": site["structured_data"]["price_range"],
        "openingHours": [f"Tu-Su {item['start']}-{item['end']}" for item in site["hours"]],
        "url": site["base_url"],
    }
    context = {
        **data,
        "products_by_section": {
            "side_dish": [item for item in products if item["section"] == "side_dish"],
            "bento": [item for item in products if item["section"] == "bento"],
        },
        "hours_display": hours_display(site),
        "structured_data": json.dumps(structured_data, ensure_ascii=False, indent=2),
        "js_plugins": [
            "js/jquery.min.js",
            "js/bootstrap.min.js",
            "js/slick.min.js",
            "js/wow.min.js",
            "js/venobox.min.js",
            "js/main.js",
        ],
        "css_plugins": [
            "css/bootstrap.min.css",
            "css/slick.css",
            "css/font-awesome.min.css",
            "css/animate.min.css",
            "css/venobox.css",
            "css/main.css",
            "css/responsive.css",
        ],
    }
    return env.get_template("index.html").render(**context)


def write_sitemap(site: dict[str, Any]) -> None:
    base_url = site["base_url"].rstrip("/")
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{xml_escape(base_url + '/')}</loc></url>\n"
        "</urlset>\n"
    )
    (DIST_DIR / "sitemap.xml").write_text(sitemap, encoding="utf-8")


def register_pdf_fonts() -> None:
    regular = FONT_DIR / "NotoSansJP-Regular.ttf"
    bold = FONT_DIR / "NotoSansJP-Bold.ttf"
    if not regular.is_file() or not bold.is_file():
        raise BuildError("NotoSansJP-Regular.ttf and NotoSansJP-Bold.ttf must be present in fonts")
    try:
        pdfmetrics.registerFont(TTFont("NotoSansJP-Regular", str(regular)))
        pdfmetrics.registerFont(TTFont("NotoSansJP-Bold", str(bold)))
    except Exception as exc:  # reportlab's parser has useful detail in the original exception
        raise BuildError(f"could not load bundled Noto Sans JP fonts: {exc}") from exc


def paragraph_text(value: str) -> str:
    return xml_escape(str(value)).replace("\n", "<br/>")


def measured_menu_column_widths(grid: list[list[str]], by_id: dict[str, dict[str, Any]]) -> list[float]:
    widths: list[float] = []
    for column_index in range(3):
        names: list[str] = []
        prices: list[str] = []
        for row in grid:
            product = by_id[row[column_index]]
            label = flyer_name(product) + ("★" if product["salmon_included"] else "")
            names.extend(label.splitlines() or [""])
            prices.append(price_label(product))
        name_width = max(
            (pdfmetrics.stringWidth(line, "NotoSansJP-Regular", 11.5) for line in names),
            default=0,
        )
        price_width = max(
            (pdfmetrics.stringWidth(line, "NotoSansJP-Regular", 11.5) for line in prices),
            default=0,
        )
        widths.extend(
            [
                name_width + (MENU_NAME_PADDING * 2) + MENU_WIDTH_BUFFER,
                price_width + MENU_PRICE_LEFT_PADDING + MENU_PRICE_RIGHT_PADDING + MENU_WIDTH_BUFFER,
            ]
        )
    content_width = 180 * mm
    minimum_width = sum(widths)
    if minimum_width > content_width:
        raise BuildError(
            f"menu table requires {minimum_width / mm:.1f}mm, exceeding the 180mm safe content width"
        )
    extra_name_width = (content_width - minimum_width) / 3
    for name_column in (0, 2, 4):
        widths[name_column] += extra_name_width
    return widths


def menu_table(grid: list[list[str]], by_id: dict[str, dict[str, Any]], styles: dict[str, ParagraphStyle]) -> Table:
    rows: list[list[Any]] = []
    for row in grid:
        cells: list[Any] = []
        for product_id in row:
            product = by_id[product_id]
            flyer_label = flyer_name(product)
            cells.extend(
                [
                    Paragraph(paragraph_text(flyer_label + ("★" if product["salmon_included"] else "")), styles["cell"]),
                    Paragraph(paragraph_text(price_label(product)), styles["price"]),
                ]
            )
        rows.append(cells)
    table = Table(rows, colWidths=measured_menu_column_widths(grid, by_id), hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#333333")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTNAME", (0, 0), (-1, -1), "NotoSansJP-Regular"),
                ("FONTSIZE", (0, 0), (-1, -1), 11.5),
                ("LEADING", (0, 0), (-1, -1), 14),
                ("LEFTPADDING", (0, 0), (-1, -1), MENU_NAME_PADDING),
                ("RIGHTPADDING", (0, 0), (-1, -1), MENU_PRICE_RIGHT_PADDING),
                ("LEFTPADDING", (1, 0), (1, -1), MENU_PRICE_LEFT_PADDING),
                ("LEFTPADDING", (3, 0), (3, -1), MENU_PRICE_LEFT_PADDING),
                ("LEFTPADDING", (5, 0), (5, -1), MENU_PRICE_LEFT_PADDING),
                ("TOPPADDING", (0, 0), (-1, -1), 8.0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8.0),
            ]
        )
    )
    return table


def note_table(site: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    table = Table(
        [
            [
                Paragraph(paragraph_text(site["flyer"]["salmon_note"]), styles["note-left"]),
                Paragraph(paragraph_text(site["flyer"]["tax_note"]), styles["note-right"]),
            ]
        ],
        colWidths=[90 * mm, 90 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "NotoSansJP-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10.5),
                ("LEADING", (0, 0), (-1, -1), 13),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def build_pdf(data: dict[str, Any], by_id: dict[str, dict[str, Any]], grid: dict[str, list[list[str]]]) -> None:
    register_pdf_fonts()
    styles = {
        "title": ParagraphStyle(
            "flyer-title", fontName="NotoSansJP-Bold", fontSize=24, leading=29, alignment=TA_CENTER, spaceAfter=5
        ),
        "meta": ParagraphStyle(
            "flyer-meta", fontName="NotoSansJP-Regular", fontSize=11, leading=15, alignment=TA_CENTER
        ),
        "section": ParagraphStyle(
            "flyer-section", fontName="NotoSansJP-Bold", fontSize=12.5, leading=15, alignment=TA_LEFT, spaceBefore=6, spaceAfter=5
        ),
        "cell": ParagraphStyle(
            "flyer-cell", fontName="NotoSansJP-Regular", fontSize=11.5, leading=14, alignment=TA_LEFT, wordWrap="CJK"
        ),
        "price": ParagraphStyle(
            "flyer-price", fontName="NotoSansJP-Regular", fontSize=11.5, leading=14, alignment=TA_RIGHT, wordWrap="CJK"
        ),
        "note-left": ParagraphStyle(
            "flyer-note-left", fontName="NotoSansJP-Bold", fontSize=10.5, leading=13, alignment=TA_LEFT
        ),
        "note-right": ParagraphStyle(
            "flyer-note-right", fontName="NotoSansJP-Bold", fontSize=10.5, leading=13, alignment=TA_RIGHT
        ),
    }
    site = data["site"]
    revision_label = revision_display(site)
    story: list[Any] = [
        Paragraph(paragraph_text(site["author"]), styles["title"]),
        Spacer(1, 4),
        Paragraph(paragraph_text(f"{revision_label}改定"), styles["meta"]),
        Paragraph(paragraph_text(f"電話番号　{site['phone']['display']}"), styles["meta"]),
        Paragraph(paragraph_text(f"営業時間　{flyer_hours_display(site)}"), styles["meta"]),
        Paragraph(paragraph_text(f"※{site['flyer']['sold_out_notice']}"), styles["meta"]),
        Paragraph(paragraph_text(f"定休日　{site['closed_days']}"), styles["meta"]),
        Spacer(1, 8),
        Paragraph(paragraph_text(data["menu_sections"][0]["label"]), styles["section"]),
        menu_table(grid["side_dish"], by_id, styles),
        Spacer(1, 6),
        Paragraph(paragraph_text(data["menu_sections"][1]["label"]), styles["section"]),
        menu_table(grid["bento"], by_id, styles),
        Spacer(1, 8),
        note_table(site, styles),
    ]
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"{site['author']} メニュー",
        author=site["author"],
    )
    try:
        doc.build(story, canvasmaker=EmbeddedFontCanvas)
    except Exception as exc:
        raise BuildError(f"PDF generation failed: {exc}") from exc
    verify_pdf(data, by_id, grid)


def pdf_font_names(reader: PdfReader) -> set[str]:
    names: set[str] = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        if not resources:
            continue
        fonts = resources.get("/Font", {})
        for font_ref in fonts.values():
            font = font_ref.get_object()
            base_font = str(font.get("/BaseFont", ""))
            if "NotoSansJP" in base_font:
                names.add(base_font)
    return names


def verify_pdf(data: dict[str, Any], by_id: dict[str, dict[str, Any]], grid: dict[str, list[list[str]]]) -> None:
    if not PDF_PATH.is_file():
        raise BuildError(f"PDF was not created: {PDF_PATH}")
    try:
        reader = PdfReader(str(PDF_PATH))
    except Exception as exc:
        raise BuildError(f"could not read generated PDF: {exc}") from exc
    if len(reader.pages) != 1:
        raise BuildError(f"menu.pdf must be exactly one page (found {len(reader.pages)})")
    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    expected_width, expected_height = A4
    if abs(width - expected_width) > 1 or abs(height - expected_height) > 1:
        raise BuildError(f"menu.pdf must be A4 portrait (found {width:.1f}x{height:.1f}pt)")
    font_names = pdf_font_names(reader)
    if len(font_names) < 2:
        raise BuildError(f"menu.pdf must embed both Noto Sans JP weights (found {sorted(font_names)})")
    for page in reader.pages:
        fonts = page.get("/Resources", {}).get("/Font", {})
        for font_ref in fonts.values():
            font = font_ref.get_object()
            descriptor = font.get("/FontDescriptor")
            if not descriptor or not any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                raise BuildError(f"menu.pdf contains an unembedded font resource: {font.get('/BaseFont')}")

    extracted = "".join((page.extract_text() or "").split())
    site = data["site"]
    normalized_extracted = extracted.replace(" ", "").replace("\u3000", "")
    for expected_text in (
        "4丁目のコロッケ屋さん",
        site["phone"]["display"],
        f"{revision_display(site)}改定",
        f"営業時間　{flyer_hours_display(site)}",
        f"※{site['flyer']['sold_out_notice']}",
        site["flyer"]["tax_note"].replace(" ", ""),
    ):
        if expected_text.replace(" ", "").replace("\u3000", "") not in normalized_extracted:
            raise BuildError(f"menu.pdf text is missing required content: {expected_text}")
    # Grid validation above proves IDs are unique and complete. Pairing each flyer
    # label with its price also confirms that all 54 rows made it into the PDF.
    for product_id in [item for rows in grid.values() for row in rows for item in row]:
        product = by_id[product_id]
        flyer = "".join(flyer_name(product).split())
        if product["salmon_included"]:
            flyer += "★"
        pair = flyer + price_label(product)
        if extracted.count(pair) != 1:
            raise BuildError(f"menu.pdf must contain one flyer row for {product_id}; found pair count {extracted.count(pair)}")
    if f"※{site['flyer']['sold_out_notice']}" not in extracted or site["closed_days"].replace(" ", "") not in extracted:
        raise BuildError("menu.pdf is missing store hours or closed-day text")


def clean_dist() -> None:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)


def full_build(data: dict[str, Any], by_id: dict[str, dict[str, Any]], grid: dict[str, list[list[str]]]) -> None:
    clean_dist()
    shutil.copytree(STATIC_DIR, DIST_DIR, dirs_exist_ok=True)
    (DIST_DIR / "index.html").write_text(render_html(data, enrich_products(data["products"])), encoding="utf-8")
    write_sitemap(data["site"])
    build_pdf(data, by_id, grid)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-only", action="store_true", help="regenerate and validate only dist/menu.pdf")
    args = parser.parse_args()
    try:
        data = load_data()
        by_id, grid = validate_data(data)
        if args.pdf_only:
            if not DIST_DIR.exists():
                DIST_DIR.mkdir(parents=True)
            build_pdf(data, by_id, grid)
        else:
            full_build(data, by_id, grid)
    except BuildError as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built {'PDF' if args.pdf_only else 'site and PDF'} successfully in {DIST_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
