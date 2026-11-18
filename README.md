# AliExpress Order Export

This project exports AliExpress buyer orders to CSV and can optionally save invoice PDFs. It is built around the AliExpress MTop endpoints observed from real order-list, order-detail, and invoice traffic.

## What it does

- Stores AliExpress auth cookies in `.auth/cookies.json`
- Supports an interactive `--setup` flow that detects installed browsers, lets you pick a browser/profile, imports cookies, and tests the live API connection
- Imports those cookies from either:
  - an installed browser profile via `--setup`
  - a HAR capture
  - a Firefox profile or `cookies.sqlite`
- Calls the order list, order detail, invoice list, and invoice file APIs directly
- Exports a CSV into `exports/` with the selected date range in the filename
- Writes a readable order CSV with key fields plus JSON payload columns for the full exposed data
- Writes a separate order-line CSV with one row per purchased item, including item titles and SKU data
- Can also write an XLSX workbook with an `Orders` sheet and an `Order Lines` sheet, each with embedded thumbnails when the images can be fetched
- Saves invoice PDFs into `exports/pdf/` and skips duplicates
- Supports offline export from HAR captures for debugging or one-off recovery

## Install

The script declares its dependencies in a [PEP 723](https://peps.python.org/pep-0723/) header, so you can run it with `uv run aliexpress_export.py` from the repo root without creating a virtual environment first.

Optional: create a project `.venv` and install the same dependencies for editor tooling (for example Ruff):

```bash
uv venv --python 3.13
uv sync
```

## Usage

Run the guided browser setup once:

```bash
uv run aliexpress_export.py --setup
```

Or, import cookies once from Firefox, then export live:

```bash
uv run aliexpress_export.py \
  --start-date 2026-03-01 \
  --end-date 2026-03-31 \
  --firefox-profile "$HOME/Library/Application Support/Firefox/Profiles/<profile>"
```

Reuse the stored cookies on later runs:

```bash
uv run aliexpress_export.py \
  --start-date 2026-03-01 \
  --end-date 2026-03-31 \
  --download-pdfs
```

Generate the CSVs plus an XLSX workbook with thumbnails:

```bash
uv run aliexpress_export.py \
  --start-date 2026-03-01 \
  --end-date 2026-03-31 \
  --download-pdfs \
  --xlsx
```

Import cookies from a HAR file instead:

```bash
uv run aliexpress_export.py \
  --start-date 2026-03-01 \
  --end-date 2026-03-31 \
  --import-har "/path/to/orders.har"
```

Export directly from HAR capture(s) without making live API calls:

```bash
uv run aliexpress_export.py \
  --start-date 2026-03-01 \
  --end-date 2026-03-31 \
  --input-har "/path/to/list.har" \
  --input-har "/path/to/detail.har" \
  --download-pdfs \
  --xlsx
```

## Notes

- The login step is expected to happen in your own browser, not through Playwright automation.
- `--setup` reads cookies from your normal browser profile. Keep that browser logged into AliExpress while running setup.
- The stored auth artifact is a cookie jar, not a single bearer token. The MTop request signing token is derived from those cookies.
- The main order CSV is row-oriented and keeps the full AliExpress payloads in JSON columns. The separate order-line CSV is the easiest place to inspect product titles.
- The XLSX workbook is optional and keeps thumbnails in a dedicated `Thumbnail` column. If image downloads fail, the workbook is still written without those embedded images.
- Some sessions may expire and require a fresh HAR or Firefox-cookie import.


## Resuming interrupted exports

Live exports automatically save a private checkpoint in the output directory after
each order-list page, completed order detail, invoice lookup and downloaded PDF.
Run the same command again to resume. Use the same date range, output directory,
cookie-file path and locale options. You can increase the page limit or enable
PDF downloads on a subsequent run.

The exporter walks the list again to discover orders without reusing an old
pagination cursor. Successfully checkpointed details and invoice lookups are
reused. Failed requests remain pending. Existing PDF files are checked for their
PDF header and reused. A successful invoice lookup with no invoices is saved as
an empty result.

During a live export, the CSV files are partial views of the checkpoint. Their
presence does not prove that all requested pages, details or invoices completed.
Order and system/authentication failures stop the run with a nonzero exit status
and preserve prior progress. Other invoice failures remain pending while later
orders continue. The run exits nonzero after saving those results, and the next
run retries pending invoice operations.
Reaching the page limit prints an incomplete-coverage warning. XLSX output is
produced after successful processing.

AliExpress token-expired responses trigger one bootstrap and freshly signed
retry, using updated response cookies. A repeated token error or expired login
still stops the export. In that case, sign in and run `--setup` manually, then
rerun the export. The session cookies are saved even after a failed request.

Use a separate output directory for each account. Checkpoints contain private
purchase data and are intended for retries of the same snapshot. To refresh
completed order details/statuses, check for newly available invoices, or change
accounts, start with a new output directory. Concurrent live exports to one
output directory are rejected by an OS file lock. Use a separate cookie file
for independent concurrent collectors.

Checkpoint and PDF replacement is atomic. If interrupted between a checkpoint
write and CSV generation, rerunning regenerates the CSVs from the checkpoint.

Offline recovery tests cover token rotation, bounded retries, interrupted details
and invoice/PDF retrieval, repeated pagination, settings mismatches and interrupted
file replacement. Live token recovery still requires validation against a
user-run export.
