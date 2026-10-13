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

## Selective reprocessing

Newly generated JSON records include `"flagged": false` as the final key inside the `processing` object. To mark an item for reprocessing, manually change this value to the JSON boolean `true` (without quotes).

Run `python offprinter.py --force-flagged` to process photographs with no existing JSON and reprocess **only** existing records with `processing.flagged == true`. Other JSON files are skipped. Reprocessed records are backed up to `.json.bak` and their newly generated `processing.flagged` is reset to `false`.

`--force` (reprocess all) and `--force-flagged` are mutually exclusive. Existing JSON records without the new flag are treated as unflagged. No API request is made for an unflagged existing record.

## Bibliographic verification (separate script)

Run `python verify_offprints.py ../offprints_YYYYMMDD_HHMMSS.xlsx` inside the repository.
This creates a copy beside the original with suffix `_verified.xlsx`, preserving all original worksheets and catalogue fields, and appending **Verification** and **Verification source** columns. The source column contains clickable links. The input workbook and the original JSON files are never modified.

The verifier searches Crossref and OpenAlex for article-level metadata. OpenAlex may link to Hungarian REAL/REAL-J deposits when indexed; it does **not** query REAL directly. Names, journal titles, and titles are fuzzy matched (Unicode/accent normalization, token-order tolerance and edit similarity). A reliable author/title match is mandatory; a mismatching year or volume is never a hard search filter. Conflicting external records are flagged instead of silently reconciled.

High-confidence matching can update the XLSX copy's year, volume, issue or page range; obvious trailing punctuation is cleaned from verified titles. Statuses include `VERIFIED CORRECT`, `VERIFIED (CLEANUP)`, `VERIFIED (CORRECTED)`, `PARTIALLY VERIFIED`, `POSSIBLE MATCH`, `AMBIGUOUS`, `NOT FOUND`, `UNVERIFIED`, `CHECK`, and `SEARCH ERROR`. These describe **external metadata comparison**, not inspection of the original photographs. The original workbook and JSON records are not touched.

An OpenAI API key is read from the same `../.openai_api_key` file used by `offprinter.py`. Ambiguous but plausible candidate comparisons may use GPT-5.4 mini, with reasoning none. Use `--ai never` to disable OpenAI or `--ai always` to check every otherwise acceptable candidate. The script stores query responses in `../.offprinter_verify_cache.json` and reuses them; `--no-cache` disables caching. An optional independent `OPENALEX_API_KEY` environment variable may be necessary for larger workloads.

For initial testing: `python verify_offprints.py ../offprints_YYYYMMDD_HHMMSS.xlsx --limit 10`. The output is a normal catalogue with inline corrections and logs, **not** a separate verification report.

### Direct REAL lookups and match diagnostics

The verifier now also searches the Hungarian Academy's **REAL** EPrints repository directly, using its public search interface and the structured JSON exports of candidate records; this is separate from finding REAL links via OpenAlex. REAL may have gaps, particularly where only REAL-J digitized whole volumes are available. Repository timeouts are treated as a source failure rather than evidence that an article does not exist.

When the external author is absent, the similarity diagnostic reports `author not supplied` rather than counting it as a mismatch. The script still requires a strong article-title match before amending metadata. Low author similarities against unrelated search results are diagnostic of poor candidate retrieval, not proof that our own transcription is wrong.

## Confidence scores (new extractions)

Newly processed JSON records and XLSX exports have four fields after the bibliographic fields: `ocr_confidence`, `numeric_confidence`, `bibliographic_confidence`, and `confidence_composite`. The first three are model judgments on an integer 1–10 scale. OCR measures readability/transcription of printed identifying text; numeric measures visual certainty of year/offprint_year, volume and issue **only** (not pages); bibliographic measures assignment of observed text/numbers to their correct fields. The Python script computes the composite as the arithmetic mean of all available confidence scores, rounded to one decimal; absent scores are excluded, not treated as zero. The XLSX exporter recalculates the composite for existing records. If no relevant numeric value exists, numeric confidence is null; composite confidence averages the available OCR and bibliographic scores instead. The scores do not override `needs_review`. Existing records export blank confidence cells unless reprocessed with `--force` or `--force-flagged`.

## Confidence-based second pass with a stronger model

Use `--force-below SCORE` to selectively reprocess records whose composite confidence (recomputed from available `ocr_confidence`, `numeric_confidence`, and `bibliographic_confidence`) is strictly below SCORE. Example:
`python offprinter.py -b 1 --force-below 7.5 -m gpt-5.4 --reasoning none`.

The threshold is inclusive of fractional scores and must be between 1 and 10. Existing JSON records with no confidence scores are skipped; records without any JSON are processed normally. Reprocessed JSON files are backed up to `.json.bak` (only the last backup is retained) and receive new confidence scores and `flagged=false`. The options `--force`, `--force-flagged`, and `--force-below` are mutually exclusive. Choosing a model does not guarantee that the new score will be higher. For GPT-6.1 Sol, use `--reasoning low` or higher.

