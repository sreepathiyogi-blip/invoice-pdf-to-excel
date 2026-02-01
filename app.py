import streamlit as st
import pdfplumber
import pandas as pd
import re
from io import BytesIO
from datetime import datetime
from collections import defaultdict


# ─────────────────────────────────────────────
# 1.  TOKENIZER  —  splits raw PDF text into
#     positional, context-aware tokens
# ─────────────────────────────────────────────

class Token:
    """A single token carrying its text, position, and surrounding context."""
    __slots__ = ("text", "pos", "line_idx", "line", "prev_tokens", "next_tokens")

    def __init__(self, text, pos, line_idx, line, prev_tokens, next_tokens):
        self.text = text
        self.pos = pos                  # character offset in full text
        self.line_idx = line_idx        # which line it came from
        self.line = line                # the full line string
        self.prev_tokens = prev_tokens  # list of up to N previous tokens (strings)
        self.next_tokens = next_tokens  # list of up to N next tokens (strings)


def _split_glued_dash(token_text):
    """
    Split tokens where a label and value are glued together with a dash.
    e.g. 'no.-450010110017123' → ['no.', '450010110017123']
         'Code-BKID0004500'   → ['Code', 'BKID0004500']
         'Name-'              → ['Name']   (trailing dash, no value — drop the dash)
    Does NOT split things like '3,000.00' or normal hyphenated words.
    """
    # Match: <label part> then dash(es) then <value part>
    # Label part must end with a letter or period; value part must start with
    # a letter or digit (so we don't break '3,000.00' or 'e-mail')
    m = re.match(r'^(.*?[A-Za-z.])[-–—]+([A-Za-z0-9].*)$', token_text)
    if m:
        label = m.group(1).strip()
        value = m.group(2).strip()
        parts = []
        if label:
            parts.append(label)
        if value:
            parts.append(value)
        return parts if parts else [token_text]

    # Trailing dash only (like 'no.-' with nothing after) — just strip the dash
    m2 = re.match(r'^(.*?[A-Za-z.])[-–—]+$', token_text)
    if m2:
        label = m2.group(1).strip()
        return [label] if label else [token_text]

    return [token_text]


def tokenize(text, context_window=4):
    """
    Tokenize full-page text into Token objects with positional context.
    Splits on whitespace, then further splits glued label-dash-value tokens
    (e.g. 'no.-450010110017123') so values can be scored independently.
    """
    tokens = []
    lines = text.split("\n")
    char_offset = 0

    for line_idx, line in enumerate(lines):
        # Split on whitespace runs
        parts = re.split(r'(\s+)', line)
        raw_tokens = [p for p in parts if p.strip()]

        # Expand any glued dash tokens
        line_tokens_text = []
        for t in raw_tokens:
            line_tokens_text.extend(_split_glued_dash(t))

        for tok_idx, tok_text in enumerate(line_tokens_text):
            # Find exact char position within original text
            pos = text.find(tok_text, char_offset)
            if pos == -1:
                pos = char_offset  # fallback

            prev = line_tokens_text[max(0, tok_idx - context_window):tok_idx]
            nxt = line_tokens_text[tok_idx + 1:tok_idx + 1 + context_window]

            tokens.append(Token(
                text=tok_text,
                pos=pos,
                line_idx=line_idx,
                line=line,
                prev_tokens=prev,
                next_tokens=nxt
            ))
            char_offset = pos + len(tok_text)

        char_offset += len(line) + 1  # +1 for the newline

    return tokens


# ─────────────────────────────────────────────
# 2.  ENTITY DEFINITIONS  —  each entity type
#     has: format regex, context keywords (with
#     weights), and validation rules
# ─────────────────────────────────────────────

