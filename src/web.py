#!/usr/bin/env python3
"""Run local-finance-tracker's local dashboard and watched-folder service."""

from __future__ import annotations

import hashlib
import gzip
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from functools import lru_cache
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from classifier_service import MODEL_NAME, TrainingDataUnavailableError, predict_many, train_from_database
from description_cleaner import clean_description
from import_natwest import apply_merchant_training_matches
from review_model_errors import classify_holdout, record_label, undo_label
from review_transactions import add_or_update_training_example, apply_exact_training_matches, normalise, save_choice
from watcher import ACTIVITY, ARCHIVED, INBOX, REJECTED, scan_once, start_watcher


ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / "data" / "local-finance-tracker.sqlite"
PORT = int(os.environ.get("LFT_PORT", "8765"))
PAGE_SIZE = 60
CATEGORY_COLORS = {
    "groceries": "#9bd49a",
    "utilities": "#84b6a5",
    "rent": "#d0ae75",
    "transport": "#84aecd",
    "education": "#c19ad9",
    "going out": "#df9a78",
    "retail": "#c7a078",
    "activities": "#9eadd8",
    "medical": "#d89191",
    "holiday": "#8fc9c3",
    "miscellaneous": "#9eaca1",
    "income": "#9bd49a",
    "transfer": "#788b80",
}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(pence: int | None) -> str:
    return f"£{(pence or 0) / 100:,.2f}"


def money_number(pence: int | None) -> str:
    return f"{(pence or 0) / 100:,.2f}"


def category_color(name: str) -> str:
    return CATEGORY_COLORS.get(name.casefold(), "#9eaca1")


def category_tag(name: str) -> str:
    return f'<span class="category-tag" style="--category-color:{category_color(name)}">{esc(name)}</span>'


def category_options_html(
    category_rows: list[sqlite3.Row], selected_id: int | None = None,
    placeholder: str = "Choose category", *, empty_option: str | None = None,
) -> str:
    """Render shared category options with the correct color metadata."""
    has_selection = any(row["category_id"] == selected_id for row in category_rows)
    options = []
    if empty_option is not None:
        options.append(
            f'<option value="" {"selected" if not has_selection else ""}>{esc(empty_option)}</option>'
        )
    elif not has_selection:
        options.append(f'<option value="" disabled selected>{esc(placeholder)}</option>')
    options.extend(
        f'<option data-color="{category_color(row["name"])}" value="{row["category_id"]}" '
        f'{"selected" if has_selection and row["category_id"] == selected_id else ""}>'
        f'{esc(row["name"])}</option>'
        for row in category_rows
    )
    return "".join(options)


def category_select_html(
    category_rows: list[sqlite3.Row], selected_id: int | None = None, *,
    aria_label: str = "Category", attributes: str = "", required: bool = False,
) -> str:
    """Render a category picker with the app-wide shared styling hook."""
    required_attribute = " required" if required else ""
    return (
        f'<select class="category-select" name="category_id" aria-label="{esc(aria_label)}" '
        f'{attributes}{required_attribute}>'
        f'{category_options_html(category_rows, selected_id)}</select>'
    )


def category_edit_form(
    transaction_id: int, selector: str, return_to: str, *, button_class: str = "button"
) -> str:
    """Render the standard inline transaction category editor used across tables."""
    return (
        f'<form class="inline-edit" data-category-form data-transaction-id="{transaction_id}" '
        f'method="post" action="/categorize"><input type="hidden" name="transaction_id" '
        f'value="{transaction_id}"><input type="hidden" name="return_to" value="{esc(return_to)}">'
        f'{selector}<button class="{esc(button_class)}" type="submit">save</button>'
        '<small class="save-state" aria-live="polite"></small></form>'
    )


def review_group_key(description: str, amount_pence: int | None) -> tuple[str, int]:
    """Use the model's merchant cleanup plus payment direction as the review group key."""
    merchant = clean_description(description)
    direction = -1 if (amount_pence or 0) < 0 else 1 if (amount_pence or 0) > 0 else 0
    return normalise(merchant or description), direction


def review_group_title(description: str) -> str:
    return clean_description(description).title() or description


@lru_cache(maxsize=4096)
def review_location(description: str) -> str:
    """Extract the trailing place text commonly found in card statement descriptions."""
    if not re.match(r"^\s*\d{3,6}\s+\d{1,2}[A-Z]{3}\d{2}", description or "", flags=re.IGNORECASE):
        return ""
    parts = [part.strip() for part in (description or "").split(",") if part.strip()]
    if len(parts) < 2:
        return ""
    place = re.sub(r"\b(?:GB|UK|US|IE)\b", " ", parts[-1], flags=re.IGNORECASE)
    place = re.sub(r"\b\d+\b", " ", place)
    words = [word for word in re.findall(r"[A-Za-z]{3,}", place.casefold())
             if word not in {"online", "mobile", "internet", "telephone", "payment", "purchase", "contactless", "card"}]
    return " ".join(words)


def review_grouping_key(row: sqlite3.Row) -> tuple[object, ...]:
    """Strict baseline: cleaned merchant, model category, and payment direction match."""
    direction = -1 if (row["amount_pence"] or 0) < 0 else 1 if (row["amount_pence"] or 0) > 0 else 0
    merchant = normalise(clean_description(row["raw_description"]) or row["raw_description"])
    return ("merchant", merchant, direction, row["category_id"])


_MERCHANT_NOISE = {"the", "ltd", "limited", "plc", "llp", "inc", "uk", "gb", "store", "stores", "market", "markets", "mkts", "s"}
_review_page_cache_lock = threading.Lock()
_review_page_cache_signature: tuple[int, int, str] | None = None
_review_page_cache_html = ""


@lru_cache(maxsize=4096)
def _merchant_features(description: str) -> tuple[str, frozenset[str]]:
    tokens = tuple(
        token for token in re.findall(r"[a-z0-9]+", clean_description(description).casefold())
        if token not in _MERCHANT_NOISE and not re.search(r"\d{3,}", token)
    )
    return " ".join(tokens), frozenset(tokens)


@lru_cache(maxsize=32768)
def _merchant_feature_similarity(
    left_name: str, left_tokens: tuple[str, ...], right_name: str, right_tokens: tuple[str, ...]
) -> float:
    if not left_name or not right_name:
        return 0.0
    left_set, right_set = set(left_tokens), set(right_tokens)
    token_overlap = len(left_set & right_set) / len(left_set | right_set)
    matcher = SequenceMatcher(None, left_name, right_name, autojunk=False)
    # quick_ratio is an upper bound for ratio(). Skip the more expensive matching
    # blocks calculation whenever the pair cannot reach even the lower threshold.
    if 0.7 * matcher.quick_ratio() + 0.3 * token_overlap < 0.55:
        return 0.0
    character_similarity = matcher.ratio()
    return 0.7 * character_similarity + 0.3 * token_overlap


def review_merchant_similarity(left: str, right: str) -> float:
    """Compare cleaned business names, discounting common legal and outlet suffixes."""
    left_name, left_set = _merchant_features(left)
    right_name, right_set = _merchant_features(right)
    left_key = (left_name, tuple(sorted(left_set)))
    right_key = (right_name, tuple(sorted(right_set)))
    if right_key < left_key:
        left_name, left_key, right_name, right_key = right_name, right_key, left_name, left_key
    return _merchant_feature_similarity(left_name, left_key[1], right_name, right_key[1])


