# OFFPRINTER

Local Python pipeline for cataloguing scholarly offprints from phone photographs. Code lives on GitHub; photos, JSON results, generated XLSX and the API key remain local.

## Installation (Linux)

    git clone https://github.com/pleusicles-code/offprinter.git
    cd offprinter
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Create .openai_api_key in the **parent directory** of the repository (beside the Box folders). The file must contain only your OpenAI API key, without quotes. Run:

    chmod 600 .openai_api_key

Do not commit the key or share it. It is outside the Git repository, so it cannot accidentally be added to a commit from this repository.

## Image layout

    ../Box001/
        first.jpg
        first.json           # written automatically
        next.jpg
        double/
            first-part.jpg
            second-part.jpg
            first-part.json  # written automatically
    ../Box002/
        ...

The script and prompt.txt stay inside the offprinter repository; the API key, Box folders and exported Excel workbooks live one directory above it. Each photograph directly in a box is one offprint. Photographs in double/ are sorted by natural numeric filename order and interpreted in consecutive pairs. An odd number of photographs causes an error.

## Commands

    python offprinter.py                   # all boxes: fix, json, xls
    python offprinter.py -b 3              # Box003 only
    python offprinter.py -b 1-5            # boxes 1 through 5
    python offprinter.py -b 1 -o fix       # image correction only
    python offprinter.py -b 1 -o json      # AI extraction
    python offprinter.py -o xls            # XLSX from existing JSON, no API
    python offprinter.py -b 1 --limit 3    # test first 3 offprints
    python offprinter.py --dry-run         # preview selection/pairing
    python offprinter.py -b 1 --force      # reprocess existing JSON
    python offprinter.py -m gpt-5.4        # alternative model
    python offprinter.py -b 1 --reasoning low  # test low reasoning effort

Operations always run in the order fix → json → xls, with defaults fix,json,xls, gpt-5.4-mini and reasoning effort none. Use --reasoning none|low|medium|high|xhigh to choose the effort level. The selected value is recorded as processing.reasoning in each newly generated JSON.

## Rotation

For EXIF orientation 3, the project-specific rule is to rotate pixels 270 degrees counterclockwise (90 degrees clockwise), reset orientation to 1, and save the photo in place. No photo backup. All other EXIF orientation values are left untouched.

## Results

One validated JSON is stored next to each offprint's first image. When forcing reprocessing, old JSON is backed up as .json.bak, replacing any earlier backup. Existing JSON is skipped by default. API calls send one or two images together for a single offprint.

Only two publication-year fields are used: year is the default year for all publication types; offprint_year is used solely when the original publication year of an offprint cannot be determined and a separate offprint date is visible. Excel keeps these as two distinct columns. Existing JSON files are not automatically rewritten. needs_review refers exclusively to bibliographic identification, never handwritten notes.

XLSX export in the parent directory produces offprints_YYYYMMDD_HHMMSS.xlsx with Catalogue and Summary worksheets and photo hyperlinks. Those hyperlinks use absolute local file paths and may need regeneration if files are moved. XLSX generation does not call the API.

## Security

This is a public repository. Box folders, credentials, and generated workbooks are ignored by Git. Do not override these exclusions when committing data. The API key is read from ../.openai_api_key and is never deliberately placed in JSON or workbook output.

Each newly processed GPT-5.4 mini JSON record includes `processing.cost_huf`, calculated at a fixed 330 HUF/USD using $0.75 per million input tokens and $4.50 per million output tokens. This is an estimate based on reported total tokens and does not apply alternative model prices or cached-input discounts; if another model is selected, `cost_huf` is null. Existing JSON files are not retroactively updated.\n\nOpenAI API usage is billed separately from ChatGPT subscriptions.

## Bibliographic fields and Excel report

Publication type is no longer extracted. Printed author names remain in `author`, while newly processed records also include `author_normalized` in the form "Surname, Given". This new field is appended to the Excel catalogue, along with `cost_huf`, without reordering existing columns (apart from removing the obsolete Type column).

The script clears `pages` unless the model returns an explicit page range with two endpoints. This rule also applies when exporting older JSON files to Excel, without changing those JSON files.

The Summary worksheet includes single/double-photo counts, review counts, handwriting counts, missing-date counts, and estimated HUF costs for each box and for the collection. Costs of older GPT-5.4 mini records can be recomputed from saved input/output tokens during Excel export.

To populate author_normalized in existing JSON files, reprocess with `--force`; regenerating XLSX alone cannot infer missing author normalization from old records.
