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

ROOT = Path(__file__).resolve().parent
EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
DEFAULT_MODEL = "gpt-5.4-mini"


class ReviewedWork(BaseModel):
    author: str | None
    title: str | None
    year: str | None
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
    publication_date: str | None
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
    return [int(t) if t.isdigit() else t.lower()
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
    if data["year"]:
        data["offprint_year"] = None
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
    "Type", "Publication", "Volume", "Issue", "Year", "Original year",
    "Offprint year", "Pages", "Publication date", "Place", "Publisher",
    "DOI", "Contributors", "Reviewed work", "Handwriting",
    "Handwriting type", "Handwriting text", "Handwriting notes",
    "Needs review", "Review reason", "Model", "Processed at",
]


def export_xlsx(records, outfile):
    # Deferred import: fix/json operations do not require the spreadsheet package.
    from artifact_tool import Workbook, SpreadsheetFile
    wb = Workbook.create()
    sh = wb.worksheets.add("Catalogue")
    rows = [HEADERS]
    summary = Counter()
    for path, item in records:
        b = item.get("box", path.parent.name)
        imgs = item.get("images", [])
        if not isinstance(imgs, list):
            imgs = []
        work = item.get("reviewed_work") or {}
        if not isinstance(work, dict):
            work = {}
        book = "; ".join(str(work[k]) for k in ("author", "title", "year")
                         if work.get(k))
        effective_year = item.get("year") or item.get("offprint_year")
        images = [str(x) for x in imgs[:2]]
        images += [""] * (2 - len(images))
        p = item.get("processing") or {}
        vals = [
            b, images[0], images[1], item.get("author"), item.get("title"),
            item.get("publication_type"), item.get("publication"),
            item.get("volume"), item.get("issue"), effective_year,
            item.get("year"), item.get("offprint_year"), item.get("pages"),
            item.get("publication_date"), item.get("publication_place"),
            item.get("publisher"), item.get("doi"),
            item.get("contributors"), book, item.get("handwriting"),
            item.get("handwriting_type"), item.get("handwriting_text"),
            item.get("handwriting_notes"),
            "Yes" if item.get("needs_review") else "No",
            item.get("review_reason"), p.get("model"), p.get("processed_at")
        ]
        rows.append(["" if v is None else str(v) for v in vals])
        summary[b] += 1
    sh.get_range_by_indexes(0, 0, len(rows), len(HEADERS)).values = rows
    sh.get_range_by_indexes(0, 0, 1, len(HEADERS)).format = {
        "fill": "#23445A",
        "font": {"bold": True, "color": "#FFFFFF"},
        "row_height": 27,
    }
    sh.freeze_panes.freeze_rows(1)
    for i, col in enumerate(HEADERS):
        width = 38 if col in {"Title", "Publication", "Handwriting text", "Review reason"} else 20
        if col == "Box":
            width = 12
        sh.get_range_by_indexes(0, i, len(rows), 1).format.column_width = width
    # Store portable relative filenames, and use Excel HYPERLINK formulas.
    for row_index, (path, item) in enumerate(records, start=2):
        box_dir = path.parent.parent if path.parent.name.lower() == "double" else path.parent
        box = item.get("box", box_dir.name)
        for j, rel in enumerate((item.get("images") or [])[:2]):
            target = f"{box}/{rel}".replace('"', '""')
            label = str(rel).replace('"', '""')
            sh.get_cell(row_index - 1, j + 1).formulas = [[
                f'=HYPERLINK("{target}","{label}")'
            ]]
    summary_sh = wb.worksheets.add("Summary")
    srows = [["Box", "Records"]] + [[k, v] for k, v in sorted(summary.items())]
    srows.append(["TOTAL", len(records)])
    summary_sh.get_range_by_indexes(0, 0, len(srows), 2).values = srows
    summary_sh.get_range("A1:B1").format = {
        "fill": "#23445A", "font": {"bold": True, "color": "#FFFFFF"}
    }
    summary_sh.get_range("A:A").format.column_width = 20
    summary_sh.get_range("B:B").format.column_width = 15
    SpreadsheetFile.export_xlsx(wb).save(str(outfile))


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
    prompt = (ROOT / "prompt.txt").read_text(encoding="utf-8") if client else None
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