ENTITY_DEFS = {
    "IFSC": {
        "format": re.compile(r'^[A-Z]{4}[0-9]{7}$', re.IGNORECASE),
        "context_keywords": {
            "ifsc": 8, "code": 3, "bank": 4, "branch": 3
        },
        "validate": lambda v: len(v) == 11 and re.match(r'^[A-Z]{4}\d{7}$', v.upper())
    },
    "PAN": {
        "format": re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$', re.IGNORECASE),
        "context_keywords": {
            "pan": 9, "permanent": 5, "account": 3, "number": 2, "tin": 4
        },
        "validate": lambda v: len(v) == 10 and re.match(r'^[A-Z]{5}\d{4}[A-Z]$', v.upper())
    },
    "GST": {
        "format": re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][Z][0-9A-Z]$', re.IGNORECASE),
        "context_keywords": {
            "gst": 9, "gstin": 10, "tin": 5, "tax": 4, "identification": 3
        },
        "validate": lambda v: len(v) == 15
    },
    "ACCOUNT_NUMBER": {
        # Indian bank account numbers: 9–18 digits
        "format": re.compile(r'^\d{9,18}$'),
        "context_keywords": {
            "account": 8, "number": 4, "ac": 7, "a/c": 9, "acc": 7,
            "bank": 5, "holder": 3, "no": 3
        },
        "validate": lambda v: 9 <= len(v) <= 18 and v.isdigit()
    },
    "INVOICE_NUMBER": {
        # 2–7 digits. 8+ digit pure numbers are phone/account territory.
        "format": re.compile(r'^\d{2,7}$'),
        "context_keywords": {
            "invoice": 9, "no": 5, "number": 4, "bill": 6, "#": 5, "dated": 3
        },
        "validate": lambda v: 2 <= len(v) <= 7 and v.isdigit()
    },
    "AMOUNT": {
        # Must have commas OR decimals — bare small integers are not amounts
        "format": re.compile(r'^(\d{1,3}(,\d{3})+(\.\d{1,2})?|\d+\.\d{1,2})$'),
        "context_keywords": {
            "amount": 9, "total": 7, "grand": 5, "net": 4, "payable": 6,
            "balance": 5, "due": 4, "rs": 3, "inr": 3, "₹": 4, "rate": 2
        },
        "validate": lambda v: float(v.replace(',', '')) >= 10
    },
    "DATE": {
        # DD-Mon-YY, DD/MM/YYYY, DD.MM.YYYY, etc.
        "format": re.compile(
            r'^\d{1,2}[-./]\s*([A-Za-z]{3}|\d{1,2})\s*[-./]\d{2,4}$'
        ),
        "context_keywords": {
            "date": 8, "dated": 9, "invoice": 4, "on": 2, "day": 3
        },
        "validate": lambda v: True  # structural validation done in format regex
    },
    "PHONE_NUMBER": {
        # Indian mobile: 10 digits, starts with 6/7/8/9
        "format": re.compile(r'^[6-9]\d{9}$'),
        "context_keywords": {
            "phone": 9, "ph": 7, "tel": 7, "mobile": 9, "mob": 8,
            "cell": 6, "contact": 5, "fax": 5, "whatsapp": 7,
            "helpline": 4, "toll": 4, "call": 4,
            # with colon variants
            "phone:": 9, "ph:": 7, "tel:": 7, "mobile:": 9, "mob:": 8,
            "fax:": 5, "contact:": 5, "whatsapp:": 7,
        },
        "validate": lambda v: len(v) == 10 and v[0] in '6789'
    }
}

# ─────────────────────────────────────────────
# 2b. NEGATIVE CONTEXT  —  words that KILL a
#     candidate's score regardless of entity type.
#     If any of these appear on the same line or
#     in surrounding tokens, score → 0 instantly.
# ─────────────────────────────────────────────

NEGATIVE_CONTEXT = {
    # Phone / contact related
    "phone", "ph", "ph.", "tel", "tel.", "mobile", "mob", "mob.",
    "cell", "contact", "fax", "fax.", "helpline", "toll", "whatsapp",
    "call", "sms", "isd", "std",
    # Prefixes that appear glued to phone labels
    "phone:", "ph:", "tel:", "mobile:", "mob:", "fax:", "contact:",
    "whatsapp:", "helpline:",
}


# ─────────────────────────────────────────────
# 3.  NLP SCORER  —  scores each candidate token
#     by combining format match + context signal
# ─────────────────────────────────────────────

