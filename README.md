# local-finance-tracker

local-finance-tracker is a local-first NatWest transaction tracker. It imports NatWest CSV statements into SQLite, suggests categories locally, and provides a browser-based dashboard and review workflow. Statement files, classifications, and reports stay on the machine running the app.

## Current portability

The Python web app uses the standard library plus scikit-learn, and its data paths are relative to the project folder. The web app can therefore be run without the native launcher on other operating systems that support Python and scikit-learn.

The bundled desktop app in `~/Applications` is macOS-only: it uses Swift, AppKit, WebKit, and macOS tools. The browser-based app has launch scripts for macOS, Windows, and Linux. Open Banking setup is separate and currently assumes OpenSSL and Cloudflare Tunnel commands available on the host.

## Run the web app

You need Python 3.10 or newer. The recommended first run is to use the launcher for your operating system from the project folder:

- macOS: double-click `run_tracker.command` in Finder, or run `./run_tracker.command` in Terminal.
- Windows: double-click `run_tracker.bat`.
- Linux: run `./run_tracker.sh` from a terminal. If needed, first allow execution with `chmod +x run_tracker.sh`.

On first launch, the script creates a Python virtual environment, installs the pinned Python dependencies, prepares the local data folders and database, and starts the app. It opens <http://127.0.0.1:8765> in a browser when possible. Keep the launcher window open while using local-finance-tracker; close it or press Control-C to stop the service. Setup is safe to repeat and does not replace an existing database.

