# local-finance-tracker project plan

## Goal

Build a local-first personal transaction tracker on a local computer. Bank exports are dropped into a watched folder, imported into SQLite, categorised with transparent rules and a lightweight local classifier, and reviewed through a simple local web UI. The UI provides spending summaries by week, month, year, account, and category.

## Current implementation status

- SQLite schema, NatWest CSV validation/import, file-hash duplicate detection, exact training lookup, local classifier evaluation, manual transaction review, error review, and reviewed-label feedback are implemented.
- `src/web.py` provides a localhost dashboard, transaction browser and reclassification, review queue, import history, model evaluation, holdout error review, and training-data browser.
- `src/watcher.py` polls the top-level `inbox/` every three seconds, waits for a stable file, validates CSVs, filters cross-statement overlap by full row fingerprint and occurrence count, classifies new rows locally, and moves successful files to `archived/` or invalid CSVs to `rejected/`.
- Unique exact training matches are accepted automatically. Other classifier results are stored as suggestions with confidence and remain in the manual review queue. Reports use confirmed categories only.
- Start with `run_tracker.command` (or `./run_tracker.sh` / `run_tracker.bat` / `./.venv/bin/python ./src/web.py`). The watcher is active while this process runs. An optional macOS LaunchAgent installer is included, though macOS TCC privacy restrictions may deny background access if the project lives under protected folders such as `Documents`.
- The first UI pass uses standard-library Python and local CSS, with no cloud assets, JavaScript framework, or hosted model calls.
- Remaining work: validate the overlap filter against multiple consecutive NatWest exports and add PDF support only if needed.

## Principles

- Keep bank files, database, model, and application local to the Mac.
- Treat SQLite as the source of truth; charts and exports are derived views.
- Never let a model edit the database or choose arbitrary spreadsheet cells. Application code validates all values and controls all writes.
- Preserve raw imported values and descriptions for auditability; store normalised merchant/category data separately.
- Make imports idempotent and transactional: a failed or repeated import must not create partial or duplicate records.
- Require review for uncertain classifications, transfers, malformed files, and unexpected format changes.
- Prefer NatWest CSV. PDF support is a later, separately validated input path.
- Keep category corrections and the reason/source for a category so they can become reusable merchant rules.

## Expected input format

NatWest CSV statement exports provide multi-account rows with these columns:

`Date, Type, Description, Value, Balance, Account Name, Account Number`

Expected properties:

- Dates use a form such as `21 Sep 2026`.
- `Value` and `Balance` are signed decimal values.
- Negative `Value` entries are outflows and positive entries are inflows.
- Common transaction types include `POS`, `DPC`, `BAC`, and `D/D` (rows without a `Type` are retained as `Unspecified`).
- Account identity must be retained per transaction; exports can combine multiple accounts.
- Descriptions include merchant/person text mixed with card details, dates, locations, and transfer information.
- Repeated merchants and same-day transactions occur legitimately. Matching must not collapse legitimate repeated transactions.

Validate headers and values on every import and show a useful error if the format changes unexpectedly.

## Labelled training data format

Historical training CSVs use the columns `Description`, `Category`, `Date`, `Value`, and `Account Name`. Rows missing date, amount, or account name remain useful as text-labelled training examples for the classifier, while complete rows can also participate in exact transaction lookup. If multiple historical rows share an identical exact key with conflicting category labels, treat that key as ambiguous and route it to manual review rather than resolving it silently.

Training rows live only in `training_examples`, linked to `training_imports`; they are never inserted into `transactions` and therefore never count in spending reports. Legacy label aliases (such as `Utilites` → `Utilities` and `Eating out` → `Going out`) are normalised on import while preserving `source_category`.

The exact lookup key is transaction date, signed amount in pence, account name, and full description. Text fields are normalised for Unicode compatibility, case, and repeated whitespace. A unique category result assigns that category with source `training_lookup` and does not require model classification. Conflicting labels remain uncategorised for manual review.

## Proposed architecture