def score_token_for_entity(token, entity_type):
    """
    Returns a confidence score (0–100) for how likely this token
    is the target entity.

    Score components:
      - Format match:      +50 if the token text matches the entity's format regex
      - Context keywords:  +N  for each keyword found in surrounding tokens
                           (weighted per keyword definition)
      - Position bonus:    +5  if keyword is immediately adjacent (prev/next[0])
      - Penalty:           -30 if the token matches a DIFFERENT entity's format
                           (e.g., an IFSC code shouldn't be picked as PAN)
    """
    edef = ENTITY_DEFS[entity_type]
    score = 0

    # --- Format match ---
    if edef["format"].match(token.text.strip()):
        score += 50
    else:
        return 0  # Hard gate: if format doesn't match at all, skip

    # --- Negative context check (runs before positive scoring) ---
    # If phone/mobile/contact/fax etc. appear nearby, this token is
    # almost certainly a phone number — kill it immediately.
    surrounding = [t.lower() for t in token.prev_tokens + token.next_tokens]
    line_words = [w.lower() for w in token.line.split()]
    all_context = set(surrounding + line_words)

    if all_context & NEGATIVE_CONTEXT and entity_type != "PHONE_NUMBER":
        return 0  # Hard kill — no phone numbers slip through

    for keyword, weight in edef["context_keywords"].items():
        kw = keyword.lower()
        if kw in surrounding:
            score += weight
            # Bonus for immediate adjacency
            if token.prev_tokens and token.prev_tokens[-1].lower().startswith(kw):
                score += 5
            if token.next_tokens and token.next_tokens[0].lower().startswith(kw):
                score += 5
        elif kw in line_words:
            # Weaker signal: keyword is on same line but not immediately adjacent
            score += weight * 0.4

    # --- Cross-entity penalty ---
    # If this token also matches a different entity's format strongly,
    # reduce confidence to avoid ambiguity (e.g., long digit strings)
    for other_type, odef in ENTITY_DEFS.items():
        if other_type == entity_type:
            continue
        if odef["format"].match(token.text.strip()):
            # Only penalize if the other entity has stronger context here
            other_context_score = 0
            for kw, w in odef["context_keywords"].items():
                if kw.lower() in surrounding or kw.lower() in line_words:
                    other_context_score += w
            if other_context_score > 5:
                score -= 20

    return max(score, 0)


def find_entity(tokens, entity_type, top_n=3):
    """
    Score all tokens for a given entity type and return the top candidates
    sorted by score descending.  Each result is (token, score).
    """
    candidates = []
    seen_values = set()

    for token in tokens:
        # Deduplicate: only score each unique value once
        clean = token.text.strip()
        if clean in seen_values:
            continue
        seen_values.add(clean)

        s = score_token_for_entity(token, entity_type)
        if s > 0:
            candidates.append((token, s))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:top_n]


# ─────────────────────────────────────────────
# 4.  SPECIALIZED EXTRACTORS  —  for fields that
#     need more than token-level scoring (party
#     name, amount from tables)
# ─────────────────────────────────────────────

def extract_party_name(text, tokens):
    """
    Party name extraction uses a multi-strategy approach:
      1. Look for 'Account Holder' label and grab the next non-keyword line
      2. Look for a capitalized name in lines 1–6 of the header
      3. Look for a name right after 'INVOICE' heading
    Scored by: all-caps likelihood, length reasonableness, absence of
    numeric/keyword pollution.
    """
    candidates = []  # (name, score)

    lines = text.split("\n")

    # --- Strategy 1: Account Holder block ---
    for i, line in enumerate(lines):
        if re.search(r'account\s+holder', line, re.IGNORECASE):
            # Check this line and next 2 lines for a name
            for j in range(i, min(i + 3, len(lines))):
                candidate = re.sub(
                    r'(account\s+holder|name|:|-)', '', lines[j], flags=re.IGNORECASE
                ).strip()
                if candidate and re.match(r'^[A-Za-z\s\.]+$', candidate) and 3 < len(candidate) < 80:
                    score = 40
                    if candidate.isupper():
                        score += 10  # All-caps names are common in Indian invoices
                    candidates.append((candidate, score))

    # --- Strategy 2: Header name (lines 1–6) ---
    for i, line in enumerate(lines[:7]):
        line = line.strip()
        if not line:
            continue
        # Must be mostly alphabetic, reasonable length
        if re.match(r'^[A-Z][A-Za-z\s\.&,\-]+$', line) and 3 < len(line) < 60:
            # Skip lines that look like labels or headers
            skip_words = ['invoice', 'phone', 'email', 'address', 'gst', 'pan',
                          'date', 'total', 'amount', 'bank', 'ifsc']
            if any(sw in line.lower() for sw in skip_words):
                continue
            score = 25
            if i <= 2:
                score += 10  # Earlier lines are more likely to be the company name
            if line.isupper():
                score += 5
            candidates.append((line, score))

    # --- Strategy 3: Name right after INVOICE keyword ---
    for i, line in enumerate(lines):
        if re.match(r'^INVOICE\b', line.strip(), re.IGNORECASE):
            # Check next 1-2 non-empty lines
            for j in range(i + 1, min(i + 3, len(lines))):
                candidate = lines[j].strip()
                if candidate and re.match(r'^[A-Z][A-Za-z\s\.&]+$', candidate) and 3 < len(candidate) < 60:
                    candidates.append((candidate, 30))
                    break

    if not candidates:
        return "", 0

    # Pick highest-scored candidate
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]