The equivalent manual setup is:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python src/setup.py
./.venv/bin/python src/web.py
```

On Windows, use `.venv\Scripts\python.exe` in place of `./.venv/bin/python`.

`src/setup.py` can also be run directly. It creates `data/`, `inbox/`, `archived/`, and `rejected/`; creates a fresh database from the current schema; or applies pending migrations to an existing local-finance-tracker database. It never replaces an existing database. Back up the database before moving between app versions. Older databases without migration history are upgraded only when their schema can be identified safely.

On macOS, the optional native app in `~/Applications` can be rebuilt with `build_macos_app.command`; it requires Xcode command-line tools and macOS 13 or newer. The launch scripts and Python server are the cross-platform route.

## First-run onboarding

If you start `src/web.py` before a database exists, local-finance-tracker shows a local setup page. Choose **Create my database** to initialize an empty database on this computer. The setup page does not upload or import financial information.

After setup, import your own NatWest CSV statements. The initial database includes the standard local-finance-tracker categories. If you have a CSV of previously categorized transactions, you can optionally import it as local training history before importing statements:

```sh
./.venv/bin/python src/import_training.py /path/to/your/categorized-history.csv --commit
```

The training CSV needs the columns `Description`, `Category`, `Date`, `Value`, and `Account Name`. The command previews the file and asks for confirmation before importing. Training data stays local and does not count as reportable spending. Without historical labels, statements can still be imported and reviewed manually; model suggestions become available after at least 20 labelled examples across two or more categories. Each confirmed or corrected transaction adds a manual training example.

The project folder contains private local data after setup. Keep it on a computer and account you trust. Use the backup guidance below before upgrades or when moving to another machine; do not copy a database containing one person's financial history to another user.

## Import NatWest statements

The inbox watcher runs while the server is running. Set your browser's download location to the project's `inbox/` folder, then download NatWest statements as CSV. Alternatively, copy a CSV into that folder. The watcher waits for a file's size and modification time to settle before processing it.

The importer validates the expected NatWest columns and date/value formats, detects exact repeat downloads, and filters rows already present in earlier statements using account, date, type, description, amount, balance, and duplicate occurrence counts. Successfully handled source CSVs move to `archived/`; invalid files move to `rejected/` with an error note. PDF statements are not supported.

## Review, classify, and report

- Unique exact matches against confirmed training examples are assigned automatically. Repeated cleaned merchant descriptions can also match when at least two examples agree at 95% or higher. Date/type details from card transactions and date/reference details from payroll are excluded from these merchant keys; original descriptions remain unchanged.
- Other transactions receive a local text-and-payment-direction model suggestion. Suggestions remain in the review queue until confirmed; confidence is guidance, not a guarantee.
- On a new install, the classifier waits for at least 20 labelled examples across at least two categories. Until then, imports remain available and transactions wait for manual review.
- The review queue groups related pending transactions to support individual decisions and bulk category changes. Groups are based on merchant, payment direction, and, where available, suggested category and statement location.
- Confirming or changing a category saves a manually labelled training example. The model error reviewer uses chronological holdout examples and supports keeping, correcting, or undoing a decision.
- The transaction browser supports search, category and account filters, and category reclassification. The dashboard has week, month, year, and all-time views with category drill-down.
- Reports include confirmed spending only. Unreviewed suggestions do not affect spending totals; income and transfers are excluded. Training examples are kept separate from reportable transactions.
- Import history, model evaluation, and a training-example browser are available from the app's Tools and Imports sections.

## Local data and backups

- Database: `data/local-finance-tracker.sqlite`
- New statements: `inbox/`
- Processed statements: `archived/`
- Rejected statements and parser notes: `rejected/`

These paths are ignored by Git. Back up the SQLite database and statement folders to a secure local or encrypted destination. To make a consistent copy, stop local-finance-tracker first, then copy `data/local-finance-tracker.sqlite` and, if you need the originals, `inbox/` and `archived/`. To restore on a new machine, install the same local-finance-tracker version, run setup once, stop the app, replace its empty `data/local-finance-tracker.sqlite` with your backup, then run the launcher again so pending migrations can be applied. Keep a second backup before restoring or upgrading. Avoid syncing financial data to a shared or public cloud folder. Do not send someone your live database or bank statements with the source code; another person should use their own data.

## Current boundaries

- The watched-folder importer accepts NatWest CSV files. The local UI binds to `127.0.0.1`, not the network.
- The optional Enable Banking CLI retrieves booked NatWest transactions into the same local database. It is experimental, not connected to the UI or a scheduler, and requires the user to configure their own Enable Banking application, private key, and bank consent.
- Model predictions are not silently accepted based on uncalibrated confidence scores.
- The Apple Foundation Models bridge is a separate macOS-only evaluation experiment; it is not required by the app's normal Python runtime.

## Optional NatWest Open Banking import

The experimental Enable Banking connector can retrieve booked NatWest transactions into the local database and review queue. It requires an Enable Banking production application linked to your own account, an HTTPS callback URL forwarded to `127.0.0.1:8766`, and OpenSSL. Production app registration asks for an app description, a data protection email address, and publicly reachable privacy-policy and terms URLs. Keep the Enable Banking private key outside this repository.

Set the application ID and the full path to its private key in your shell environment:

```sh
export LFT_ENABLE_BANKING_APP_ID="your-application-id"
export LFT_ENABLE_BANKING_PRIVATE_KEY="$HOME/path/to/enable-banking-private-key.pem"
chmod 600 "$LFT_ENABLE_BANKING_PRIVATE_KEY"
```

From the project folder, run the first-time bank consent flow, then import transactions:

```sh
export LFT_ENABLE_BANKING_REDIRECT_URL="https://your-tunnel.trycloudflare.com/callback"
./.venv/bin/python src/enable_banking.py authorize
./.venv/bin/python src/enable_banking.py sync
```

Install the tunnel tool once with `brew install cloudflared` on macOS. Before `authorize`, start a Cloudflare Quick Tunnel in another terminal window with `cloudflared tunnel --url http://127.0.0.1:8766`. Register the HTTPS URL it prints, with `/callback` appended, in Enable Banking and set that same URL as `LFT_ENABLE_BANKING_REDIRECT_URL`. Keep the tunnel running until authorisation finishes, then stop it with Control-C; transaction sync calls Enable Banking directly and do not use the tunnel. Quick Tunnels use a temporary hostname, so a newly started tunnel may need to be registered again when you renew consent. Consent/session details are saved under the ignored `data/` folder with owner-only file permissions. `sync` requests the most recent 90 days, imports booked entries only, and avoids repeats using bank entry references where supplied and transaction fingerprints otherwise. New rows enter the normal review queue. Bank background-fetch limits apply; this is intended for a once-daily sync, not real-time updates. Re-run `authorize` when consent expires.

### Publish the Enable Banking information pages

The static pages are in `docs/`. The Pages workflow deploys them when changes reach `main`; first enable Pages in the repository's **Settings → Pages → Build and deployment → Source → GitHub Actions**. After merging this branch to `main` and the workflow succeeds, the expected URLs for this repository are:

- Privacy policy: `https://<your-github-username>.github.io/<repo>/privacy.html`
- Terms of use: `https://<your-github-username>.github.io/<repo>/terms.html`

If Pages is unavailable for this repository's visibility/plan, use a public repository or another static host. GitHub Pages project sites are public. Review the pages and replace any details that become inaccurate before publishing.
