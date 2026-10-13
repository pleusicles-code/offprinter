#!/usr/bin/env python3
"""OFFPRINTER: rotate, catalogue and export scholarly offprints."""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from PIL import Image
from openai import OpenAI
from pydantic import BaseModel

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
DEFAULT_MODEL = "gpt-5.4-mini"
HUF_PER_USD = 330.0
# Standard API token prices (USD / 1M tokens), fixed as of 2026-10-13.
# Tuple: regular input, cached input, output. Review periodically.
MODEL_PRICING = {
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
}
MIN_REASONING_MODELS = {"gpt-6.1-sol", "gpt-6-astra", "gemini-3.8-flash"}


def calculate_cost_huf(model, input_tokens, output_tokens, cached_input_tokens=0):
    rates = MODEL_PRICING.get(model)
    if rates is None or not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if not isinstance(cached_input_tokens, int) or not 0 <= cached_input_tokens <= input_tokens:
        cached_input_tokens = 0
    normal, cached, output = rates
    usd = ((input_tokens - cached_input_tokens) * normal
           + cached_input_tokens * cached + output_tokens * output) / 1_000_000
    return round(usd * HUF_PER_USD, 1)


def choose_model():
    print("Select API model (standard USD per million tokens):", flush=True)
    names = list(MODEL_PRICING)
    for i, name in enumerate(names, 1):
        inp, _, out = MODEL_PRICING[name]
        print(f"  {i}. {name:<17} input ${inp:g} / output ${out:g}", flush=True)
    if not sys.stdin.isatty():
        raise ValueError("--model without a value requires an interactive terminal")
    while True:
        try:
            answer = input(f"Model [1-{len(names)}] (Enter for {DEFAULT_MODEL}): ").strip()
        except EOFError as exc:
            raise ValueError("No model selected") from exc
        if not answer:
            return DEFAULT_MODEL
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        if answer in MODEL_PRICING:
            return answer
        print("Invalid selection; enter a number from the list.", flush=True)


class ReviewedWork(BaseModel):
    author: str | None
    title: str | None
    publication_place: str | None
    publisher: str | None


class Metadata(BaseModel):
    author: str | None
    author_normalized: str | None
    title: str | None
    publication: str | None
    volume: str | None
    issue: str | None
    year: str | None
    offprint_year: str | None
    pages: str | None
    publication_place: str | None
    publisher: str | None
    doi: str | None
    contributors: str | None
    reviewed_work: ReviewedWork | None
    ocr_confidence: int
    numeric_confidence: int | None
    bibliographic_confidence: int
    handwriting: str
    handwriting_type: str | None
    handwriting_text: str | None
    handwriting_notes: str | None
    needs_review: bool
    review_reason: str | None


def natural_key(p):
    return [(0, int(t)) if t.isdigit() else (1, t.lower())
            for t in re.split(r"(\d+)", p.name)]


def selected_boxes(root, selection):
    if selection is None:
        return sorted((p for p in root.iterdir()
                       if p.is_dir() and re.fullmatch(r"Box\d{3}", p.name, re.I)),
                      key=lambda p: int(p.name[-3:]))
    if "-" in selection:
        left, right = selection.split("-", 1)
        if not left.isdigit() or not right.isdigit() or int(left) > int(right):
            raise ValueError("Invalid --box range")
        numbers = range(int(left), int(right) + 1)
    elif selection.isdigit():
        numbers = [int(selection)]
    else:
        raise ValueError("--box should be an integer or inclusive range, e.g. 1-5")
    result = []
    for n in numbers:
        choices = [p for p in root.iterdir() if p.is_dir()
                   and p.name.lower() == f"box{n:03d}"]
        if not choices:
            raise ValueError(f"Missing box directory: Box{n:03d}")
        result.append(choices[0])
    return result