def extract_amount_from_tables(tables):
    """
    Table-aware amount extraction.  Looks for an 'Amount' column header,
    then collects all numeric values in that column.  Scores them by:
      - Proper currency format (X,XXX.XX) → high score
      - Larger value → slight preference (totals tend to be at the bottom / largest)
      - Position: last value in an Amount column is often the total
    """
    candidates = []

    for table in tables:
        if not table:
            continue

        amount_col_idx = None

        for row_idx, row in enumerate(table):
            if not row:
                continue

            # Detect Amount column header
            for col_idx, cell in enumerate(row):
                if cell and re.search(r'amount', str(cell), re.IGNORECASE):
                    amount_col_idx = col_idx
                    break

            # Once we have the column, pull all numeric values below it
            if amount_col_idx is not None:
                for sub_row in table[row_idx + 1:]:
                    if amount_col_idx < len(sub_row) and sub_row[amount_col_idx]:
                        raw = re.sub(r'[^\d,.]', '', str(sub_row[amount_col_idx]).strip())
                        if not raw:
                            continue
                        try:
                            val = float(raw.replace(',', ''))
                            if 10 <= val < 1e8:
                                score = 10
                                if '.' in raw:
                                    score += 15   # Has decimals → likely a real amount
                                if re.match(r'^\d{1,3}(,\d{3})*\.\d{2}$', raw):
                                    score += 20   # Perfect currency format
                                candidates.append((raw.replace(',', ''), score, val))
                        except ValueError:
                            continue
                # Reset so we don't re-trigger on next row
                amount_col_idx = None

    if not candidates:
        return "", 0

    # Sort by score desc, then by value desc (prefer totals)
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return candidates[0][0], candidates[0][1]


# ─────────────────────────────────────────────
# 5.  DATE NORMALIZER
# ─────────────────────────────────────────────

MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
}


def normalize_date(date_str):
    """Normalize any detected date to DD-MM-YYYY."""
    if not date_str:
        return ""

    # DD-Mon-YY or DD-Mon-YYYY
    m = re.match(r'(\d{1,2})[-./]\s*([A-Za-z]{3})\s*[-./](\d{2,4})', date_str)
    if m:
        day = m.group(1).zfill(2)
        month = MONTH_MAP.get(m.group(2).lower(), '01')
        year = m.group(3)
        if len(year) == 2:
            year = '20' + year
        return f"{day}-{month}-{year}"

    # DD-MM-YY or DD-MM-YYYY
    m = re.match(r'(\d{1,2})[-./]\s*(\d{1,2})\s*[-./](\d{2,4})', date_str)
    if m:
        day = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        year = m.group(3)
        if len(year) == 2:
            year = '20' + year
        return f"{day}-{month}-{year}"

    return date_str


# ─────────────────────────────────────────────
# 6.  BANK NAME LOOKUP FROM IFSC
# ─────────────────────────────────────────────

IFSC_PREFIX_MAP = {
    "HDFC": "HDFC Bank",
    "ICIC": "ICICI Bank",
    "SBIN": "SBI",
    "AXIS": "Axis Bank",
    "KKBK": "Kotak Mahindra Bank",
    "BKID": "Bank of India",
    "PUNB": "Punjab National Bank",
    "UBIN": "Union Bank of India",
    "BARB": "Bank of Baroda",
    "CNRB": "Canara Bank",
    "INDB": "IndusInd Bank",
    "UTIB": "Axis Bank",
    "YESB": "Yes Bank",
    "RATN": "RBL Bank",
    "BANDL": "Bandhan Bank",
    "FDCB": "Federal Bank",
    "SIBL": "South Indian Bank",
    "KVBL": "KVB Bank",
    "TMBL": "TMB Bank",
    "CITI": "Citibank",
    "HSBC": "HSBC",
}


def derive_bank_name(ifsc):
    if not ifsc or len(ifsc) < 4:
        return ""
    prefix = ifsc[:4].upper()
    return IFSC_PREFIX_MAP.get(prefix, prefix)