1. **Input folders**
   - `data/inbox/`: watched for new exports.
   - `data/processed/`: successfully imported originals, organised by import or date.
   - `data/rejected/`: files that could not be parsed, with an error visible in the UI/log.
   - Keep these folders and the database outside cloud-synced locations unless that is an intentional choice.

2. **Python application**
   - A small importer parses CSV, validates the expected schema, normalises fields, and generates an import preview.
   - A folder watcher queues new files; it should not import directly into the database before validation.
   - A local web UI provides import review, category correction, search, filters, and summaries. Streamlit is a reasonable first implementation; keep import/classification/database logic independent of the UI framework.
   - Run on the Mac and bind the web server to localhost by default.

3. **SQLite database**
   Initial logical tables:
   - `accounts`: internal ID, stable account key derived from account number, display name, and active state. Avoid showing full account numbers unnecessarily in the UI.
   - `transactions`: account ID, transaction date, bank type, raw description, cleaned merchant, signed amount, balance-after, category ID, classification source/confidence, review state, import ID, and source-row identity.
   - `categories`: controlled category list, with a distinction between ordinary spending, income, and transfer categories/flags.
   - `merchant_rules`: normalised match pattern, category, priority, and user-created/system-created provenance.
   - `training_imports` and `training_examples`: historical labelled examples, stored separately from reportable transactions and indexed for exact full-transaction lookup.
   - `imports`: filename, content hash, timestamps, status, row counts, and validation errors.
   - Use database constraints and migrations; store money as integer minor units (pence), not floating-point values.

4. **Classification**
   - Apply explicit user-approved merchant rules first.
   - Train a small local text classifier from reviewed examples for descriptions not covered by rules. Include useful context such as transaction type and cleaned merchant, but do not assume the full raw description is a stable merchant identifier.
   - Return category plus confidence. Below a chosen confidence threshold, leave it for review.
   - Never train on or silently promote an unreviewed prediction. Corrections become labelled examples and may create/update a rule.
   - Keep transfers and income out of normal spending totals by default. Transfer detection should use type/description clues and should be reviewable; do not automatically assume every positive/negative pair is a transfer.
   - Evaluate with chronological holdouts and per-category recall/macro F1. The error reviewer can confirm or correct held-out labels while preserving imported labels for audit.
   - Keep raw descriptions as the model baseline until a reusable local cleaner shows improved evaluation results.

5. **Reporting**
   - Define spending from signed `Value` entries, not balance movement.
   - Default spending reports to outflows only and exclude transfers and income.
   - Support week/month/year views, account and category filters, category totals/splits, and transaction drill-down.
   - Document the chosen week boundary and reporting currency (likely GBP; verify before implementation).

## Import identity and duplicate handling

- Hash the complete source file to detect an exact re-drop of an already imported file.
- Do not use only date, merchant, and amount as a unique key: legitimate repeated transactions occur.
- Prefer a bank-provided transaction identifier if one appears in future exports.
- For the current CSV, build row identity with account key plus normalised source fields and a duplicate occurrence index within that file. Store import ID and source row number as provenance.
- Consider cross-file overlap explicitly: compare exact transaction fingerprints with occurrence counts, use balance and type where helpful, and surface ambiguous matches instead of deleting or merging them silently.
- Commit a validated import atomically. Keep an import summary and allow a safe rollback of that import.

## User workflow

1. Drop a NatWest CSV in `data/inbox/`.
2. The app detects it and parses a preview without committing data.
3. Preview shows account names, date range, row counts, totals, duplicate/overlap warnings, and rows needing review.
4. User confirms; validated records are committed in one SQLite transaction.
5. User reviews uncertain categories and transfer/income classifications.
6. Dashboard updates from SQLite; corrections are retained for future imports.

The first review tool is a local interactive CLI. It applies unique exact training matches first, then presents remaining items with the configured category list. Each confirmed choice updates the transaction and adds a linked row to `training_examples`. Reclassifying that transaction later updates its existing feedback row, avoiding contradictory duplicates. `review_transactions.py --all` also permits correcting already classified transactions.