def units(box):
    def photos(folder):
        return sorted((p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in EXTENSIONS),
                      key=natural_key)
    singles = [([p], p) for p in photos(box)]
    doubles = box / "double"
    pairs = []
    if doubles.is_dir():
        images = photos(doubles)
        if len(images) % 2:
            raise ValueError(f"{doubles}: odd number of photos ({len(images)})")
        pairs = [([images[i], images[i + 1]], images[i])
                 for i in range(0, len(images), 2)]
    return singles + pairs


def model_json_path(photo, model):
    """Output path for one model's independent extraction of an offprint."""
    if model not in MODEL_PRICING:
        raise ValueError(f"Unknown model: {model}")
    return photo.with_name(photo.stem + "." + model + ".json")


def model_or_legacy_path(photo, model):
    """Resolve a model record, falling back to legacy mini JSON if appropriate."""
    named = model_json_path(photo, model)
    if named.exists():
        return named
    if model == DEFAULT_MODEL:
        legacy = photo.with_suffix(".json")
        if legacy.exists():
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                recorded_model = (data.get("processing") or {}).get("model")
                if recorded_model in (None, DEFAULT_MODEL):
                    return legacy
            except (ValueError, OSError, AttributeError):
                pass
    return None


def existing_json_paths(photo):
    """Discover model-specific files and earlier legacy unsuffixed JSON."""
    candidates = [photo.with_suffix(".json")]
    candidates.extend(sorted(photo.parent.glob(photo.stem + ".*.json")))
    return [p for p in dict.fromkeys(candidates) if p.is_file()]


def fix_rotation(path):
    with Image.open(path) as original:
        orientation = original.getexif().get(274, 1)
        if orientation != 3:
            return False
        image = original.rotate(270, expand=True)
        exif = original.getexif()
        exif[274] = 1
        # Remove embedded thumbnail / stale preview, if any.
        try:
            del exif[0x501B]
        except KeyError:
            pass
        tmp = path.with_name(path.stem + ".offprinter-tmp" + path.suffix)
        options = {"exif": exif.tobytes()}
        if original.info.get("icc_profile"):
            options["icc_profile"] = original.info["icc_profile"]
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            options.update(quality=95, subsampling=0)
        fmt = original.format
        try:
            image.save(tmp, format=fmt, **options)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return True