def review_group_compatible(rows: list[sqlite3.Row]) -> bool:
    """Validate a group by category, payment direction, and merchant-name similarity."""
    if not rows:
        return False
    exact_keys = {review_grouping_key(row) for row in rows}
    if len(exact_keys) == 1:
        return True
    categories = {row["category_id"] for row in rows}
    directions = {-1 if (row["amount_pence"] or 0) < 0 else 1 if (row["amount_pence"] or 0) > 0 else 0 for row in rows}
    if len(categories) != 1 or None in categories or len(directions) != 1:
        return False
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if not review_pair_compatible(left, right):
                return False
    return True


def review_pair_compatible(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    if left["category_id"] is None or left["category_id"] != right["category_id"]:
        return False
    left_direction = -1 if (left["amount_pence"] or 0) < 0 else 1 if (left["amount_pence"] or 0) > 0 else 0
    right_direction = -1 if (right["amount_pence"] or 0) < 0 else 1 if (right["amount_pence"] or 0) > 0 else 0
    if left_direction != right_direction:
        return False
    left_description = left["raw_description"]
    right_description = right["raw_description"]
    left_location = review_location(left_description)
    same_location = bool(left_location and left_location == review_location(right_description))
    threshold = 0.55 if same_location else 0.72
    return review_merchant_similarity(left_description, right_description) >= threshold


def review_merchant_groups_compatible(left: list[sqlite3.Row], right: list[sqlite3.Row]) -> bool:
    """Compare merchant clusters conservatively when each exact merchant spans places."""
    if not left or not right:
        return False
    left_description = left[0]["raw_description"]
    right_description = right[0]["raw_description"]
    left_location = review_location(left_description)
    same_location = (
        bool(left_location)
        and all(review_location(row["raw_description"]) == left_location for row in left)
        and all(review_location(row["raw_description"]) == left_location for row in right)
    )
    threshold = 0.55 if same_location else 0.72
    return review_merchant_similarity(left_description, right_description) >= threshold


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def categories(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT category_id,name,kind FROM categories WHERE name<>'Uncategorised' ORDER BY kind,name").fetchall()


def shell(title: str, active: str, body: str, notice: str = "") -> str:
    nav = [("/", "Overview", "home"), ("/transactions", "Transactions", "transactions"),
           ("/review", "Review", "review"), ("/imports", "Imports", "imports"),
           ("/tools", "Tools", "tools")]
    links = "".join(f'<a class="navlink {"active" if key == active else ""}" href="{url}">{label}</a>' for url,label,key in nav)
    notice_html = f'<div class="notice" role="status" data-auto-dismiss>{esc(notice)}</div>' if notice else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · local-finance-tracker</title><link rel="stylesheet" href="/style.css"><script src="/app.js" defer></script></head><body>
      <header class="mast"><a class="brand" href="/">local-finance-tracker<span>/</span></a><nav>{links}</nav><span class="local"><i></i>local</span></header>
      <main class="main">{notice_html}{body}<footer>{date.today().isoformat()} · sqlite · local</footer></main>
      </body></html>"""


def query_period(period: str, anchor: date) -> tuple[str, str] | None:
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
    elif period == "year":
        start, end = date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    elif period == "all":
        return None
    else:
        start = anchor.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def clamp_period_anchor(period: str, anchor: date, today: date | None = None) -> date:
    """Keep date navigation within the period containing today, including its future days."""
    current_bounds = query_period(period, today or date.today())
    if current_bounds is None:
        return anchor
    latest_allowed = date.fromisoformat(current_bounds[1])
    return min(anchor, latest_allowed)


def dashboard(period: str = "month", anchor_text: str = "") -> str:
    if period not in {"week", "month", "year", "all"}:
        period = "month"
    try:
        anchor = date.fromisoformat(anchor_text) if anchor_text else date.today()
    except ValueError:
        anchor = date.today()
    today = date.today()
    anchor = clamp_period_anchor(period, anchor, today)
    with db() as connection:
        bounds = query_period(period, anchor)
        date_clause, date_params = ("", []) if bounds is None else (" AND t.transaction_date BETWEEN ? AND ?", list(bounds))
        stats = connection.execute(
            f"""SELECT COUNT(*) AS total,
                SUM(CASE WHEN t.review_required=1 OR t.category_id IS NULL THEN 1 ELSE 0 END) AS pending,
                COALESCE(SUM(CASE WHEN c.kind='spending' AND t.amount_pence<0 AND t.review_required=0 THEN -t.amount_pence ELSE 0 END),0) AS spend
               FROM transactions t LEFT JOIN categories c ON c.category_id=t.category_id
               WHERE 1=1 {date_clause}""", date_params
        ).fetchone()
        breakdown = connection.execute(
            f"""SELECT c.category_id,c.name,COALESCE(SUM(-t.amount_pence),0) AS total,COUNT(*) AS n
                FROM transactions t JOIN categories c ON c.category_id=t.category_id
                WHERE c.kind='spending' AND t.amount_pence<0 AND t.review_required=0 {date_clause}
                GROUP BY c.category_id ORDER BY total DESC""", date_params
        ).fetchall()
        recent = connection.execute(
            f"""SELECT t.transaction_id,t.transaction_date,t.raw_description,t.amount_pence,t.review_required,
                      t.category_source,c.name AS category,a.account_name
               FROM transactions t LEFT JOIN categories c ON c.category_id=t.category_id
               JOIN accounts a ON a.account_id=t.account_id WHERE 1=1 {date_clause}
               ORDER BY t.transaction_date DESC,t.transaction_id DESC LIMIT 8""", date_params
        ).fetchall()
        imports = connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    max_total = max([row["total"] for row in breakdown], default=1) or 1
    bars = "".join(
        f'''<details class="category-detail" data-category-details data-category-id="{row["category_id"]}"
             data-period="{period}" data-date="{anchor.isoformat()}" style="--category-color:{category_color(row["name"])}">
          <summary><span class="category-name"><i></i>{esc(row["name"])}</span>
            <span class="bartrack"><span class="barfill" style="width:{max(3,round(100*row["total"]/max_total))}%"></span></span>
            <strong class="barvalue">{money(row["total"])}</strong>
            <small>{row["n"]} transaction{"s" if row["n"] != 1 else ""}</small>
          </summary><div class="category-transactions" data-category-transactions data-loaded="false"><small>Open to view transactions</small></div>
        </details>'''
        for row in breakdown
    ) or '<p class="empty">No confirmed spending in this period yet.</p>'
    periods = "".join(f'<a class="seg {"selected" if p==period else ""}" href="/?period={p}&amp;date={anchor.isoformat()}">{label}</a>' for p,label in [("week","Week"),("month","Month"),("year","Year"),("all","All")])
    if bounds:
        start_date, end_date = date.fromisoformat(bounds[0]), date.fromisoformat(bounds[1])
        if period == "week":
            range_label = f"{start_date:%d %b} – {end_date:%d %b %Y}"
        elif period == "month":
            range_label = start_date.strftime("%B %Y")
        else:
            range_label = str(start_date.year)
        previous_link = f'<a class="range-arrow" aria-label="Previous period" href="/?period={period}&amp;date={(start_date-timedelta(days=1)).isoformat()}">‹</a>'
        current_period_start = date.fromisoformat(query_period(period, today)[0])
        period_ceiling = query_period(period, today)[1]
        next_link = (
            f'<a class="range-arrow" aria-label="Next period" href="/?period={period}&amp;date={(end_date+timedelta(days=1)).isoformat()}">›</a>'
            if end_date < current_period_start else ""
        )
    else:
        range_label, previous_link, next_link = "all dates", "", ""
    date_form = "" if period == "all" else f'<form method="get" action="/"><input type="hidden" name="period" value="{period}"><input type="date" name="date" value="{anchor.isoformat()}" max="{period_ceiling}" aria-label="Choose a date"><button type="submit">go</button></form>'
    tr = "".join(f'<tr><td>{esc(r["transaction_date"])}</td><td class="merchant">{esc(r["raw_description"])}</td><td>{esc(r["account_name"])}</td><td>{category_tag(r["category"] or "Uncategorised")}</td><td class="amount {"negative" if r["amount_pence"]<0 else "positive"}">{money_number(r["amount_pence"])}</td></tr>' for r in recent)
    body = f"""<div class="page-head"><h1>Overview</h1><div class="range-tools"><div class="segmented">{periods}</div><div class="range-picker">{previous_link}<strong>{range_label}</strong>{next_link}{date_form}</div></div></div>
      <section class="stats overview-stats"><div class="total-spending"><small>Total spending · {esc(range_label)}</small><strong>{money(stats["spend"])}</strong></div><div><small>to review</small><strong><a href="/review">{stats["pending"] or 0}</a></strong></div></section>
      <section class="panel"><div class="panel-head"><h2>Categories</h2><a href="/transactions">all transactions →</a></div>{bars}</section>
      <section class="panel"><div class="panel-head"><h2>Recent</h2><a href="/transactions">browse →</a></div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Description</th><th>Account</th><th>Category</th><th class="right">Amount (£)</th></tr></thead><tbody>{tr or '<tr><td colspan="5" class="empty">No transactions</td></tr>'}</tbody></table></div></section>"""
    return shell("Overview", "home", body)


def category_transactions_fragment(category_id: int, period: str, anchor_text: str) -> str:
    if period not in {"week", "month", "year", "all"}:
        period = "month"
    try:
        anchor = date.fromisoformat(anchor_text) if anchor_text else date.today()
    except ValueError:
        anchor = date.today()
    anchor = clamp_period_anchor(period, anchor)
    bounds = query_period(period, anchor)
    date_clause, date_params = ("", []) if bounds is None else (" AND t.transaction_date BETWEEN ? AND ?", list(bounds))
    with db() as connection:
        rows = connection.execute(
            f"""SELECT t.transaction_date,t.raw_description,t.amount_pence,a.account_name
                FROM transactions t JOIN categories c ON c.category_id=t.category_id
                JOIN accounts a ON a.account_id=t.account_id
                WHERE c.category_id=? AND c.kind='spending' AND t.amount_pence<0
                  AND t.review_required=0 {date_clause}
                ORDER BY t.transaction_date DESC,t.transaction_id DESC""",
            [category_id, *date_params],
        ).fetchall()
    if not rows:
        return '<p class="empty category-empty">No confirmed transactions for this category in the selected period.</p>'
    transactions = "".join(
        f'<tr><td>{esc(row["transaction_date"])}</td><td class="merchant">{esc(row["raw_description"])}</td>'
        f'<td>{esc(row["account_name"])}</td><td class="amount negative">{money_number(row["amount_pence"])}</td></tr>'
        for row in rows
    )
    return f'<div class="table-wrap category-table"><table aria-label="Transactions in category"><tbody>{transactions}</tbody></table></div>'


def _optional_int(raw: str) -> int | None:
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def transaction_page(params: dict[str, list[str]], notice: str = "") -> str:
    search = (params.get("q") or [""])[0].strip()
    category_id = _optional_int((params.get("category") or [""])[0].strip())
    account_id = _optional_int((params.get("account") or [""])[0].strip())
    parsed_page = _optional_int((params.get("page") or ["1"])[0].strip())
    page = max(1, parsed_page if parsed_page is not None else 1)
    where, values = [], []
    if search:
        where.append("(t.raw_description LIKE ? OR a.account_name LIKE ?)")
        values.extend([f"%{search}%", f"%{search}%"])
    if category_id is not None:
        where.append("t.category_id=?"); values.append(category_id)
    if account_id is not None:
        where.append("t.account_id=?"); values.append(account_id)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with db() as connection:
        cats, accounts = categories(connection), connection.execute("SELECT account_id,account_name FROM accounts ORDER BY account_name").fetchall()
        total = connection.execute(f"SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.account_id=t.account_id{clause}", values).fetchone()[0]
        rows = connection.execute(
            f"""SELECT t.transaction_id,t.transaction_date,t.raw_description,t.amount_pence,t.category_id,t.review_required,
                       t.category_source,c.name AS category,a.account_name
                FROM transactions t LEFT JOIN categories c ON c.category_id=t.category_id
                JOIN accounts a ON a.account_id=t.account_id {clause}
                ORDER BY t.transaction_date DESC,t.transaction_id DESC LIMIT ? OFFSET ?""",
            values + [PAGE_SIZE, (page-1)*PAGE_SIZE],
        ).fetchall()
    cat_options = category_options_html(
        cats, category_id, empty_option="All categories"
    )
    acc_options = '<option value="">All accounts</option>' + "".join(f'<option value="{a["account_id"]}" {"selected" if account_id==a["account_id"] else ""}>{esc(a["account_name"])}</option>' for a in accounts)
    return_params = {k: v[0] for k, v in params.items() if k in {"q", "category", "account", "page"} and v and v[0]}
    return_to = "/transactions" + ("?" + urlencode(return_params) if return_params else "")
    rows_html = []
    for r in rows:
        state = "Needs review" if r["review_required"] or not r["category"] else r["category_source"].replace("_", " ")
        selector = category_select_html(cats, r["category_id"], attributes="data-category-select", required=True)
        editor = category_edit_form(r["transaction_id"], selector, return_to, button_class="tiny-button")
        rows_html.append(f'''<tr data-transaction-row="{r["transaction_id"]}"><td>{esc(r["transaction_date"])}</td><td class="merchant">{esc(r["raw_description"])}</td><td>{esc(r["account_name"])}</td><td class="tx-status"><span class="pill {"pill-warn" if r["review_required"] else ""}">{esc(state)}</span></td><td class="amount {"negative" if r["amount_pence"]<0 else "positive"}">{money_number(r["amount_pence"])}</td><td>{editor}</td></tr>''')
    pages = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
    base = {k:v[0] for k,v in params.items() if k in {"q","category","account"} and v and v[0]}
    previous = f'<a class="button button-quiet" href="/transactions?{urlencode(base|{"page":page-1})}">← Previous</a>' if page>1 else ""
    next_link = f'<a class="button button-quiet" href="/transactions?{urlencode(base|{"page":page+1})}">Next →</a>' if page<pages else ""
    body = f'''<div class="page-head"><h1>Transactions <small>/{total}</small></h1><form method="post" action="/reclassify"><button class="button" type="submit">reclassify pending</button></form></div>
      <section class="panel filter-panel"><form class="filters" method="get"><input name="q" value="{esc(search)}" placeholder="search" aria-label="Search transactions"><select class="category-select" name="category" aria-label="Filter by category">{cat_options}</select><select name="account" aria-label="Filter by account">{acc_options}</select><button class="button" type="submit">filter</button><a class="button" href="/transactions">reset</a></form></section>
      <section class="panel"><div class="table-wrap"><table class="wide"><thead><tr><th>Date</th><th>Description</th><th>Account</th><th>Status</th><th class="right">Amount (£)</th><th>Category</th></tr></thead><tbody>{''.join(rows_html) or '<tr><td colspan="6" class="empty">No results</td></tr>'}</tbody></table></div><div class="pager"><span>{page}/{pages}</span>{previous}{next_link}</div></section>'''
    return shell("Transactions", "transactions", body, notice)


def review_page(notice: str = "") -> str:
    with db() as connection:
        cats = categories(connection)
        historical_labels: dict[tuple[str, int], dict[int, int]] = {}
        for example in connection.execute(
            "SELECT description_key,amount_pence,category_id FROM training_examples WHERE label_origin='manual'"
        ):
            key = review_group_key(example["description_key"], example["amount_pence"])
            labels = historical_labels.setdefault(key, {})
            labels[example["category_id"]] = labels.get(example["category_id"], 0) + 1
        rows = connection.execute(
            """SELECT t.transaction_id,t.transaction_date,t.raw_description,t.amount_pence,t.category_id,
                      t.category_source,t.classification_confidence,c.name AS category,a.account_name,
                      a.account_id
               FROM transactions t LEFT JOIN categories c ON c.category_id=t.category_id
               JOIN accounts a ON a.account_id=t.account_id
               WHERE t.review_required=1 OR t.category_id IS NULL
               ORDER BY t.transaction_date DESC,t.transaction_id DESC"""
        ).fetchall()
    exact_groups: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    for row in rows:
        key = review_grouping_key(row) if row["category_source"] == "model" and row["category_id"] is not None else ("individual", row["transaction_id"])
        exact_groups.setdefault(key, []).append(row)

    # Fuzzy-group only when the model category and payment direction agree.
    # Merchant similarity is primary; a shared city lowers the name threshold
    # modestly, but cannot create a group on its own.
    grouped: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    merchant_buckets: dict[tuple[object, ...], list[tuple[tuple[object, ...], list[sqlite3.Row]]]] = {}
    for key, members in exact_groups.items():
        if key[0] != "merchant":
            grouped[key] = members
            continue
        merchant_buckets.setdefault((key[3], key[2]), []).append((key, members))
    for (category_id, direction), merchants in merchant_buckets.items():
        clusters: list[list[tuple[tuple[object, ...], list[sqlite3.Row]]]] = []
        for merchant_group in sorted(merchants, key=lambda item: (-len(item[1]), str(item[0][1]))):
            representative = merchant_group[1][0]
            eligible = []
            for cluster in clusters:
                if all(review_merchant_groups_compatible(entry[1], merchant_group[1]) for entry in cluster):
                    cluster_rows = [entry[1][0] for entry in cluster]
                    score = min(review_merchant_similarity(representative["raw_description"], row["raw_description"]) for row in cluster_rows)
                    eligible.append((score, cluster))
            if eligible:
                max(eligible, key=lambda item: item[0])[1].append(merchant_group)
            else:
                clusters.append([merchant_group])
        for cluster in clusters:
            if len(cluster) == 1:
                key, members = cluster[0]
                grouped[key] = members
            else:
                name = str(cluster[0][0][1])
                fuzzy_key = ("merchant-similar", category_id, direction, name)
                grouped[fuzzy_key] = [row for _, members in cluster for row in members]

    repeated_groups = []
    individual_rows = []
    for key, members in grouped.items():
        predictions = {r["category_id"] for r in members if r["category_id"] is not None}
        complete_prediction = len(predictions) == 1 and all(r["category_id"] is not None for r in members)
        confidences = [r["classification_confidence"] for r in members]
        weakest = min((float(value) for value in confidences if value is not None), default=0.0)
        known_labels = historical_labels.get(review_group_key(members[0]["raw_description"], members[0]["amount_pence"]), {})
        known_categories = set(known_labels)
        history_conflicts = bool(known_categories and (len(known_categories) > 1 or not complete_prediction or not known_categories.issubset(predictions)))
        history_support = complete_prediction and len(known_categories) == 1 and next(iter(known_categories), None) in predictions and max(known_labels.values(), default=0) >= 2
        if not complete_prediction or history_conflicts:
            confidence_order = 2
        elif weakest >= 0.80 and history_support:
            confidence_order = 0
        elif weakest >= 0.55:
            confidence_order = 1
        else:
            confidence_order = 2
        if len(members) > 1:
            category_id = next(iter(predictions)) if complete_prediction else None
            row_html = []
            for row in members:
                selector = category_select_html(
                    cats, row["category_id"], aria_label=f'Category for {row["raw_description"]}',
                    attributes="data-category-select",
                )
                row_html.append(f'''<tr data-group-transaction="{row["transaction_id"]}"><td>{esc(row["transaction_date"])}</td><td>{esc(row["account_name"])}</td><td class="merchant">{esc(row["raw_description"])}</td><td class="amount">{money_number(row["amount_pence"])}</td><td class="group-row-actions"><form data-group-row-form data-transaction-id="{row["transaction_id"]}" data-predicted-category="{row["category_id"] or ""}"><input type="hidden" name="transaction_id" value="{row["transaction_id"]}">{selector}<button class="tiny-button" type="button" data-group-action="accept">Confirm</button><small class="save-state" aria-live="polite"></small></form></td></tr>''')
            can_accept_group = all(r["category_source"] == "model" and r["category_id"] is not None for r in members) and len(members) <= 500
            bulk_button = (f'<button class="button" type="button" data-group-bulk>Confirm all</button>' if can_accept_group else "")
            bulk_selector = category_select_html(cats, category_id, aria_label="Category for the whole group", attributes="data-category-select")
            bulk_form = f'''<form class="group-bulk-change" data-group-bulk-change-form><span class="sr-only">Category for the whole group</span>{''.join(f'<input type="hidden" name="transaction_id" value="{r["transaction_id"]}">' for r in members)}{bulk_selector}<small class="save-state" aria-live="polite"></small></form>'''
            # Rank by estimated correctly reviewable transactions: confidence still
            # matters, while larger groups can surface when they offer more total value.
            confidence_weight = {0: 1.0, 1: 0.75, 2: 0.5}[confidence_order]
            review_value = confidence_weight * len(members)
            if key[0] == "merchant-similar":
                names = list(dict.fromkeys(review_group_title(row["raw_description"]) for row in members))
                group_title = f'{names[0]} + {len(names) - 1} similar'
                shared_locations = {review_location(row["raw_description"]) for row in members}
                if len(shared_locations) == 1 and next(iter(shared_locations)):
                    group_title += f' · {next(iter(shared_locations)).title()}'
            else:
                group_title = review_group_title(members[0]["raw_description"])
            repeated_groups.append((review_value, confidence_order, -len(members), f'''<details class="review-group" data-review-group><summary><span class="group-description">{esc(group_title)}</span><span class="group-count">{len(members)} transactions</span>{bulk_button}{bulk_form}</summary><div class="table-wrap"><table class="wide"><tbody>{''.join(row_html)}</tbody></table></div></details>'''))
        else:
            row = members[0]
            selector = category_select_html(cats, row["category_id"], attributes="data-category-select", required=True)
            editor = category_edit_form(row["transaction_id"], selector, "/review")
            individual_rows.append(f'''<tr data-transaction-row="{row["transaction_id"]}"><td>{esc(row["transaction_date"])}</td><td>{esc(row["account_name"])}</td><td class="merchant">{esc(row["raw_description"])}</td><td class="amount">{money_number(row["amount_pence"])}</td><td>{editor}</td></tr>''')
    repeated_groups.sort(key=lambda item: (-item[0], item[1], item[2]))
    group_html = "".join(item[3] for item in repeated_groups) or '<p class="empty">No repeated transaction descriptions to group.</p>'
    individual_html = ''.join(individual_rows) or '<tr><td colspan="5" class="empty">No individual transactions to review.</td></tr>'
    group_headers = '<div class="review-column-head"><span>Date</span><span>Account</span><span>Description</span><span>Amount (£)</span><span>Category / actions</span></div>'
    body = f'''<div class="page-head"><h1>Review <small id="review-count" data-pending-count="{len(rows)}">/{len(rows)} waiting</small></h1></div><section class="panel review-groups">{group_headers}<div class="review-group-list">{group_html}</div></section><section class="panel individual-review"><h2>Review transactions individually</h2><div class="table-wrap"><table class="wide"><thead><tr><th>Date</th><th>Account</th><th>Description</th><th>Amount (£)</th><th>Category</th></tr></thead><tbody>{individual_html}</tbody></table></div></section>'''
    return shell("Review queue", "review", body, notice)


def imports_page(notice: str = "") -> str:
    INBOX.mkdir(parents=True, exist_ok=True); ARCHIVED.mkdir(parents=True, exist_ok=True); REJECTED.mkdir(parents=True, exist_ok=True)
    pending_files = [p for p in sorted(INBOX.iterdir()) if p.is_file() and not p.name.startswith(".")]
    rejected_files = [p for p in sorted(REJECTED.iterdir(), reverse=True)
                      if p.is_file() and not p.name.endswith(".error.txt")][:15]
    with db() as connection:
        imports = connection.execute("SELECT i.import_id,i.filename,i.imported_at,i.row_count,COUNT(CASE WHEN t.review_required=1 THEN 1 END) AS needs_review FROM imports i LEFT JOIN transactions t ON t.import_id=i.import_id GROUP BY i.import_id ORDER BY i.imported_at DESC LIMIT 30").fetchall()
    events = []
    if ACTIVITY.exists():
        try:
            events = [json.loads(line) for line in ACTIVITY.read_text(encoding="utf-8").splitlines()[-8:]]
        except (OSError, json.JSONDecodeError):
            events = []
    inbox_rows = "".join(
        f'<tr><td>{esc(p.name)}</td><td>{"Waiting for download to finish" if p.suffix.casefold()==".csv" else "Only NatWest CSV files are processed; convert or remove this file"}</td><td><span class="pill {"pill-warn" if p.suffix.casefold()==".csv" else ""}">{"In inbox" if p.suffix.casefold()==".csv" else "Unsupported format"}</span></td></tr>'
        for p in pending_files
    ) or '<tr><td colspan="3" class="empty">Inbox is clear.</td></tr>'
    rejected_rows = []
    for path in rejected_files:
        error_path = path.with_suffix(path.suffix + ".error.txt")
        reason = error_path.read_text(encoding="utf-8", errors="replace").strip() if error_path.exists() else "Rejected; no detail recorded."
        rejected_rows.append(f'<tr><td>{esc(path.name)}</td><td class="reject-reason">{esc(reason)}</td></tr>')
    rejected_panel = (f'<section class="panel"><h2>Rejected</h2><div class="table-wrap"><table><thead><tr><th>File</th><th>Reason</th></tr></thead><tbody>{"".join(rejected_rows)}</tbody></table></div></section>'
                      if rejected_rows else "")
    import_rows = "".join(f'<tr><td>{esc(r["imported_at"])}</td><td class="merchant">{esc(r["filename"])}</td><td>{r["row_count"]}</td><td>{r["needs_review"]}</td></tr>' for r in imports) or '<tr><td colspan="4" class="empty">No statements imported yet.</td></tr>'
    activity = "".join(f'<li><span class="activity-dot"></span><b>{esc(e.get("event"))}</b> · {esc(e.get("filename") or "local-finance-tracker service")} <small>{esc(e.get("at"))}</small></li>' for e in reversed(events)) or '<li class="empty">No activity recorded yet.</li>'
    body = f'''<div class="page-head"><h1>Imports</h1><form method="post" action="/scan"><button class="button" type="submit">scan inbox</button></form></div><section class="panel"><div class="table-wrap"><table><thead><tr><th>File</th><th>Status</th><th></th></tr></thead><tbody>{inbox_rows}</tbody></table></div></section>{rejected_panel}<section class="panel"><h2>History</h2><div class="table-wrap"><table><thead><tr><th>Imported</th><th>File</th><th>Rows</th><th>Review</th></tr></thead><tbody>{import_rows}</tbody></table></div></section><section class="panel"><h2>Activity</h2><ul class="activity">{activity}</ul></section>'''
    return shell("Imports", "imports", body, notice)


def tools_page(notice: str = "") -> str:
    with db() as connection:
        training_count = connection.execute("SELECT COUNT(*) FROM training_examples").fetchone()[0]
        manual_count = connection.execute("SELECT COUNT(*) FROM training_examples WHERE label_origin='manual'").fetchone()[0]
        pending_count = connection.execute("SELECT COUNT(*) FROM transactions WHERE review_required=1 OR category_id IS NULL").fetchone()[0]
    body = f'''<div class="page-head"><h1>Tools</h1><a class="button button-quiet" href="/">back</a></div><section class="panel tool-list"><div><b>Reclassify pending</b><small>{pending_count} rows · model uses {training_count} examples</small></div><form method="post" action="/reclassify"><button class="button" type="submit">run</button></form></section><section class="panel tool-list"><div><b>Evaluate model</b><small>Chronological holdout report</small></div><form method="post" action="/evaluate"><button class="button" type="submit">run</button></form></section><section class="panel tool-list"><div><b>Model errors</b><small>Review holdout misclassifications</small></div><a class="button" href="/model-errors">open</a></section><section class="panel tool-list"><div><b>Training data</b><small>{manual_count} manually reviewed · {training_count} total</small></div><a class="button" href="/training">browse</a></section>'''
    return shell("Tools", "tools", body, notice)


def training_page() -> str:
    with db() as connection:
        rows = connection.execute("""SELECT e.example_id,e.transaction_date,e.description,e.source_category,e.label_origin,e.reviewed_at,c.name AS category
          FROM training_examples e JOIN categories c ON c.category_id=e.category_id ORDER BY COALESCE(e.reviewed_at,'') DESC,e.example_id DESC LIMIT 120""").fetchall()
        total = connection.execute("SELECT COUNT(*) FROM training_examples").fetchone()[0]
    trs = "".join(f'<tr><td>{esc(r["transaction_date"] or "—")}</td><td class="merchant">{esc(r["description"])}</td><td>{category_tag(r["category"])}</td><td>{esc(r["label_origin"])}</td><td>{esc(r["reviewed_at"] or "—")}</td></tr>' for r in rows)
    body = f'<div class="page-head"><h1>Training <small>/{total:,}</small></h1><a class="button" href="/tools">back</a></div><section class="panel"><div class="table-wrap"><table><thead><tr><th>Date</th><th>Description</th><th>Category</th><th>Source</th><th>Reviewed</th></tr></thead><tbody>{trs}</tbody></table></div></section>'
    return shell("Training data", "tools", body)


def error_review_page(notice: str = "") -> str:
    with db() as connection:
        try:
            errors, _, _, _, _, _ = classify_holdout(connection, 0.20, "raw")
        except ValueError as exc:
            return shell("Model errors", "tools", f'<div class="page-head"><h1>Model errors</h1><a class="button button-quiet" href="/tools">back to tools</a></div><section class="panel"><p class="empty">{esc(exc)}</p></section>', notice)
        categories_list = categories(connection)
        recent = connection.execute(
            """SELECT d.decision_id,d.decided_at,e.example_id,e.transaction_date,e.description,e.amount_pence,
                      old.name AS previous_category,new.name AS chosen_category,
                      NOT EXISTS (SELECT 1 FROM error_review_decisions later
                                  WHERE later.example_id=d.example_id
                                    AND later.decision_id>d.decision_id
                                    AND later.undone_at IS NULL) AS can_undo
               FROM error_review_decisions d
               JOIN training_examples e ON e.example_id=d.example_id
               JOIN categories old ON old.category_id=d.previous_category_id
               JOIN categories new ON new.category_id=d.chosen_category_id
               WHERE d.undone_at IS NULL ORDER BY d.decision_id DESC LIMIT 15"""
        ).fetchall()
    error_rows = []
    for row, predicted, confidence in errors:
        predicted_id = next((c["category_id"] for c in categories_list if c["name"] == predicted), None)
        selector = category_select_html(
            categories_list, predicted_id, aria_label=f'Category for {row["description"]}',
            attributes=f'data-error-category="{row["example_id"]}"',
        )
        error_rows.append(f'''<tr data-error-row="{row["example_id"]}"><td>{esc(row["transaction_date"] or "—")}</td><td>{esc(row["account_name"] or "—")}</td><td class="merchant">{esc(row["description"])}</td><td class="amount">{money_number(row["amount_pence"])}</td><td>{category_tag(row["category"])}</td><td><form class="inline-edit error-review-form" data-error-review-form method="post" action="/model-errors"><input type="hidden" name="example_id" value="{row["example_id"]}"><small class="error-model-confidence">{confidence:.0%}</small>{selector}<button type="submit" class="tiny-button" name="action" value="correct">Confirm</button><small class="save-state" aria-live="polite"></small></form></td></tr>''')
    recent_rows = []
    for decision in recent:
        disabled = "" if decision["can_undo"] else "disabled title=\"Undo newer decisions for this transaction first\""
        recent_rows.append(f'''<tr data-decision-row="{decision["decision_id"]}"><td>{esc(decision["decided_at"])}</td><td>{esc(decision["transaction_date"] or "—")}</td><td class="merchant">{esc(decision["description"])}</td><td class="amount">{money_number(decision["amount_pence"])}</td><td>{category_tag(decision["previous_category"])} → {category_tag(decision["chosen_category"])}</td><td><form data-undo-form method="post" action="/undo-error-decision"><input type="hidden" name="decision_id" value="{decision["decision_id"]}"><button class="button" type="submit" {disabled}>undo</button></form></td></tr>''')
    body = f'''<div class="page-head"><h1>Model errors <small id="error-count">/{len(errors)} to review</small></h1><a class="button button-quiet" href="/tools">back to tools</a></div><section class="panel"><div class="table-wrap"><table class="wide error-table"><thead><tr><th>Date</th><th>Account</th><th>Description</th><th>Value (£)</th><th>Label</th><th>Model</th></tr></thead><tbody id="error-rows">{''.join(error_rows) or '<tr id="no-errors"><td colspan="6" class="empty">No model errors to review</td></tr>'}</tbody></table></div></section><details class="panel error-history"><summary>Recent decisions</summary><div class="table-wrap"><table class="wide"><thead><tr><th>Decided</th><th>Date</th><th>Description</th><th>Value (£)</th><th>Label change</th><th></th></tr></thead><tbody id="recent-decisions">{''.join(recent_rows) or '<tr class="no-decisions"><td colspan="6" class="empty">No recent decisions</td></tr>'}</tbody></table></div></details>'''
    return shell("Model errors", "tools", body, notice)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalFinanceTracker/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        # Avoid writing search terms or local filenames to the terminal log.
        return

    def respond(self, content: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        data = content.encode("utf-8")
        compressed = len(data) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", "").casefold()
        if compressed:
            data = gzip.compress(data, compresslevel=1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        cache_control = (
            "private, max-age=300"
            if content_type.startswith(("text/css", "text/javascript"))
            else "no-store"
        )
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # WebKit can close a navigation after the user has switched pages.
            # The next request is independent; don't log a noisy traceback.
            return

    def redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def onboarding_page(self, error: str = "") -> str:
        error_html = (
            f'<section class="panel" role="alert"><h2>Setup could not finish</h2>'
            f'<p>{esc(error)}</p><p>Fix the issue, then try again.</p></section>'
            if error else ""
        )
        body = f'''<div class="page-head"><div><div class="eyebrow">FIRST RUN</div><h1>Set up your local local-finance-tracker</h1><p>local-finance-tracker is ready to create a private database on this computer.</p></div></div>
        {error_html}<section class="panel"><h2>What setup does</h2><p>Creates an empty SQLite database and the folders used for statement imports and archives. It does not download, upload, or import any financial data.</p><p>Your database and statements stay on this computer. Import your own bank CSV statements after setup; do not copy somebody else's database if it contains their financial records.</p><form method="post" action="/setup"><button type="submit">Create my database</button></form></section>
        <section class="panel"><h2>After setup</h2><p>local-finance-tracker will open its overview. Use the Imports page to add your own CSV statements. You can stop the app at any time with Ctrl-C in the terminal.</p></section>'''
        return shell("Set up local-finance-tracker", "", body)

    def _validate_local_request(self, *, require_origin: bool) -> bool:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.respond("Forbidden", 403, "text/plain; charset=utf-8")
            return False
        host = (self.headers.get("Host") or "").strip()
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}
        if host not in allowed_hosts:
            self.respond("Forbidden", 403, "text/plain; charset=utf-8")
            return False
        if not require_origin:
            return True
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            self.respond("Requests must come from the local local-finance-tracker page.", 403, "text/plain; charset=utf-8")
            return False
        allowed_origins = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}"}
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if origin in allowed_origins and urlparse(origin).netloc == host:
                return True
            self.respond("Requests must come from the local local-finance-tracker page.", 403, "text/plain; charset=utf-8")
            return False
        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            parsed_ref = urlparse(referer)
            ref_origin = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
            if ref_origin in allowed_origins and parsed_ref.netloc == host:
                return True
        self.respond("Requests must come from the local local-finance-tracker page.", 403, "text/plain; charset=utf-8")
        return False

    def _setup_database(self) -> None:
        # Never run initialization over an existing DB.
        if DATABASE.exists():
            self.redirect("/")
            return
        setup_script = ROOT / "src" / "setup.py"
        if not setup_script.is_file():
            self.respond(self.onboarding_page("Setup command src/setup.py is not available."), 503)
            return
        try:
            result = subprocess.run(
                [sys.executable, str(setup_script)], cwd=ROOT,
                capture_output=True, text=True, timeout=90,
            )
            if result.returncode != 0 or not DATABASE.is_file():
                detail = (result.stderr or result.stdout or "Setup did not create the database.").strip()
                self.respond(self.onboarding_page(detail[-2000:]), 500)
                return
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.respond(self.onboarding_page(f"{type(exc).__name__}: {exc}"), 500)
            return
        for folder in (INBOX, ARCHIVED, REJECTED):
            folder.mkdir(parents=True, exist_ok=True)
        start_watcher()
        self.redirect("/")

    def do_GET(self) -> None:
        if not self._validate_local_request(require_origin=False):
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not DATABASE.is_file() and parsed.path not in {"/style.css", "/app.js"}:
            self.respond(self.onboarding_page())
            return
        if parsed.path == "/style.css":
            css = ROOT / "src" / "style.css"
            self.respond(css.read_text(encoding="utf-8"), content_type="text/css; charset=utf-8")
        elif parsed.path == "/app.js":
            script = ROOT / "src" / "app.js"
            self.respond(script.read_text(encoding="utf-8"), content_type="text/javascript; charset=utf-8")
        elif parsed.path == "/":
            self.respond(dashboard((params.get("period") or ["month"])[0], (params.get("date") or [""])[0]))
        elif parsed.path == "/category-transactions":
            try:
                category_id = int((params.get("category_id") or [""])[0])
                fragment = category_transactions_fragment(category_id, (params.get("period") or ["month"])[0], (params.get("date") or [""])[0])
                self.respond(fragment)
            except (TypeError, ValueError):
                self.respond('<p class="empty category-empty">Invalid category.</p>', 400)
        elif parsed.path == "/transactions":
            self.respond(transaction_page(params))
        elif parsed.path == "/review":
            with db() as connection:
                exact_matches = apply_exact_training_matches(connection)
                merchant_matches = apply_merchant_training_matches(connection)
            count = exact_matches + merchant_matches
            notice = f"Automatically matched {count} transaction(s) to reviewed training data." if count else ""
            stat = DATABASE.stat()
            signature = (stat.st_mtime_ns, stat.st_size, date.today().isoformat())
            global _review_page_cache_signature, _review_page_cache_html
            with _review_page_cache_lock:
                if not count and signature == _review_page_cache_signature:
                    page = _review_page_cache_html
                else:
                    page = review_page(notice)
                    _review_page_cache_signature = signature
                    _review_page_cache_html = page
            self.respond(page)
        elif parsed.path == "/imports":
            self.respond(imports_page())
        elif parsed.path == "/tools":
            self.respond(tools_page())
        elif parsed.path == "/training":
            self.respond(training_page())
        elif parsed.path == "/model-errors":
            self.respond(error_review_page())
        else:
            self.respond(shell("Not found", "", '<section class="panel"><h1>Page not found</h1><a href="/">Return to overview</a></section>'), 404)

    def do_POST(self) -> None:
        if not self._validate_local_request(require_origin=True):
            return
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.respond("Invalid Content-Length.", 400, "text/plain; charset=utf-8")
            return
        if length < 0:
            self.respond("Invalid Content-Length.", 400, "text/plain; charset=utf-8")
            return
        if length > 200_000:
            self.respond("Request body too large.", 413, "text/plain; charset=utf-8")
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        try:
            if parsed.path == "/setup":
                self._setup_database()
            elif not DATABASE.is_file():
                self.respond(self.onboarding_page(), 409)
            elif parsed.path == "/scan":
                results = scan_once()
                summary = ", ".join(f"{count} {name}" for name,count in results.items() if count)
                self.respond(imports_page("Inbox checked: " + (summary or "no stable CSV ready yet")))
            elif parsed.path == "/categorize":
                self._categorize(form)
            elif parsed.path == "/categorize-bulk":
                self._categorize_bulk(form)
            elif parsed.path == "/decline-suggestion":
                self._decline_suggestion(form)
            elif parsed.path == "/reclassify":
                self._reclassify()
            elif parsed.path == "/evaluate":
                result = subprocess.run([sys.executable, str(ROOT / "src" / "evaluate_classifier.py"), "--text-mode", "raw+direction"], cwd=ROOT, capture_output=True, text=True, timeout=90)
                output = result.stdout + ("\n" + result.stderr if result.stderr else "")
                body = f'<div class="page-title"><div><div class="eyebrow">LOCAL CLASSIFIER</div><h1>Evaluation results</h1><p>Chronological holdout · raw description with payment direction.</p></div><a class="button button-quiet" href="/tools">Back to tools</a></div><section class="panel"><pre class="eval-output">{esc(output[:30000])}</pre></section>'
                self.respond(shell("Evaluation", "tools", body))
            elif parsed.path == "/model-errors":
                self._save_error(form)
            elif parsed.path == "/undo-error-decision":
                self._undo_error_decision(form)
            else:
                self.respond("Not found", 404)
        except Exception as exc:
            # Keep a concise local application error; never log request form contents.
            if "application/json" in self.headers.get("Accept", ""):
                self.respond(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500, "application/json; charset=utf-8")
                return
            self.respond(shell("Action failed", "", f'<section class="panel"><div class="eyebrow">COULD NOT COMPLETE ACTION</div><h1>Please try again</h1><p>{esc(type(exc).__name__)}: {esc(exc)}</p><a href="/">Return to overview</a></section>'), 500)

    def _categorize(self, form: dict[str, list[str]]) -> None:
        transaction_id = int(form["transaction_id"][0])
        category_id = int(form["category_id"][0])
        with db() as connection:
            transaction = connection.execute(
                """SELECT t.transaction_id,t.transaction_date,t.raw_description,t.amount_pence,t.category_id,
                          a.account_name FROM transactions t JOIN accounts a USING(account_id) WHERE transaction_id=?""",
                (transaction_id,),
            ).fetchone()
            category = connection.execute("SELECT name FROM categories WHERE category_id=?", (category_id,)).fetchone()
            if not transaction or not category:
                raise ValueError("Transaction or category was not found")
            token = uuid.uuid4().hex
            cursor = connection.execute("INSERT INTO training_imports(filename,file_sha256,row_count) VALUES (?,?,0)",
                ("web-review-" + datetime.now().strftime("%Y%m%dT%H%M%S"), hashlib.sha256(token.encode()).hexdigest()))
            batch_id = cursor.lastrowid
            connection.commit()
            added = save_choice(connection, transaction, category_id, category[0], batch_id, 1)
            if not added:
                connection.execute("DELETE FROM training_imports WHERE training_import_id=?", (batch_id,))
        if "application/json" in self.headers.get("Accept", ""):
            self.respond(json.dumps({"ok": True, "transaction_id": transaction_id, "category": category[0]}), content_type="application/json; charset=utf-8")
        else:
            return_to = form.get("return_to", ["/review"])[0]
            parsed_return = urlparse(return_to)
            if parsed_return.path not in {"/review", "/transactions"} or parsed_return.netloc:
                return_to = "/review"
            self.redirect(return_to)

    def _categorize_bulk(self, form: dict[str, list[str]]) -> None:
        transaction_ids = [int(value) for value in form.get("transaction_id", [])]
        mode = form.get("mode", ["accept"])[0]
        requested_category_id = int(form["category_id"][0]) if mode == "change" and form.get("category_id", [""])[0] else None
        selected_category_ids = [int(value) for value in form.get("category_id", [])] if mode == "accept" else []
        if (mode not in {"accept", "change"}
                or (mode == "change" and requested_category_id is None)
                or (mode == "accept" and len(selected_category_ids) != len(transaction_ids))):
            raise ValueError("Choose a category for the whole group")
        if len(transaction_ids) < 2 or len(transaction_ids) > 500 or len(set(transaction_ids)) != len(transaction_ids):
            raise ValueError("The group selection is invalid")
        with db() as connection:
            placeholders = ",".join("?" for _ in transaction_ids)
            rows = connection.execute(
                f"""SELECT t.transaction_id,t.transaction_date,t.raw_description,t.amount_pence,
                           t.category_id,t.category_source,t.classification_confidence,a.account_name
                    FROM transactions t JOIN accounts a USING(account_id)
                    WHERE t.transaction_id IN ({placeholders})
                      AND (t.review_required=1 OR t.category_id IS NULL)""",
                transaction_ids,
            ).fetchall()
            if len(rows) != len(transaction_ids):
                raise ValueError("Some transactions in this group are no longer awaiting review")
            if not review_group_compatible(rows) or any(row["category_source"] not in {"model", "unreviewed"} for row in rows):
                raise ValueError("Only pending transactions from one review group can be confirmed together")
            category_names = {row[0]: row[1] for row in connection.execute("SELECT category_id,name FROM categories")}
            categories_to_apply = set(selected_category_ids) if mode == "accept" else {requested_category_id}
            if not categories_to_apply.issubset(category_names):
                raise ValueError("The selected category was not found")
            category_by_transaction = dict(zip(transaction_ids, selected_category_ids)) if mode == "accept" else {}

            connection.execute("BEGIN IMMEDIATE")
            try:
                token = uuid.uuid4().hex
                cursor = connection.execute(
                    "INSERT INTO training_imports(filename,file_sha256,row_count) VALUES (?,?,0)",
                    ("web-review-group-" + datetime.now().strftime("%Y%m%dT%H%M%S"), hashlib.sha256(token.encode()).hexdigest()),
                )
                batch_id = cursor.lastrowid
                added = 0
                for row in rows:
                    category_id = requested_category_id if mode == "change" else category_by_transaction[row["transaction_id"]]
                    category_name = category_names[category_id]
                    if add_or_update_training_example(connection, row, category_id, category_name, batch_id, added + 1):
                        added += 1
                    connection.execute(
                        "UPDATE transactions SET category_id=?,category_source='manual',review_required=0 WHERE transaction_id=?",
                        (category_id, row["transaction_id"]),
                    )
                if added:
                    connection.execute("UPDATE training_imports SET row_count=? WHERE training_import_id=?", (added, batch_id))
                else:
                    connection.execute("DELETE FROM training_imports WHERE training_import_id=?", (batch_id,))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.respond(json.dumps({"ok": True, "count": len(rows)}), content_type="application/json; charset=utf-8")

    def _decline_suggestion(self, form: dict[str, list[str]]) -> None:
        transaction_id = int(form["transaction_id"][0])
        with db() as connection:
            cursor = connection.execute(
                """UPDATE transactions SET category_id=NULL,category_source='unreviewed',
                       review_required=1,classification_confidence=NULL,classification_model=NULL
                   WHERE transaction_id=? AND (review_required=1 OR category_id IS NULL)""",
                (transaction_id,),
            )
            if not cursor.rowcount:
                raise ValueError("This transaction is no longer awaiting review")
        self.respond(json.dumps({"ok": True, "transaction_id": transaction_id}), content_type="application/json; charset=utf-8")

    def _reclassify(self) -> None:
        with db() as connection:
            exact_matches = apply_exact_training_matches(connection)
            merchant_matches = apply_merchant_training_matches(connection)
            rows = connection.execute("""SELECT t.transaction_id,t.raw_description,t.amount_pence
                FROM transactions t WHERE t.review_required=1 OR t.category_id IS NULL""").fetchall()
            try:
                model = train_from_database(connection)
            except TrainingDataUnavailableError as exc:
                suggestions = []
                training_notice = f" Model suggestions are not available yet: {exc}. Review transactions manually or import labelled history."
            else:
                cat_ids = {name: category_id for name, category_id in connection.execute("SELECT name,category_id FROM categories")}
                predictions = predict_many(model, [(r["raw_description"], r["amount_pence"]) for r in rows])
                suggestions = list(zip(predictions, (r["transaction_id"] for r in rows)))
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany("""UPDATE transactions SET category_id=?,category_source='model',review_required=1,
                    classification_confidence=?,classification_model=? WHERE transaction_id=?""",
                    [(cat_ids[pred[0]], pred[1], MODEL_NAME, transaction_id) for pred,transaction_id in suggestions])
                connection.commit()
                training_notice = " Model suggestions remain in the review queue."
        lookup_count = exact_matches + merchant_matches
        lookup_notice = f" Matched {lookup_count} transaction(s) to reviewed training data." if lookup_count else ""
        self.respond(review_page(f"Refreshed {len(suggestions)} pending model suggestions.{lookup_notice}{training_notice}"))

    def _save_error(self, form: dict[str, list[str]]) -> None:
        example_id = int(form["example_id"][0])
        action = form.get("action", [""])[0]
        with db() as connection:
            errors, *_ = classify_holdout(connection, 0.20, "raw")
            item = next((error for error in errors if error[0]["example_id"] == example_id), None)
            if item is not None:
                row = item[0]
                if action == "keep":
                    chosen_id = row["category_id"]
                elif action == "correct":
                    chosen_id = int(form["category_id"][0])
                elif action == "model":
                    chosen_id = connection.execute("SELECT category_id FROM categories WHERE name=?", (item[1],)).fetchone()[0]
                else:
                    raise ValueError("Choose keep, model, or set")
                category_name = connection.execute("SELECT name FROM categories WHERE category_id=?", (chosen_id,)).fetchone()[0]
                decision_id = record_label(connection, row, chosen_id)
            else:
                raise ValueError("This item is no longer in the current error list")
        if "application/json" in self.headers.get("Accept", ""):
            self.respond(json.dumps({"ok": True, "decision_id": decision_id,
                "example_id": example_id, "date": row["transaction_date"],
                "description": row["description"], "amount_pence": row["amount_pence"],
                "previous_category": row["category"], "chosen_category": category_name}),
                content_type="application/json; charset=utf-8")
        else:
            self.redirect("/model-errors")

    def _undo_error_decision(self, form: dict[str, list[str]]) -> None:
        decision_id = int(form["decision_id"][0])
        with db() as connection:
            decision = undo_label(connection, decision_id)
        if "application/json" in self.headers.get("Accept", ""):
            self.respond(json.dumps({"ok": True, "decision_id": decision_id,
                                     "example_id": decision["example_id"]}),
                         content_type="application/json; charset=utf-8")
        else:
            self.redirect("/model-errors")


def main() -> None:
    if DATABASE.is_file():
        for folder in (INBOX, ARCHIVED, REJECTED):
            folder.mkdir(parents=True, exist_ok=True)
        start_watcher()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"local-finance-tracker is running at http://127.0.0.1:{PORT}")
    print(f"Drop NatWest CSV statements into: {INBOX}")
    print("Keep this process running while you want the folder watcher active. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local-finance-tracker.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