## Milestones

### 0. Project setup and decisions

- Establish repository/project structure, Python environment, configuration, and local data paths.
- Decide category list, reporting week start, and GBP assumptions.
- Inspect additional exports if available, especially overlap between adjacent downloads and any account naming changes.

### 1. CSV importer and database

- Define SQLite schema and migration approach.
- Parse the observed NatWest CSV format and validate required headers, dates, amounts, account identity, and row counts.
- Implement file hashing, source-row provenance, duplicate/overlap detection, preview, atomic commit, and import summary.
- Preserve original CSV files.
- Import existing labelled history into the separate training tables; never count it in spending reports.
- Apply a unique exact training-data match before any model step; treat conflicting exact labels as review cases.

### 2. Review UI and manual categorisation

- Build local UI for import previews, transaction browsing, category edits, and review queue.
- Implement categories and explicit merchant rules.
- Record every manual correction as training data and rule provenance.

### 3. Reports

- Add week/month/year totals, category splits, account filters, transaction search, and transfer/income exclusions.
- Verify totals against imported rows and make report definitions visible in the UI.

### 4. Lightweight classifier

- Train and evaluate a local classifier using reviewed labels.
- Use confidence thresholds and abstain/review behaviour.
- Compare against a simple merchant-rule baseline before enabling automatic categorisation.
- Save model version and classification source per transaction so results are explainable.

### 5. Watched-folder automation and resilience

- Enable background folder watching and clear notifications/status for success, duplicates, and failures.
- Handle partial file copies by waiting for file stability before parsing.
- Add logs, backups, and documented restore/rollback steps.

### 6. Optional PDF support

- Add only if needed after CSV flow is reliable.
- Detect text-based versus scanned PDFs; use extraction/OCR as needed.
- Require preview and manual confirmation because PDF layouts and extraction can be less dependable than CSV.

## Security and operational requirements

- Store data locally with restrictive filesystem permissions; avoid committing real statements or database contents to version control.
- Do not send transaction data to hosted APIs.
- Bind the UI to localhost unless remote access is deliberately designed and secured.
- Keep secrets out of source/config files; do not log full account numbers or full transaction details unnecessarily.
- Back up the SQLite database and document how to restore it.
- Preserve an auditable link from every transaction back to its import file and source row.

## Agent handoff guidance

If work is delegated to agents later, give each a narrow, non-overlapping deliverable and ask for a short report listing files changed, assumptions, and unresolved questions. Suggested independent tasks:

- **NatWest format research:** inspect additional anonymised CSV/PDF examples and document format variations; read-only, no code changes.
- **Database design review:** review schema, import provenance, duplicate cases, migrations, and rollback semantics; propose changes before implementation.
- **UI/report prototype:** implement or sketch reporting screens against a stable repository interface, without changing importer/schema contracts.
- **Classifier evaluation:** compare rules and lightweight local text classifiers using only reviewed/approved sample data; report per-category errors and abstention thresholds.

The primary project owner should integrate changes and resolve shared-interface decisions. Do not give multiple agents concurrent ownership of the same files or schema.

## Open decisions

- Final category list and whether categories can be nested.
- Whether the data is single-user and whether account numbers should be masked in all screens.
- Week boundary and treatment of refunds in spending reports.
- Whether to use Streamlit or another local UI after a small usability prototype.
- Exact duplicate/overlap policy after examining consecutive NatWest exports.
- Whether any PDF workflow is necessary once CSV import is in place.

## Initial acceptance criteria

- Importing the same CSV twice does not duplicate its transactions.
- Multiple accounts remain distinguishable throughout storage, review, and reporting.
- Repeated legitimate same-day transactions are retained.
- Invalid or changed CSV input is rejected with a useful explanation and no partial commit.
- A user can correct categories and see those corrections persist.
- Weekly, monthly, and yearly category reports reconcile to the filtered transaction rows under clearly stated rules.
- No transaction data is sent off the Mac.