# ─────────────────────────────────────────────
# 7.  VALIDATION ENGINE  —  cross-field checks
#     after all entities are extracted
# ─────────────────────────────────────────────

def validate_extraction(record):
    """
    Cross-field validation.  Returns a list of warning strings.
    """
    warnings = []

    # Amount present but no account number
    if record.get("Amount") and not record.get("Bank Account No"):
        warnings.append("Amount found but no Bank Account Number detected")

    # Account number present but no IFSC
    if record.get("Bank Account No") and not record.get("IFSC Code"):
        warnings.append("Account Number found but no IFSC Code detected")

    # Invoice number looks suspiciously like a phone/account number (too long)
    inv = record.get("Invoice No.", "")
    if inv and len(inv) > 7:
        warnings.append(f"Invoice No. '{inv}' is unusually long — may be misclassified")

    # No party name
    if not record.get("Party name"):
        warnings.append("Party name could not be detected")

    # No invoice date
    if not record.get("Invoice Date"):
        warnings.append("Invoice Date could not be detected")

    # Amount is 0 or missing
    if not record.get("Amount"):
        warnings.append("Amount could not be detected")

    return warnings


def parse_inline_bank_details(text):
    """
    Handles the single-line comma-separated bank detail format:
      'Your Bank details: NAME - CHETAN ANAND, BANK NAME - AXIS BANK,
       BANK ACCOUNT NO - 918010048531255, IFSC CODE - UTIB0000378'

    Returns a dict with any fields found: account_no, ifsc, bank_name
    """
    result = {}

    # Find the line that contains inline bank details
    # Trigger: 'Bank details' or 'Your Bank' followed by multiple KEY - VALUE pairs
    for line in text.split("\n"):
        if not re.search(r'bank\s+(details|account)', line, re.IGNORECASE):
            continue

        # Account number: BANK ACCOUNT NO - <digits>
        m = re.search(r'BANK\s+ACCOUNT\s+NO\s*[-–—:]\s*(\d{9,18})', line, re.IGNORECASE)
        if m:
            result["account_no"] = m.group(1).strip()

        # IFSC: IFSC CODE - <code>
        m = re.search(r'IFSC\s*(?:CODE)?\s*[-–—:]\s*([A-Z]{4}\d{7})', line, re.IGNORECASE)
        if m:
            result["ifsc"] = m.group(1).upper().strip()

        # Bank name: BANK NAME - <name> (up to next comma or end)
        m = re.search(r'BANK\s+NAME\s*[-–—:]\s*([A-Za-z\s]+?)(?:,|$)', line, re.IGNORECASE)
        if m:
            result["bank_name"] = m.group(1).strip()

        # If we found at least account or IFSC, no need to check other lines
        if result:
            break

    return result