def image_content(path):
    types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png", ".webp": "image/webp"}
    # Normalize TIFF in memory, since TIFF is not an API image input format.
    if path.suffix.lower() not in types:
        from io import BytesIO
        with Image.open(path) as im:
            buf = BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=90)
            data = buf.getvalue()
        mime = "image/jpeg"
    else:
        mime = types[path.suffix.lower()]
        data = path.read_bytes()
    return {"type": "input_image",
            "image_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            "detail": "high"}


def load_key(filename=".openai_api_key"):
    path = ROOT / filename
    if not path.is_file():
        raise FileNotFoundError(f"Create local API key file: {path}")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("Local API key file is empty")
    return key


def write_json_atomic(path, data, backup):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    try:
        if backup and path.exists():
            os.replace(path, path.with_suffix(".json.bak"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)



def roman_to_int(letters):
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    text = letters.upper()
    total, last = 0, 0
    for c in reversed(text):
        n = values[c]
        total += -n if n < last else n
        last = n
    # Reject invalid numerals, including accidental words.
    def to_roman(n):
        result = ""
        for val, token in [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),
                           (100,"C"),(90,"XC"),(50,"L"),(40,"XL"),
                           (10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]:
            result += token * (n // val)
            n %= val
        return result
    return total if total > 0 and to_roman(total) == text else None


def normalize_number_field(value):
    """Conservatively remove volume/issue labels and convert Roman numerals."""
    if not value:
        return value
    text = str(value).strip()
    text = re.sub(
        r"(?i)\b(?:tomus|t|vol(?:ume)?|band|bd|jahrgang|jg|évf(?:olyam)?|"
        r"fasc(?:iculus)?|heft|no|nr|szám)\b\.?",
        " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:.")
    text = re.sub(r"\s*[\u2012-\u2015-]\s*", "-", text)
    # Only normalize when the remainder consists of numbers/Roman numerals
    # and optional single-letter suffixes (e.g. 2A). Preserve complex strings.
    parts = text.split("-")
    cleaned = []
    for part in parts:
        part = part.strip().strip(".")
        if re.fullmatch(r"[IVXLCDM]+", part, re.I):
            converted = roman_to_int(part)
            cleaned.append(str(converted) if converted is not None else part)
        else:
            cleaned.append(part)
    return "-".join(cleaned) or None



def normalize_pages(value):
    """Do not mistake isolated page numbers or annotations for ranges."""
    if value is None:
        return None
    value = re.sub(r"\s*[–—−-]\s*", "-", str(value).strip())
    return value if re.fullmatch(r"\d+[A-Za-z]?-\d+[A-Za-z]?", value) else None


def composite_confidence(item):
    """Average available confidence scores; missing fields do not reduce confidence."""
    fields = ("ocr_confidence", "numeric_confidence", "bibliographic_confidence")
    available = [item.get(k) for k in fields
                 if isinstance(item.get(k), (int, float))
                 and not isinstance(item.get(k), bool)]
    return round(sum(available) / len(available), 1) if available else None


def estimate_cost(item):
    usage = item.get("processing") or {}
    computed = calculate_cost_huf(
        usage.get("model"), usage.get("input_tokens"), usage.get("output_tokens"),
        usage.get("cached_input_tokens", 0))
    return computed if computed is not None else (
        round(usage["cost_huf"], 1) if isinstance(usage.get("cost_huf"), (int, float)) else None)


def normalize_author_fields(author, normalized):
    """Fix the Ritoók spelling and avoid commas in unchanged Hungarian name order."""
    def fix_spelling(value):
        if not isinstance(value, str):
            return value
        return re.sub(r"(?i)\b(?:Ritók|Ritóók|Ritook)\b", lambda m: (
            "RITOÓK" if m.group().isupper() else
            "ritoók" if m.group().islower() else "Ritoók"
        ), value)

    author = fix_spelling(author)
    normalized = fix_spelling(normalized)
    if isinstance(author, str) and isinstance(normalized, str):
        # Remove only one comma, and only if that reproduces the printed name exactly.
        if normalized.count(",") == 1 and normalized.replace(",", "").split() == author.split():
            normalized = normalized.replace(",", "", 1).strip()
    return author, normalized



def gemini_image_part(path):
    """Use the existing image conversion, including TIFF to JPEG."""
    from google.genai import types
    image = image_content(path)
    mime, encoded = image["image_url"].split(";base64,", 1)
    return types.Part.from_bytes(
        data=base64.b64decode(encoded), mime_type=mime.removeprefix("data:"))


def call_gemini(client, model, reasoning, prompt, images):
    from google.genai import types
    result = client.models.generate_content(
        model=model,
        contents=[prompt] + [gemini_image_part(p) for p in images],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Metadata,
            thinking_config=types.ThinkingConfig(thinking_level=reasoning),
        ),
    )
    if not result.text:
        raise RuntimeError("Gemini returned no JSON response")
    parsed = Metadata.model_validate_json(result.text)
    usage = result.usage_metadata
    input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    output_tokens = (
        (getattr(usage, "candidates_token_count", 0) or 0)
        + (getattr(usage, "thoughts_token_count", 0) or 0)
    ) if usage else None
    cached_tokens = (getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0
    return parsed, input_tokens, output_tokens, cached_tokens


def extract(client, model, reasoning, prompt, box, images):
    if model == "gemini-3.8-flash":
        parsed, input_tokens, output_tokens, cached_tokens = call_gemini(
            client, model, reasoning, prompt, images)
    else:
        content = [{"type": "input_text", "text": prompt}]
        content.extend(image_content(p) for p in images)
        response = client.responses.parse(
            model=model,
            reasoning={"effort": reasoning},
            input=[{"role": "user", "content": content}],
            text_format=Metadata,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("Model did not return a structured bibliographic record")
        parsed = response.output_parsed
        input_tokens = response.usage.input_tokens if response.usage else None
        output_tokens = response.usage.output_tokens if response.usage else None
        details = getattr(response.usage, "input_tokens_details", None) if response.usage else None
        cached_tokens = getattr(details, "cached_tokens", 0) or 0
    data = parsed.model_dump()
    data["author"], data["author_normalized"] = normalize_author_fields(
        data.get("author"), data.get("author_normalized"))
    if data["handwriting"] not in {"Yes", "No", "Uncertain"}:
        raise ValueError("Invalid handwriting status")
    for field in ("volume", "issue"):
        data[field] = normalize_number_field(data[field])
    data["pages"] = normalize_pages(data["pages"])
    scores = ("ocr_confidence", "numeric_confidence", "bibliographic_confidence")
    if not any(data.get(k) for k in ("year", "offprint_year", "volume", "issue")):
        data["numeric_confidence"] = None
    for k in scores:
        v = data[k]
        if v is not None and (type(v) is not int or not 1 <= v <= 10):
            raise ValueError(f"{k} must be an integer from 1 to 10 or null")
    observed = [data[k] for k in scores if data[k] is not None]
    data["confidence_composite"] = round(sum(observed) / len(observed), 1) if observed else None
    composite = data.pop("confidence_composite")
    ordered = {}
    for key, val in data.items():
        if key == "handwriting":
            ordered["confidence_composite"] = composite
        ordered[key] = val
    data = ordered
    if data["year"]:
        data["offprint_year"] = None
    missing = [field for field in ("author", "title")
               if not (data.get(field) or "").strip()]
    if not (data.get("year") or data.get("offprint_year")):
        missing.append("year")
    if missing:
        data["needs_review"] = True
        reason = "Missing required bibliographic information: " + ", ".join(missing) + "."
        existing = (data.get("review_reason") or "").strip()
        data["review_reason"] = (reason + " " + existing).strip()
    if not data["needs_review"]:
        data["review_reason"] = None
    data["box"] = box.name
    data["images"] = [p.relative_to(box).as_posix() for p in images]
    cost_huf = calculate_cost_huf(model, input_tokens, output_tokens, cached_tokens)
    data["processing"] = {
        "model": model,
        "reasoning": reasoning,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens,
        "cost_huf": cost_huf,
        "flagged": False,
    }
    return data


HEADERS = [
    "Box", "Photograph 1", "Photograph 2", "Author", "Author_normalized", "Title",
    "Publication", "Volume", "Issue", "Year",
    "Offprint year", "Pages", "Place", "Publisher",
    "DOI", "Contributors", "Reviewed work",
    "OCR confidence", "Numeric confidence", "Bibliographic confidence", "Composite confidence", "Handwriting",
    "Handwriting type", "Handwriting text", "Handwriting notes",
    "Needs review", "Review reason", "Model", "Processed at",
    "cost_huf",
]


def export_xlsx(records, outfile, cost_records=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    sh = wb.active
    sh.title = "Catalogue"
    sh.append(HEADERS)
    summary = Counter()
    summary_stats = {}
    for path, item in records:
        box = item.get("box") or (path.parent.parent.name if path.parent.name == "double" else path.parent.name)
        images = item.get("images") or []
        work = item.get("reviewed_work") or {}
        if not isinstance(work, dict):
            work = {}
        book = "; ".join(str(work[k]) for k in ("author", "title") if work.get(k))
        usage = item.get("processing") or {}
        author, author_normalized = normalize_author_fields(
            item.get("author"), item.get("author_normalized"))
        row = [
            box, images[0] if images else None, images[1] if len(images) > 1 else None,
            author, author_normalized, item.get("title"), item.get("publication"), item.get("volume"), item.get("issue"),
            item.get("year"), item.get("offprint_year"), normalize_pages(item.get("pages")),
            item.get("publication_place"), item.get("publisher"), item.get("doi"),
            item.get("contributors"), book,
            item.get("ocr_confidence"), item.get("numeric_confidence"),
            item.get("bibliographic_confidence"), composite_confidence(item),
            item.get("handwriting"),
            item.get("handwriting_type"), item.get("handwriting_text"),
            item.get("handwriting_notes"), "Yes" if item.get("needs_review") else "No",
            item.get("review_reason"), usage.get("model"), usage.get("processed_at"),
            estimate_cost(item),
        ]
        sh.append(row)
        current = sh.max_row
        for column, relative_path in ((2, images[0] if images else None),
                                       (3, images[1] if len(images) > 1 else None)):
            if relative_path:
                cell = sh.cell(current, column)
                cell.hyperlink = (ROOT / box / relative_path).resolve().as_uri()
                cell.style = "Hyperlink"
        summary[box] += 1
        v = summary_stats.setdefault(box, {"single": 0, "double": 0, "review": 0, "handwriting": 0,
                                            "missing_date": 0, "cost": 0.0, "priced": 0})
        v["double" if len(images) == 2 else "single"] += 1
        v["review"] += bool(item.get("needs_review"))
        v["handwriting"] += (item.get("handwriting") == "Yes")
        v["missing_date"] += not bool(item.get("year") or item.get("offprint_year"))
        price = estimate_cost(item)
        if price is not None:
            v["cost"] += price
            v["priced"] += 1
    sh.freeze_panes = "A2"
    sh.auto_filter.ref = sh.dimensions
    sh.sheet_view.showGridLines = False
    for cell in sh[1]:
        cell.fill = PatternFill("solid", fgColor="23445A")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center")
    sh.row_dimensions[1].height = 26
    for i, name in enumerate(HEADERS, 1):
        sh.column_dimensions[get_column_letter(i)].width = (
            44 if name in {"Title", "Publication", "Handwriting text", "Review reason"}
            else 28 if name in {"Photograph 1", "Photograph 2"}
            else 13 if name == "Box" else 20
        )

    expense = Counter()
    expense_n = Counter()
    for path, item in (cost_records if cost_records is not None else records):
        box_name = item.get("box") or (path.parent.parent.name if path.parent.name == "double" else path.parent.name)
        amount = estimate_cost(item)
        if amount is not None:
            expense[box_name] += amount
            expense_n[box_name] += 1

    stats = wb.create_sheet("Summary")
    cols = ["Box", "Records", "Single", "Double", "Needs review", "Handwriting yes",
            "Missing date", "Selected cost HUF", "Selected priced", "All passes cost HUF", "All passes priced"]
    stats.append(cols)
    total = {k: 0 for k in ("single", "double", "review", "handwriting", "missing_date", "cost", "priced")}
    for box, count in sorted(summary.items()):
        v = summary_stats[box]
        stats.append([box, count, v["single"], v["double"], v["review"],
                      v["handwriting"], v["missing_date"], round(v["cost"], 1), v["priced"], round(expense[box], 1), expense_n[box]])
        for k in total:
            total[k] += v[k]
    stats.append(["TOTAL", len(records), total["single"], total["double"],
                  total["review"], total["handwriting"], total["missing_date"],
                  round(total["cost"], 1), total["priced"], round(sum(expense.values()), 1), sum(expense_n.values())])
    for cell in stats[1]:
        cell.fill = PatternFill("solid", fgColor="23445A")
        cell.font = Font(bold=True, color="FFFFFF")
    for j in range(1, len(cols) + 1):
        stats.column_dimensions[get_column_letter(j)].width = 21
    stats.freeze_panes = "A2"
    for sheet in (sh, stats):
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.border = Border(bottom=Side(style="hair", color="CDD5DD"))
    for col in (8, 10):
        for row in stats.iter_rows(min_row=2, min_col=col, max_col=col):
            row[0].number_format = '#,##0.0 "Ft"'
    for row in sh.iter_rows(min_row=2, min_col=len(HEADERS), max_col=len(HEADERS)):
        row[0].number_format = '0.0 "Ft"'
    wb.save(outfile)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-b", "--box", help="box number or inclusive range (e.g. 3 or 1-5)")
    ap.add_argument("-o", "--operation", default="fix,json,xls",
                    help="comma-separated operations: fix,json,xls")
    force_group = ap.add_mutually_exclusive_group()
    force_group.add_argument("-f", "--force", action="store_true",
                             help="reprocess and back up all existing JSON")
    force_group.add_argument("--force-flagged", action="store_true",
                             help="reprocess and back up JSON with processing.flagged=true")
    force_group.add_argument("--force-below", metavar="SCORE_OR_PERCENT",
                             help="reprocess below score 1-10 or lowest percentage e.g. 20%")
    ap.add_argument("-m", "--model", nargs="?", const="__MENU__", default=DEFAULT_MODEL,
                    help="API model ID; omit value to choose interactively")
    ap.add_argument("--below-model", choices=tuple(MODEL_PRICING),
                    help="standard model for confidence-based escalation and XLSX fallback")
    ap.add_argument("--reasoning", choices=("none", "low", "medium", "high", "xhigh"), default="none",
                    help="model reasoning effort (default: none)")
    ap.add_argument("--limit", type=int, help="maximum offprints to process (testing)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    operations = {x.strip().lower() for x in args.operation.split(",")}
    if not operations or operations - {"fix", "json", "xls"}:
        ap.error("operations must be fix,json,xls in any combination")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")
    percentile = None
    if args.force_below is not None:
        raw = args.force_below.strip()
        try:
            if raw.endswith("%"):
                percentile = float(raw[:-1])
                if not math.isfinite(percentile) or not 0 < percentile <= 100:
                    raise ValueError
            else:
                args.force_below = float(raw)
                if not math.isfinite(args.force_below) or not 1 <= args.force_below <= 10:
                    raise ValueError
        except ValueError:
            ap.error("--force-below must be a score from 1 to 10 or a percentage 0%-100%")
    if args.model == "__MENU__":
        try:
            args.model = choose_model()
        except ValueError as exc:
            ap.error(str(exc))
    if args.model not in MODEL_PRICING:
        ap.error("Unknown model; run --model without a value to see supported models")
    if args.below_model == args.model:
        ap.error("--below-model must differ from --model")
    if args.below_model and args.force_below is None and "json" in operations:
        ap.error("--below-model with JSON extraction requires --force-below")
    if args.model == "gemini-3.8-flash" and args.reasoning == "xhigh":
        ap.error("Gemini 3.8 Flash supports low, medium or high reasoning, not xhigh")
    if args.model in MIN_REASONING_MODELS and args.reasoning == "none":
        print(f"{args.model} requires reasoning; using low instead of none.")
        args.reasoning = "low"
    boxes = selected_boxes(ROOT, args.box)
    if not boxes:
        ap.error("No BoxXXX folders found beside offprinter.py")
    tasks = [(box, ims, model_json_path(photo, args.model))
             for box in boxes for ims, photo in units(box)]
    if args.limit:
        tasks = tasks[:args.limit]
    percentile_selected = None
    if percentile is not None and "json" in operations:
        eligible = []
        reference_model = args.below_model or args.model
        for _, images, _ in tasks:
            source = model_or_legacy_path(images[0], reference_model)
            if source is None:
                continue
            try:
                item = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(item, dict):
                    raise ValueError("JSON root must be an object")
                score = composite_confidence(item)
                if score is not None:
                    eligible.append((score, images[0]))
            except (ValueError, OSError) as exc:
                ap.error(f"Cannot rank {source}: {exc}")
        count = math.ceil(len(eligible) * percentile / 100)
        eligible.sort(key=lambda entry: entry[0])
        percentile_selected = {photo for _, photo in eligible[:count]}
        print(f"Lowest {percentile:g}%: selected {count} of {len(eligible)} "
              f"scored {reference_model} records in the selected boxes/limit.")
    print(f"Selected {len(boxes)} boxes, {len(tasks)} offprints; operations: {','.join(sorted(operations))}")
    if args.dry_run:
        for box, ims, dest in tasks:
            print(f"{dest.relative_to(ROOT)} <= {', '.join(p.name for p in ims)}")
        return
    client = None
    if "json" in operations:
        if args.model == "gemini-3.8-flash":
            from google import genai
            client = genai.Client(api_key=load_key(".gemini_api_key"))
        else:
            client = OpenAI(api_key=load_key(), max_retries=3, timeout=90)
    prompt = (SCRIPT_DIR / "prompt.txt").read_text(encoding="utf-8") if client else None
    failures = 0
    for number, (box, images, dest) in enumerate(tasks, 1):
        print(f"[{number}/{len(tasks)}] {box.name}: {', '.join(p.name for p in images)}", flush=True)
        if "fix" in operations:
            for p in images:
                try:
                    if fix_rotation(p):
                        print("  rotated:", p.name)
                except Exception as exc:
                    failures += 1
                    print(f"  ERROR rotating {p}: {exc}", file=sys.stderr)
                    continue
        if "json" in operations:
            try:
                if percentile_selected is not None:
                    reference = model_or_legacy_path(images[0], args.below_model or args.model)
                    if reference is not None and images[0] not in percentile_selected:
                        print("  outside lowest-confidence percentile; skipped")
                        continue
                    if reference is None and args.below_model:
                        print("  standard-model JSON missing; skipped")
                        continue
                if args.below_model:
                    standard_path = model_or_legacy_path(images[0], args.below_model)
                    if standard_path is None:
                        print("  standard-model JSON missing; skipped")
                        continue
                    standard = json.loads(standard_path.read_text(encoding="utf-8"))
                    if not isinstance(standard, dict):
                        raise ValueError("Standard-model JSON root is not an object")
                    score = composite_confidence(standard)
                    if score is None or (percentile_selected is None and score >= args.force_below):
                        print(f"  standard confidence {score}: skipped")
                        continue
                    if dest.exists():
                        print("  advanced JSON exists; skipped")
                        continue
                    print(f"  standard confidence {score}: escalating")
                if dest.exists() and not args.force:
                    if not args.force_flagged and args.force_below is None:
                        print("  JSON exists; skipped")
                        continue
                    existing = json.loads(dest.read_text(encoding="utf-8"))
                    if not isinstance(existing, dict):
                        raise ValueError("Existing JSON root is not an object")
                    if args.force_flagged:
                        processing = existing.get("processing") or {}
                        if not isinstance(processing, dict):
                            raise ValueError("Existing JSON processing is not an object")
                        if processing.get("flagged") is not True:
                            print("  JSON not flagged; skipped")
                            continue
                        print("  flagged: reprocessing")
                    elif args.force_below is not None:
                        confidence = composite_confidence(existing)
                        if confidence is None or (percentile_selected is None and confidence >= args.force_below):
                            print(f"  confidence {confidence}: skipped")
                            continue
                        print(f"  confidence {confidence}: reprocessing")
                result = extract(client, args.model, args.reasoning, prompt, box, images)
                write_json_atomic(dest, result, backup=dest.exists())
                print(f"  {result.get('author') or '?'} — {result.get('title') or '?'}")
            except Exception as exc:
                failures += 1
                print(f"  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        # Avoid artificial waits; SDK handles transient API retries.
    if "xls" in operations:
        records = []
        all_cost_records = []
        selected_photos = {ims[0] for _, ims, _ in tasks}
        for box in boxes:
            for images, first_photo in units(box):
                if first_photo not in selected_photos:
                    continue
                advanced = model_or_legacy_path(first_photo, args.model)
                standard = model_or_legacy_path(first_photo, args.below_model) if args.below_model else None
                chosen = advanced or standard
                for dest in existing_json_paths(first_photo):
                    try:
                        data = json.loads(dest.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError("JSON root is not an object")
                        all_cost_records.append((dest, data))
                        if dest == chosen:
                            records.append((dest, data))
                    except Exception as exc:
                        failures += 1
                        print(f"ERROR reading {dest}: {exc}", file=sys.stderr)
        outfile = ROOT / f"offprints_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        export_xlsx(records, outfile, all_cost_records)
        print(f"Wrote {outfile.name}: {len(records)} records")
    print(f"Done; errors: {failures}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
