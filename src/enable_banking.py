#!/usr/bin/env python3
"""Connect a personal NatWest account through Enable Banking and import transactions."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from classifier_service import MODEL_NAME, predict, train_from_database
from import_natwest import Transaction, commit_import


ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / "data" / "local-finance-tracker.sqlite"
STATE_FILE = ROOT / "data" / "enable_banking_state.json"
LOCAL_CALLBACK = ("127.0.0.1", 8766)
API_ROOT = "https://api.enablebanking.com"


class EnableBankingError(RuntimeError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def application_config() -> tuple[str, Path]:
    app_id = os.environ.get("LFT_ENABLE_BANKING_APP_ID", "").strip()
    key_text = os.environ.get("LFT_ENABLE_BANKING_PRIVATE_KEY", "").strip()
    if not app_id or not key_text:
        raise EnableBankingError(
            "Set LFT_ENABLE_BANKING_APP_ID and LFT_ENABLE_BANKING_PRIVATE_KEY. "
            "The second value must be the path to your Enable Banking .pem private key."
        )
    key_path = Path(key_text).expanduser().resolve()
    if not key_path.is_file():
        raise EnableBankingError(f"Private key file not found: {key_path}")
    if os.name == "posix" and stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        raise EnableBankingError(
            "The private key is accessible to other local users. Restrict it with: chmod 600 <private-key.pem>"
        )
    return app_id, key_path


def jwt_for(app_id: str, key_path: Path) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": app_id}, separators=(",", ":")).encode())
    claims = b64url(json.dumps({
        "iss": "enablebanking.com", "aud": "api.enablebanking.com",
        "iat": now, "exp": now + 300,
    }, separators=(",", ":")).encode())
    unsigned = f"{header}.{claims}"
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
            input=unsigned.encode(), capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise EnableBankingError(f"Could not sign Enable Banking request with OpenSSL: {detail}") from exc
    return f"{unsigned}.{b64url(result.stdout)}"


class Client:
    def __init__(self, app_id: str, key_path: Path):
        self.app_id = app_id
        self.key_path = key_path

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            API_ROOT + path, data=body, method=method,
            headers={"Authorization": f"Bearer {jwt_for(self.app_id, self.key_path)}",
                     "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise EnableBankingError(f"Enable Banking returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EnableBankingError(f"Enable Banking request failed: {exc}") from exc


class CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    received = threading.Event()
    expected_state = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        params = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items() if values}
        if not hmac.compare_digest(params.get("state", ""), self.__class__.expected_state):
            self.send_error(403, "Invalid authorisation state")
            return
        self.__class__.result = params
        self.__class__.received.set()
        content = "NatWest authorisation received. You can close this tab and return to local-finance-tracker.".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


def authorize(client: Client, redirect_uri: str) -> None:
    CallbackHandler.result = {}
    CallbackHandler.received.clear()
    state = b64url(os.urandom(24))
    CallbackHandler.expected_state = state
    valid_until = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat(timespec="seconds")
    auth = client.request("POST", "/auth", {
        "access": {"valid_until": valid_until, "transactions": True, "balances": True},
        "aspsp": {"country": "GB", "name": "NatWest"},
        "state": state,
        "redirect_url": redirect_uri,
        "psu_type": "personal",
        "language": "en",
    })
    url = auth.get("url")
    parsed_auth_url = urllib.parse.urlsplit(url or "")
    if (parsed_auth_url.scheme != "https" or parsed_auth_url.hostname != "auth.enablebanking.com"
            or parsed_auth_url.username or parsed_auth_url.password):
        raise EnableBankingError("Authorization response did not include a valid Enable Banking URL")
    server = HTTPServer(LOCAL_CALLBACK, CallbackHandler)
    server.timeout = 1
    print("Opening NatWest authorisation in your browser. Sign in at NatWest and approve read access.")
    print(f"If it does not open, visit: {url}")
    webbrowser.open(url)
    try:
        deadline = datetime.now().timestamp() + 300
        while not CallbackHandler.received.is_set() and datetime.now().timestamp() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = CallbackHandler.result
    if not result:
        raise EnableBankingError(f"No authorisation callback received. Ensure {redirect_uri} is registered in Control Panel and that the HTTPS tunnel is still running.")
    if result.get("state") != state:
        raise EnableBankingError("Authorisation callback state did not match; refusing the response")
    if result.get("error"):
        raise EnableBankingError(f"Bank consent failed: {result.get('error_description', result['error'])}")
    code = result.get("code")
    if not code:
        raise EnableBankingError("Authorisation callback did not contain a code")
    session = client.request("POST", "/sessions", {"code": code})
    account_rows = session.get("accounts", [])
    if not session.get("session_id") or not account_rows:
        raise EnableBankingError("Authorised session returned no accounts")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "session_id": session["session_id"],
        "valid_until": session.get("access", {}).get("valid_until", valid_until),
        "accounts": account_rows,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    state_fd = os.open(STATE_FILE, flags, 0o600)
    try:
        os.fchmod(state_fd, 0o600)
        with os.fdopen(state_fd, "w", encoding="utf-8") as state_file:
            json.dump(state_payload, state_file, indent=2)
    except Exception:
        try:
            os.close(state_fd)
        except OSError:
            pass
        raise
    print(f"Authorised {len(account_rows)} account(s). Session details saved locally to {STATE_FILE}.")
    print("Next run: python src/enable_banking.py sync")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EnableBankingError("No saved Enable Banking session. Run the authorize command first.") from exc
    except json.JSONDecodeError as exc:
        raise EnableBankingError(f"Saved session file is invalid: {STATE_FILE}") from exc


def pounds_to_pence(value: object) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise EnableBankingError(f"Bank returned an invalid transaction amount: {value!r}") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise EnableBankingError(f"Bank returned an invalid GBP precision: {value!r}")
    return int(amount * 100)


def account_number(account: dict) -> str:
    identifiers = [account.get("account_id", {}), *account.get("all_account_ids", [])]
    pending = list(identifiers)
    while pending:
        identifier = pending.pop()
        if isinstance(identifier, dict):
            number = identifier.get("identification", "")
            if str(number).isdigit() and len(str(number)) == 8:
                return str(number)
            pending.extend(identifier.values())
    stable = account.get("identification_hash") or account.get("uid")
    return f"eb:{stable}"


def description_for(row: dict) -> str:
    remittance = row.get("remittance_information") or []
    if isinstance(remittance, str):
        remittance = [remittance]
    parts = [str(part).strip() for part in remittance if str(part).strip()]
    if not parts:
        party = row.get("debtor") if row.get("credit_debit_indicator") == "DBIT" else row.get("creditor")
        if isinstance(party, dict) and party.get("name"):
            parts = [str(party["name"]).strip()]
    if not parts and row.get("note"):
        parts = [str(row["note"]).strip()]
    return " — ".join(parts)[:500] or "Open Banking transaction"


def fetch_transactions(client: Client, account_id: str, start: date) -> list[dict]:
    params = urllib.parse.urlencode({"date_from": start.isoformat(), "date_to": date.today().isoformat()})
    path = f"/accounts/{urllib.parse.quote(account_id, safe='')}/transactions?{params}"
    transactions: list[dict] = []
    while path:
        page = client.request("GET", path)
        transactions.extend(page.get("transactions", []))
        continuation = page.get("continuation_key")
        if continuation:
            path = f"/accounts/{urllib.parse.quote(account_id, safe='')}/transactions?{params}&{urllib.parse.urlencode({'continuation_key': continuation})}"
        else:
            path = ""
    return transactions


def sync(client: Client, database: Path) -> int:
    state = load_state()
    expiry = datetime.fromisoformat(state["valid_until"].replace("Z", "+00:00"))
    if expiry <= datetime.now(timezone.utc):
        raise EnableBankingError("Bank consent has expired. Run authorize again to renew it.")
    rows: list[tuple[Transaction, str, str]] = []
    start = date.today() - timedelta(days=90)
    for account in state["accounts"]:
        account_uid = account.get("uid")
        if not account_uid:
            continue
        account_key = account.get("identification_hash") or account_uid
        account_label = account.get("name") or account.get("product") or "NatWest account"
        number = account_number(account)
        for row in fetch_transactions(client, account_uid, start):
            if row.get("status") != "BOOK":
                continue
            amount = pounds_to_pence(row.get("transaction_amount", {}).get("amount"))
            if row.get("credit_debit_indicator") == "DBIT":
                amount = -abs(amount)
            else:
                amount = abs(amount)
            transaction_date = row.get("booking_date") or row.get("transaction_date") or row.get("value_date")
            if not transaction_date:
                continue
            try:
                date.fromisoformat(transaction_date)
            except ValueError:
                continue
            transaction = Transaction(
                source_row_number=len(rows) + 1,
                transaction_date=transaction_date,
                transaction_type=row.get("bank_transaction_code", {}).get("description") or row.get("credit_debit_indicator", "Unspecified"),
                description=description_for(row), amount_pence=amount,
                balance_pence=(pounds_to_pence(row["balance_after_transaction"]["amount"])
                               if row.get("balance_after_transaction", {}).get("amount") is not None else None),
                account_name=account_label, account_number=number,
            )
            rows.append((transaction, account_key, str(row.get("entry_reference") or "")))

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("""CREATE TABLE IF NOT EXISTS enable_banking_entry_refs (
        account_key TEXT NOT NULL, entry_reference TEXT NOT NULL,
        transaction_id INTEGER NOT NULL REFERENCES transactions(transaction_id),
        PRIMARY KEY(account_key, entry_reference))""")
    known_refs = set(connection.execute("SELECT account_key,entry_reference FROM enable_banking_entry_refs"))
    novel: list[tuple[Transaction, str, str]] = []
    seen_refs: set[tuple[str, str]] = set()
    for item in rows:
        tx, key, ref = item
        ref_key = (key, ref)
        if ref and (ref_key in known_refs or ref_key in seen_refs):
            continue
        if ref:
            seen_refs.add(ref_key)
        novel.append(item)
    if not novel:
        connection.close()
        print("No new booked transactions found.")
        return 0

    # Fall back to the current transaction fingerprint when a bank omits its stable entry reference.
    existing_rows = connection.execute(
        """SELECT a.account_number,t.transaction_date,t.raw_description,t.amount_pence,COUNT(*)
           FROM transactions t JOIN accounts a USING(account_id) GROUP BY 1,2,3,4"""
    ).fetchall()
    existing = {tuple(row[:4]): row[4] for row in existing_rows}
    filtered: list[tuple[Transaction, str, str]] = []
    for item in novel:
        tx = item[0]
        key = (tx.account_number, tx.transaction_date, tx.description, tx.amount_pence)
        if existing.get(key, 0) > 0:
            existing[key] -= 1
        else:
            filtered.append(item)
    if not filtered:
        connection.close()
        print("No new transactions found (all rows already exist in local-finance-tracker).")
        return 0

    transactions = [item[0] for item in filtered]
    digest_data = [{"account": item[1], "ref": item[2], "date": item[0].transaction_date,
                    "description": item[0].description, "amount": item[0].amount_pence}
                   for item in filtered]
    digest = hashlib.sha256(json.dumps(digest_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    pseudo_path = ROOT / "data" / f"enable-banking-{date.today().isoformat()}.json"
    try:
        model = train_from_database(connection)
        category_ids = {row[1]: row[0] for row in connection.execute("SELECT category_id,name FROM categories")}
        suggestions = {}
        for transaction in transactions:
            category, confidence = predict(model, transaction.description, transaction.amount_pence)
            suggestions[transaction.source_row_number] = (category_ids[category], confidence)
    finally:
        connection.close()
    import_id = commit_import(pseudo_path, database, transactions, digest, suggestions, MODEL_NAME)
    with sqlite3.connect(database) as connection:
        for item in filtered:
            ref = item[2]
            if not ref:
                continue
            transaction_id = connection.execute(
                "SELECT transaction_id FROM transactions WHERE import_id=? AND source_row_number=?",
                (import_id, item[0].source_row_number),
            ).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO enable_banking_entry_refs VALUES (?,?,?)",
                (item[1], ref, transaction_id),
            )
    print(f"Imported {len(transactions)} new booked transaction(s) into local-finance-tracker (import {import_id}).")
    print("New transactions appear in the review queue until confirmed, as with CSV imports.")
    return len(transactions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("authorize", "sync"), help="authorize NatWest or fetch and import transactions")
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()
    try:
        app_id, key_path = application_config()
        client = Client(app_id, key_path)
        if args.action == "authorize":
            redirect_uri = os.environ.get("LFT_ENABLE_BANKING_REDIRECT_URL", "").strip()
            parsed_redirect = urllib.parse.urlparse(redirect_uri)
            hostname = (parsed_redirect.hostname or "").lower()
            try:
                redirect_port = parsed_redirect.port
            except ValueError:
                redirect_port = -1
            if (parsed_redirect.scheme != "https" or not hostname.endswith(".trycloudflare.com")
                    or hostname == "trycloudflare.com" or parsed_redirect.username or parsed_redirect.password
                    or redirect_port not in (None, 443) or parsed_redirect.path != "/callback"
                    or parsed_redirect.query or parsed_redirect.fragment):
                raise EnableBankingError(
                    "Use the exact HTTPS Quick Tunnel callback URL ending in /callback, "
                    "with a *.trycloudflare.com hostname and no port, query, or fragment."
                )
            authorize(client, redirect_uri)
        else:
            sync(client, args.database.expanduser().resolve())
        return 0
    except (EnableBankingError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