def extract_invoice_data(pdf_file, debug_mode=False):
    """
    Full pipeline:
      PDF → Text Extract → Tokenize → NLP Score → Validate → Record
    """
    try:
        with pdfplumber.open(pdf_file) as pdf:
            full_text = ""
            all_tables = []

            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"
                tables = page.extract_tables()
                if tables:
                    all_tables.extend(tables)

            if not full_text.strip():
                return None, ["No text extracted — PDF may be scanned/image-based"], ""

            # ── Tokenize ──
            tokens = tokenize(full_text, context_window=5)

            # ── NLP Entity Detection ──
            ifsc_candidates = find_entity(tokens, "IFSC")
            pan_candidates = find_entity(tokens, "PAN")
            gst_candidates = find_entity(tokens, "GST")
            acc_candidates = find_entity(tokens, "ACCOUNT_NUMBER")
            inv_candidates = find_entity(tokens, "INVOICE_NUMBER")
            date_candidates = find_entity(tokens, "DATE")
            amount_candidates = find_entity(tokens, "AMOUNT")
            phone_candidates = find_entity(tokens, "PHONE_NUMBER")

            # ── Pick best candidate per field ──
            ifsc = ifsc_candidates[0][0].text.upper().strip() if ifsc_candidates else ""
            pan = pan_candidates[0][0].text.upper().strip() if pan_candidates else ""
            gst = gst_candidates[0][0].text.upper().strip() if gst_candidates else ""

            # Account number: must not be the same as invoice number candidate
            inv_no = inv_candidates[0][0].text.strip() if inv_candidates else ""
            acc_no = ""
            for tok, sc in acc_candidates:
                if tok.text.strip() != inv_no:
                    acc_no = tok.text.strip()
                    break

            # Date: normalize the best candidate
            inv_date = normalize_date(date_candidates[0][0].text.strip()) if date_candidates else ""

            # Amount: prefer NLP candidate, fall back to table extraction
            amount = ""
            if amount_candidates:
                raw = amount_candidates[0][0].text.strip().replace(',', '')
                try:
                    if float(raw) >= 10:
                        amount = raw
                except ValueError:
                    pass

            if not amount and all_tables:
                amount, _ = extract_amount_from_tables(all_tables)

            # Party name (specialized extractor)
            party_name, _ = extract_party_name(full_text, tokens)

            # Phone number: pick best candidate, must not collide with account number
            phone_no = ""
            for tok, sc in phone_candidates:
                if tok.text.strip() != acc_no:
                    phone_no = tok.text.strip()
                    break

            # ── Inline bank details fallback ──
            # Handles format like: "NAME - X, BANK NAME - Y, BANK ACCOUNT NO - Z, IFSC CODE - W"
            # Only fills in fields that NLP missed
            inline_bank = parse_inline_bank_details(full_text)
            if not acc_no and inline_bank.get("account_no"):
                acc_no = inline_bank["account_no"]
            if not ifsc and inline_bank.get("ifsc"):
                ifsc = inline_bank["ifsc"]

            # PAN/GST preference
            pan_gst = pan if pan else gst

            # Bank name from IFSC (or from inline parser if IFSC missing)
            bank_name = derive_bank_name(ifsc)
            if not bank_name and inline_bank.get("bank_name"):
                bank_name = inline_bank["bank_name"]

            record = {
                "Party name": party_name,
                "Invoice Date": inv_date,
                "Invoice No.": inv_no,
                "Amount": amount,
                "Phone Number": phone_no,
                "Bank Name": bank_name,
                "Bank Account No": acc_no,
                "IFSC Code": ifsc,
                "PAN Number / GST": pan_gst,
            }

            # ── Validate ──
            warnings = validate_extraction(record)

            # ── Debug info ──
            debug_info = ""
            if debug_mode:
                debug_info = (
                    f"--- IFSC candidates: {[(t.text, s) for t, s in ifsc_candidates]}\n"
                    f"--- PAN  candidates: {[(t.text, s) for t, s in pan_candidates]}\n"
                    f"--- GST  candidates: {[(t.text, s) for t, s in gst_candidates]}\n"
                    f"--- Acc  candidates: {[(t.text, s) for t, s in acc_candidates]}\n"
                    f"--- Inv  candidates: {[(t.text, s) for t, s in inv_candidates]}\n"
                    f"--- Date candidates: {[(t.text, s) for t, s in date_candidates]}\n"
                    f"--- Amt  candidates: {[(t.text, s) for t, s in amount_candidates]}\n"
                    f"--- Phn  candidates: {[(t.text, s) for t, s in phone_candidates]}\n"
                    f"--- Party name candidates checked in text\n"
                    f"--- Warnings: {warnings}\n"
                    f"\n--- RAW TEXT (first 3000 chars) ---\n{full_text[:3000]}"
                )

            return record, warnings, debug_info, full_text

    except Exception as e:
        import traceback
        return None, [f"Error: {str(e)}\n{traceback.format_exc()}"], "", ""