The `processing.cost_huf` calculation supports every model in the supported model menu, using the fixed pricing table below.

## Model menu and current pricing

Run `python offprinter.py --model` (or `-m` without a value) in an interactive terminal to choose from a numbered menu. Explicit `-m MODEL_ID` still works for automation. The menu includes: `gpt-5.4-mini`, `gpt-5.4`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-6.1-sol`, and `gpt-6-astra`.

Fixed **standard API prices** as checked 2026-10-13, in USD per million input/output tokens:
- `gpt-5.4-mini`: 0.75 / 4.50
- `gpt-5.4`: 2.50 / 15.00
- `gpt-5.6-terra`: 2.00 / 12.00
- `gpt-5.6-sol`: 4.00 / 20.00
- `gpt-6.1-sol`: 2.00 / 10.00
- `gpt-6-astra`: 10.00 / 50.00

These prices are embedded in `offprinter.py`, not fetched at runtime. Rates also include cached-input discounts (when the API reports cached input). The fixed conversion is **330 HUF/USD**, with `cost_huf` rounded to 1 decimal. The script saves `cached_input_tokens` in new JSON records, and Excel export recalculates cost from stored token counts for supported models, including older entries lacking `cost_huf`. Pricing excludes nonstandard tiers, regional surcharges, special long-context rates and future price changes.

When GPT-6.1 Sol or GPT-6 Astra is selected with the default `--reasoning none`, the script uses `low` reasoning and announces the change, since those models require reasoning. Other listed models retain the default `none`.

**References:** https://developers.openai.com/api/docs/pricing and the corresponding individual model pages.

## Google Gemini 3.8 Flash (alternative provider)

Gemini 3.8 Flash is available with `--model gemini-3.8-flash` or from the interactive `--model` menu. The default remains `gpt-5.4-mini`. Gemini uses the same `prompt.txt`, photographs, Pydantic `Metadata` schema, JSON postprocessing (including name corrections, review flags, confidence scoring, and backups), and XLSX exporter.

Install the added SDK dependency with `pip install -r requirements.txt`. Obtain a **Gemini API key** from Google AI Studio and save it in a new local file `../.gemini_api_key` (alongside `../.openai_api_key`), with no quotes or extra characters. Run `chmod 600 ../.gemini_api_key`. The key is read only for Gemini; OpenAI models continue using `../.openai_api_key`.

Example comparison: `python offprinter.py -b 1 --limit 3 --force -m gemini-3.8-flash --reasoning low`. Be aware that `--force` replaces the original JSON and stores only one `.json.bak` backup; preserve copies before benchmarking multiple models.

Gemini 3.8 Flash supports `low`, `medium`, and `high` thinking, but **not** `none` or `xhigh`. The program converts the existing default `--reasoning none` to `low` with an informational message; explicitly requesting `xhigh` is rejected. The effective reasoning level is saved in `processing.reasoning`.

Gemini usage is saved as `input_tokens`, `output_tokens` (answer plus thinking tokens), and `cached_input_tokens`. The fixed introductory paid API prices through December 31, 2026 are **$0.75 per million input tokens**, **$3.75 per million output/thinking tokens**, and **$0.075 per million cached input tokens**; prices increase starting January 1, 2027. The same fixed conversion of 330 HUF/USD is applied for `processing.cost_huf`. Source: https://ai.google.dev/gemini-api/docs/pricing and https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash .

The Gemini pathway has been implemented against the public SDK documentation but has not yet been exercised with a live API key and offprint photograph; test a small sample before using it on an entire box.

## Model-specific JSON filenames (independent recognition passes)

Each new extraction is saved beside the first image as `PHOTO_STEM.MODEL_ID.json`, e.g. `20261012_121451.gpt-5.4-mini.json` and `20261012_121451.gemini-3.8-flash.json`. For two-photo offprints, both model-specific files are beside the first image in the `double/` directory.

Existing unsuffixed files (`PHOTO_STEM.json`) are **preserved as legacy records**, never implicitly renamed or overwritten. Running another model creates an independent JSON. Ordinary skip and selective reprocessing options (`--force`, `--force-flagged`, `--force-below`) act **only on the selected model's file**; `--force` backs up that model's previous JSON as `.json.bak`. If no file exists for the selected model, its extraction runs normally even if other model or legacy records exist.

XLSX export discovers **all** model-specific and legacy JSON files for each physical offprint and includes one catalogue row per JSON; use the `Model` column to filter or compare independent recognitions. Consequently, total rows in the spreadsheet may exceed the number of physical offprints. The Summary's `Records` count is of extraction records, not distinct physical objects. No merge or adjudication by a third model has been implemented yet.

## Two-tier model escalation and one-row-per-offprint Excel

Use `--below-model STANDARD` with `--model ADVANCED --force-below SCORE` to process **only** those offprints whose STANDARD extraction has a composite confidence strictly below SCORE. Example:

```bash
python offprinter.py -m gpt-5.4 --below-model gpt-5.4-mini --force-below 7.5
```

Standard model records remain unchanged. An already existing advanced-model JSON is preserved (not reprocessed). If the standard model record or its scores are absent, the item is skipped; old unsuffixed mini JSON can serve as the standard baseline. The advanced and standard models must be different. Without `--below-model`, `--force-below` retains its existing model-specific meaning.

**XLSX now always contains at most one row per physical offprint**: choose the `--model` version if available; otherwise choose `--below-model` if supplied and available; otherwise skip that offprint. With `-o xls`, `--below-model` does not require `--force-below`:

```bash
python offprinter.py -o xls -m gpt-5.4 --below-model gpt-5.4-mini
```

The `Model` column identifies the extraction used. The `cost_huf` column is the **selected record's cost**. Summary separates **Selected cost HUF** (displayed records) from **All passes cost HUF** (all JSON variants, including legacy, for the selected physical offprints). Costs for overwritten earlier runs in `.json.bak` are not included.

The earlier statement that XLSX exports one row for every model's JSON is superseded by this new one-row-per-offprint rule.

## Percentile-based confidence selection

`--force-below` now accepts either an absolute score (`--force-below 7`, strictly below 7) or a percent written with `%` (`--force-below '20%'`). For percentages, OFFPRINTER first considers **only the offprints selected by `--box` and `--limit`**, gathers the existing JSON of the source model (`--below-model` if supplied, otherwise `--model`), excludes missing/unscored JSON from the ranking, sorts ascending by composite confidence, and selects `ceil(scored_count × percent / 100)` lowest-scoring items. Ties at the cutoff are resolved deterministically in photograph traversal order; the number selected does not expand for ties.

Example: `python offprinter.py -b 1-5 --limit 100 -m gpt-5.6-terra --below-model gpt-5.4-mini --force-below '20%'`. This identifies the lowest-scoring 20% of **existing scored mini JSONs** among the first 100 eligible photographs in boxes 1–5, and generates Terra JSON only for selected entries that do not already have Terra output. Existing advanced JSONs remain unchanged. Where `--below-model` is omitted, selection uses the chosen model's existing JSON, reprocesses the lowest-scoring entries with that same model, and still processes items with no existing JSON as usual.

Percentile selection does not change XLSX precedence: advanced results when available, standard fallback otherwise. Numeric threshold mode remains unchanged.

## Additional Gemini advanced models

The interactive model menu also offers:
- `gemini-3.1-pro-preview` — confirmed Google Gemini API model ID, **$2 input / $12 output / $0.20 cached input** per million tokens for OFFPRINTER-sized requests (under 200,000 input tokens).
- `gemini-4-argon` — OFFPRINTER selection alias, with the reported introductory **$2 input / $10 output** per million tokens (October 2026). Google's publicly documented API identifier is not yet confirmed. The script searches the models accessible to your Google API key for exactly one Argon model and uses its actual ID. If unavailable or ambiguous, extraction fails explicitly without creating a JSON record. Cached input is conservatively charged at the full $2 rate until an official discounted rate is established. Introductory pricing may change after November 30, 2026.

Both models use `../.gemini_api_key`, the shared Gemini extraction code and the original `prompt.txt`. `--reasoning low`, `medium`, and `high` are accepted; for Argon the precise model capabilities still require a live test. These models have not yet been exercised end-to-end in this repository. Examples:

```bash
python offprinter.py -b 1 --limit 3 -m gemini-3.1-pro-preview --reasoning low
python offprinter.py -b 1 --limit 3 -m gemini-4-argon --reasoning low
```

The pricing constants are fixed estimates; Google's official pricing documentation may change. For Pro Preview, requests larger than 200k tokens have higher rates, which the simple OFFPRINTER pricing table does not model.

## Accurate JSON dry-run preview

Use `--dry-run` with any JSON extraction selection, including a percentile:

```bash
python offprinter.py -m gpt-5.6-terra --below-model gpt-5.4-mini --force-below 20% --dry-run
```

The preview uses the **same selection function** as live extraction: prints `CREATE`, `OVERWRITE` (existing record backed up), `SKIP`, or `ERROR` for each offprint, alongside the destination JSON path and reason (confidence, cutoff, already processed, missing source). A summary counts each action. It does not contact APIs, rewrite JSON, rotate images, or generate XLSX; `fix`/`xls` operations are not simulated.

## Default extraction model (October 2026)

The default model when `-m/--model` is omitted, or Enter is pressed in the interactive model menu, is now **`gemini-3.8-flash`**. Use `-m gpt-5.4-mini` explicitly for the previous default. The default therefore requires `../.gemini_api_key` for JSON extraction.

Legacy unsuffixed `photo.json` files remain associated with **GPT-5.4 mini** when `processing.model` is `gpt-5.4-mini` or missing; changing the default does not relabel them as Gemini Flash. Existing model-specific JSONs are unaffected.
