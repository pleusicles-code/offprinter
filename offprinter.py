#!/usr/bin/env python3
"""OFFPRINTER: rotate, catalogue and export scholarly offprints."""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import json
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


class ReviewedWork(BaseModel):
    author: str | None
    title: str | None
    publication_place: str | None
    publisher: str | None


class Metadata(BaseModel):
    author: str | None
    title: str | None
    publication_type: str | None
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
    singles = [([p], p.with_suffix(".json")) for p in photos(box)]
    doubles = box / "double"
    pairs = []
    if doubles.is_dir():
        images = photos(doubles)
        if len(images) % 2:
            raise ValueError(f"{doubles}: odd number of photos ({len(images)})")
        pairs = [([images[i], images[i + 1]], images[i].with_suffix(".json"))
                 for i in range(0, len(images), 2)]
    return singles + pairs


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


def load_key():
    path = ROOT / ".openai_api_key"
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


def extract(client, model, prompt, box, images):
    content = [{"type": "input_text", "text": prompt}]
    content.extend(image_content(p) for p in images)
    response = client.responses.parse(
        model=model,
        reasoning={"effort": "none"},
        input=[{"role": "user", "content": content}],
        text_format=Metadata,
        store=False,
    )
    if response.output_parsed is None:
        raise RuntimeError("Model did not return a structured bibliographic record")
    data = response.output_parsed.model_dump()
    if data["handwriting"] not in {"Yes", "No", "Uncertain"}:
        raise ValueError("Invalid handwriting status")
    for field in ("volume", "issue"):
        data[field] = normalize_number_field(data[field])
    if data["publication_type"]:
        data["publication_type"] = data["publication_type"].strip().lower()
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
    data["processing"] = {
        "model": model,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "input_tokens": response.usage.input_tokens if response.usage else None,
        "output_tokens": response.usage.output_tokens if response.usage else None,
    }
    return data


HEADERS = [
    "Box", "Photograph 1", "Photograph 2", "Author", "Title",
    "Type", "Publication", "Volume", "Issue", "Year",
    "Offprint year", "Pages", "Place", "Publisher",
    "DOI", "Contributors", "Reviewed work", "Handwriting",
    "Handwriting type", "Handwriting text", "Handwriting notes",
    "Needs review", "Review reason", "Model", "Processed at",
]


def export_xlsx(records, outfile):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    sh = wb.active
    sh.title = "Catalogue"
    sh.append(HEADERS)
    summary = Counter()
    for path, item in records:
        box = item.get("box") or (path.parent.parent.name if path.parent.name == "double" else path.parent.name)
        images = item.get("images") or []
        work = item.get("reviewed_work") or {}
        if not isinstance(work, dict):
            work = {}
        book = "; ".join(str(work[k]) for k in ("author", "title") if work.get(k))
        usage = item.get("processing") or {}
        row = [
            box, images[0] if images else None, images[1] if len(images) > 1 else None,
            item.get("author"), item.get("title"), item.get("publication_type"),
            item.get("publication"), item.get("volume"), item.get("issue"),
            item.get("year"), item.get("offprint_year"), item.get("pages"),
            item.get("publication_place"), item.get("publisher"), item.get("doi"),
            item.get("contributors"), book, item.get("handwriting"),
            item.get("handwriting_type"), item.get("handwriting_text"),
            item.get("handwriting_notes"), "Yes" if item.get("needs_review") else "No",
            item.get("review_reason"), usage.get("model"), usage.get("processed_at"),
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

    stats = wb.create_sheet("Summary")
    stats.append(["Box", "Records"])
    for box, count in sorted(summary.items()):
        stats.append([box, count])
    stats.append(["TOTAL", len(records)])
    for cell in stats[1]:
        cell.fill = PatternFill("solid", fgColor="23445A")
        cell.font = Font(bold=True, color="FFFFFF")
    stats.column_dimensions["A"].width = 19
    stats.column_dimensions["B"].width = 15
    stats.freeze_panes = "A2"
    wb.save(outfile)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-b", "--box", help="box number or inclusive range (e.g. 3 or 1-5)")
    ap.add_argument("-o", "--operation", default="fix,json,xls",
                    help="comma-separated operations: fix,json,xls")
    ap.add_argument("-f", "--force", action="store_true", help="reprocess and back up prior JSON")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, help="maximum offprints to process (testing)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    operations = {x.strip().lower() for x in args.operation.split(",")}
    if not operations or operations - {"fix", "json", "xls"}:
        ap.error("operations must be fix,json,xls in any combination")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")
    boxes = selected_boxes(ROOT, args.box)
    if not boxes:
        ap.error("No BoxXXX folders found beside offprinter.py")
    tasks = [(box, ims, dest) for box in boxes for ims, dest in units(box)]
    if args.limit:
        tasks = tasks[:args.limit]
    print(f"Selected {len(boxes)} boxes, {len(tasks)} offprints; operations: {','.join(sorted(operations))}")
    if args.dry_run:
        for box, ims, dest in tasks:
            print(f"{dest.relative_to(ROOT)} <= {', '.join(p.name for p in ims)}")
        return
    client = OpenAI(api_key=load_key(), max_retries=3, timeout=90) if "json" in operations else None
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
            if dest.exists() and not args.force:
                print("  JSON exists; skipped")
                continue
            try:
                result = extract(client, args.model, prompt, box, images)
                write_json_atomic(dest, result, backup=args.force)
                print(f"  {result.get('author') or '?'} — {result.get('title') or '?'}")
            except Exception as exc:
                failures += 1
                print(f"  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        # Avoid artificial waits; SDK handles transient API retries.
    if "xls" in operations:
        records = []
        for box in boxes:
            for images, dest in units(box):
                if args.limit and (box, images, dest) not in tasks:
                    continue
                if dest.exists():
                    try:
                        data = json.loads(dest.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError("JSON root is not an object")
                        records.append((dest, data))
                    except Exception as exc:
                        failures += 1
                        print(f"ERROR reading {dest}: {exc}", file=sys.stderr)
        outfile = ROOT / f"offprints_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        export_xlsx(records, outfile)
        print(f"Wrote {outfile.name}: {len(records)} records")
    print(f"Done; errors: {failures}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