# ─────────────────────────────────────────────
# 9.  STREAMLIT UI
# ─────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Invoice PDF → Excel Converter", layout="wide")
    st.title("📄 Invoice PDF → Excel Converter")
    st.markdown("Extract structured data from invoice PDFs using offline NLP entity detection.")

    debug_mode = st.sidebar.checkbox("🔍 Debug Mode", value=False,
                                     help="Show NLP scoring details and raw text")

    uploaded_files = st.file_uploader(
        "Upload Invoice PDFs",
        type=['pdf'],
        accept_multiple_files=True,
        help="Select one or more PDF invoices"
    )

    if not uploaded_files:
        st.info("👆 Please upload one or more invoice PDFs to get started")
        # Sidebar info
        with st.sidebar:
            st.markdown("### 📝 How It Works")
            st.markdown("""
            1. **Tokenize** — PDF text is split into context-aware tokens
            2. **Score** — Each token is scored per entity type using format + context signals
            3. **Validate** — Cross-field checks flag suspicious extractions
            4. **Export** — Clean data exported to Excel
            
            **Supported fields:** Party Name, Invoice No., Date, Amount, Bank Account, IFSC, PAN, GST
            
            **Supported date formats:** DD-Mon-YY, DD.MM.YYYY, DD/MM/YYYY
            """)
        return

    st.success(f"✅ {len(uploaded_files)} PDF(s) uploaded")

    if st.button("🔄 Process Invoices", type="primary"):
        with st.spinner("Processing invoices..."):
            all_data = []
            all_warnings = {}
            all_raw_texts = {}
            failed_files = []

            progress_bar = st.progress(0)
            status_text = st.empty()

            for idx, pdf_file in enumerate(uploaded_files):
                status_text.text(f"Processing: {pdf_file.name}")

                record, warnings, debug_info, raw_text = extract_invoice_data(pdf_file, debug_mode)

                if debug_mode and debug_info:
                    with st.expander(f"🔍 Debug — {pdf_file.name}"):
                        st.text_area("NLP Scores & Raw Text", debug_info, height=400,
                                     key=f"debug_{idx}")

                if record:
                    all_data.append(record)
                    if warnings:
                        all_warnings[pdf_file.name] = warnings
                    if raw_text:
                        all_raw_texts[pdf_file.name] = raw_text
                else:
                    failed_files.append(pdf_file.name)
                    if warnings:
                        st.warning(f"⚠️ {pdf_file.name}: {'; '.join(warnings)}")

                progress_bar.progress((idx + 1) / len(uploaded_files))

            status_text.empty()

            # ── Show validation warnings ──
            if all_warnings:
                with st.expander("⚠️ Validation Warnings", expanded=True):
                    for fname, warns in all_warnings.items():
                        st.markdown(f"**{fname}:**")
                        for w in warns:
                            st.markdown(f"  - ⚠️ {w}")

            if all_data:
                columns = [
                    "Party name", "Invoice Date", "Invoice No.", "Amount",
                    "Phone Number", "Bank Name", "Bank Account No", "IFSC Code",
                    "PAN Number / GST"
                ]
                df = pd.DataFrame(all_data, columns=columns)

                st.success(f"✅ Successfully processed {len(all_data)} invoice(s)")
                if failed_files:
                    st.warning(f"⚠️ Failed: {', '.join(failed_files)}")

                st.subheader("📊 Extracted Data")
                st.dataframe(df, use_container_width=True)

                # ── Excel Export ──
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Invoices')

                    ws = writer.sheets['Invoices']
                    for col_idx, col_name in enumerate(columns):
                        max_len = max(
                            df[col_name].astype(str).apply(len).max(),
                            len(col_name)
                        ) + 2
                        col_letter = chr(65 + col_idx)
                        ws.column_dimensions[col_letter].width = min(max_len, 50)

                    # Add warnings sheet if any exist
                    if all_warnings:
                        warn_rows = []
                        for fname, warns in all_warnings.items():
                            for w in warns:
                                warn_rows.append({"File": fname, "Warning": w})
                        warn_df = pd.DataFrame(warn_rows)
                        warn_df.to_excel(writer, index=False, sheet_name='Warnings')
                        ws2 = writer.sheets['Warnings']
                        ws2.column_dimensions['A'].width = 35
                        ws2.column_dimensions['B'].width = 60

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                st.download_button(
                    label="⬇️ Download Excel File",
                    data=output.getvalue(),
                    file_name=f"invoices_{timestamp}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

                # If there were warnings, offer a debug text file download
                if all_warnings and all_raw_texts:
                    debug_txt = ""
                    for fname, raw in all_raw_texts.items():
                        debug_txt += f"{'='*60}\n"
                        debug_txt += f"FILE: {fname}\n"
                        debug_txt += f"{'='*60}\n"
                        debug_txt += raw + "\n\n"
                    st.download_button(
                        label="⬇️ Download Debug Text (for troubleshooting)",
                        data=debug_txt,
                        file_name=f"debug_raw_text_{timestamp}.txt",
                        mime="text/plain",
                        use_container_width=True
                    )
            else:
                st.error("❌ No data could be extracted from any uploaded PDFs.")
                st.info("💡 Make sure PDFs are text-based (not scanned images).")

    # Footer
    st.markdown("---")
    st.markdown(
        "**Pipeline:** PDF → Text Extract → Tokenize → NLP Entity Scoring → "
        "Cross-Field Validation → Excel Output"
    )


if __name__ == "__main__":
    main()
