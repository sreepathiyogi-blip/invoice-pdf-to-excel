import streamlit as st
import pdfplumber
import pandas as pd
import re
from io import BytesIO
from datetime import datetime


# ─────────────────────────────────────────────
# 1.  PRE-PROCESSOR
# ─────────────────────────────────────────────

def preprocess_text(text):
    """
    Normalise raw PDF text before tokenizing.  Fixes:
      • Phone: strip parens + country code  (91-858...) → 858...
      • Amount: strip /- suffix, strip Rs./₹/INR prefix, strip parens
      • Account: collapse spaces / dashes / dots into pure digits on label lines
    """
    lines = text.split("\n")
    out = []

    for line in lines:
        # ── Phone: parens + country code ──
        # (91-XXXXXXXXXX) or (+91-XXXXXXXXXX)
        line = re.sub(r'\(\+?91[-\s]?([6-9]\d{9})\)', r'\1', line)
        # +91 XXXXXXXXXX (no parens, with or without space)
        line = re.sub(r'\+91[-\s]?([6-9]\d{9})', r'\1', line)
        # 91-XXXXXXXXXX bare, only near phone keywords
        if re.search(r'phone|mob|tel|call|contact', line, re.IGNORECASE):
            line = re.sub(r'\b91[-\s]?([6-9]\d{9})\b', r'\1', line)

        # ── Amount: strip trailing /- ──
        line = re.sub(r'([\d,\.]+)\s*/\s*[-–—]', r'\1', line)

        # ── Amount: strip Rs./₹/INR prefix glued to number ──
        line = re.sub(r'(?:Rs\.?|₹|INR)\s*([\d,\.]+)', r'\1', line)

        # ── Amount: strip surrounding parentheses ──
        line = re.sub(r'\(([\d,\.]+)\)', r'\1', line)

        # ── Date: collapse spaces around separators ──
        # "28 / 11 / 2025" → "28/11/2025"   "28 - Nov - 25" → "28-Nov-25"
        line = re.sub(
            r'\b(\d{1,2})\s*([/\-.])\s*([A-Za-z]{3,9}|\d{1,2})\s*([/\-.])\s*(\d{2,4})\b',
            lambda m: m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5),
            line
        )

        # ── Account: collapse spaces / dashes / dots into pure digits ──
        # Fires on label lines AND on any line where a spaced-out digit group
        # looks like a bank account (total 9-18 digits with internal separators)
        if re.search(r'account|a\s*/?\s*c|acc|beneficiary|pay\s+to|transfer', line, re.IGNORECASE):
            def collapse_digits(m):
                digits = re.sub(r'[\s\-\.]', '', m.group(0))
                if 9 <= len(digits) <= 18:
                    return digits
                return m.group(0)
            line = re.sub(r'\d[\d\s\-\.]{8,25}\d', collapse_digits, line)
        else:
            # Even without a label keyword, collapse if it's a standalone spaced number
            # that totals 9-18 digits (e.g. "4500 1011 0017 123" on its own line).
            # GUARD: never collapse if the line contains a date pattern (DD.MM.YYYY etc.)
            stripped = line.strip()
            has_date = re.search(r'\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}', stripped)
            if not has_date and re.match(r'^[\d\s\-\.]+$', stripped):
                digits_only = re.sub(r'[\s\-\.]', '', stripped)
                if 9 <= len(digits_only) <= 18 and digits_only.isdigit():
                    line = digits_only

        out.append(line)

    return "\n".join(out)


# ─────────────────────────────────────────────
# 2.  TOKENIZER
# ─────────────────────────────────────────────

class Token:
    __slots__ = ("text", "pos", "line_idx", "line", "prev_tokens", "next_tokens")
    def __init__(self, text, pos, line_idx, line, prev_tokens, next_tokens):
        self.text = text
        self.pos = pos
        self.line_idx = line_idx
        self.line = line
        self.prev_tokens = prev_tokens
        self.next_tokens = next_tokens


def _split_glued(token_text):
    """
    Split label-separator-value tokens.  Separators: dash, en-dash, em-dash, colon.

    PROTECTED patterns (never split):
      • Dates:      28-Nov-25, 05-Dec-2024, 01-Jan-25
      • Phases:     PHASE-III, PHASE-IV
      • Hyphenated: e-mail, well-known, self-employed
      • Numbers:    3,000.00, 10,000/-

    Examples that DO split:
      'no.-450010110017123'  → ['no.', '450010110017123']
      'Code-BKID0004500'    → ['Code', 'BKID0004500']
      'IFSC:BKID0004500'    → ['IFSC', 'BKID0004500']
      'No:-06AAFCI1834E1ZX' → ['No', '06AAFCI1834E1ZX']
    """
    # ── Phone in parens: (+91-XXXXXXXXXX) / (91-XXXXXXXXXX) / (XXXXXXXXXX) ──
    m = re.match(r'^\(\+?(?:91[-\s]?)?([6-9]\d{9})\)$', token_text)
    if m:
        return [m.group(1)]

    # ── Guard: protect known unsplittable patterns ──

    # Date: DD-Mon-YY or DD-MonthName-YYYY (digits-letters-digits)
    if re.match(r'^\d{1,2}[-–—][A-Za-z]{3,9}[-–—]\d{2,4}$', token_text):
        return [token_text]

    # PHASE-III / PHASE-IV style
    if re.match(r'^PHASE[-–—][IVX]+$', token_text, re.IGNORECASE):
        return [token_text]

    # Common hyphenated English words (short-short pattern, both sides alphabetic, neither side is a known label)
    if re.match(r'^[A-Za-z]{2,8}[-][A-Za-z]{2,8}$', token_text):
        # Only protect if neither side looks like a known invoice label
        left = token_text.split('-')[0].lower()
        known_labels = {'no', 'code', 'ifsc', 'pan', 'gst', 'tin', 'acc', 'name',
                        'bank', 'branch', 'date', 'invoice', 'bill', 'ref', 'amount'}
        if left not in known_labels:
            return [token_text]

    # ── Main split logic ──
    # Label ends with letter or period; value starts with letter or digit
    m = re.match(r'^(.*?[A-Za-z.])[-–—:]+([A-Za-z0-9].*)$', token_text)
    if m:
        label = m.group(1).strip()
        value = m.group(2).strip()
        parts = []
        if label:
            parts.append(label)
        if value:
            parts.append(value)
        return parts if parts else [token_text]

    # Trailing separator only: 'Name-' or 'Code:'
    m2 = re.match(r'^(.*?[A-Za-z.])[-–—:]+$', token_text)
    if m2:
        label = m2.group(1).strip()
        return [label] if label else [token_text]

    return [token_text]


def tokenize(text, context_window=5):
    tokens = []
    lines = text.split("\n")
    char_offset = 0

    for line_idx, line in enumerate(lines):
        parts = re.split(r'(\s+)', line)
        raw_tokens = [p for p in parts if p.strip()]

        # Expand glued tokens
        line_tokens_text = []
        for t in raw_tokens:
            line_tokens_text.extend(_split_glued(t))

        for tok_idx, tok_text in enumerate(line_tokens_text):
            pos = text.find(tok_text, char_offset)
            if pos == -1:
                pos = char_offset

            prev = line_tokens_text[max(0, tok_idx - context_window):tok_idx]
            nxt = line_tokens_text[tok_idx + 1:tok_idx + 1 + context_window]

            tokens.append(Token(
                text=tok_text, pos=pos, line_idx=line_idx, line=line,
                prev_tokens=prev, next_tokens=nxt
            ))
            char_offset = pos + len(tok_text)

        char_offset += len(line) + 1

    return tokens


# ─────────────────────────────────────────────
# 3.  ENTITY DEFINITIONS
# ─────────────────────────────────────────────

ENTITY_DEFS = {
    "IFSC": {
        "format": re.compile(r'^[A-Z]{4}[0-9]{7}$', re.IGNORECASE),
        "context_keywords": {
            "ifsc": 8, "code": 3, "bank": 4, "branch": 3
        },
    },
    "PAN": {
        "format": re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$', re.IGNORECASE),
        "context_keywords": {
            "pan": 9, "permanent": 5, "account": 3, "number": 2, "tin": 4
        },
    },
    "GST": {
        # 15-char GSTIN: 2digits + 5letters + 4digits + 1letter + 1alphanumeric + Z + 1alphanumeric
        "format": re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][Z][0-9A-Z]$', re.IGNORECASE),
        "context_keywords": {
            "gst": 9, "gstin": 10, "tin": 5, "tax": 4, "identification": 3, "no": 2
        },
    },
    "ACCOUNT_NUMBER": {
        "format": re.compile(r'^\d{9,18}$'),
        "context_keywords": {
            "account": 8, "number": 4, "ac": 7, "a/c": 9, "acc": 7,
            "acct": 7, "bank": 5, "holder": 3, "no": 3, "no.": 3,
            "beneficiary": 4, "credit": 3, "saving": 3, "current": 3,
            "pay": 2, "transfer": 3
        },
    },
    "INVOICE_NUMBER": {
        # Single digit allowed — many small invoices use "1"
        "format": re.compile(r'^\d{1,7}$'),
        "context_keywords": {
            "invoice": 9, "no": 5, "number": 4, "bill": 6, "#": 5, "dated": 3,
            "ref": 4, "reference": 4
        },
    },
    "AMOUNT": {
        # Comma-formatted, decimal, OR bare integer ≥ 2 digits.
        # Bare integers rely on strong context keywords to outscore other entity types.
        "format": re.compile(r'^(\d{1,3}(,\d{3})+(\.\d{1,2})?|\d+\.\d{1,2}|\d{2,})$'),
        "context_keywords": {
            "amount": 9, "total": 7, "grand": 5, "net": 4, "payable": 6,
            "balance": 5, "due": 4, "rs": 3, "inr": 3, "₹": 4, "rate": 2,
            "rs.": 3, "charges": 3, "chargeable": 4
        },
    },
    "DATE": {
        # DD-Mon-YY, DD/MM/YYYY, DD.MM.YYYY, YYYY-MM-DD
        "format": re.compile(
            r'^(\d{1,2}[-./]\s*([A-Za-z]{3,9}|\d{1,2})\s*[-./]\d{2,4}|\d{4}[-./]\d{1,2}[-./]\d{1,2})$'
        ),
        "context_keywords": {
            "date": 8, "dated": 9, "invoice": 4, "on": 2, "day": 3
        },
    },
    "PHONE_NUMBER": {
        # Indian mobile: 10 digits starting 6/7/8/9
        "format": re.compile(r'^[6-9]\d{9}$'),
        "context_keywords": {
            "phone": 9, "ph": 7, "tel": 7, "mobile": 9, "mob": 8,
            "cell": 6, "contact": 5, "fax": 5, "whatsapp": 7,
            "helpline": 4, "toll": 4, "call": 4,
            "phone:": 9, "ph:": 7, "tel:": 7, "mobile:": 9, "mob:": 8,
            "fax:": 5, "contact:": 5, "whatsapp:": 7,
        },
    }
}


# ─────────────────────────────────────────────
# 4.  NEGATIVE CONTEXT
# ─────────────────────────────────────────────

NEGATIVE_CONTEXT = {
    "phone", "ph", "ph.", "tel", "tel.", "mobile", "mob", "mob.",
    "cell", "contact", "fax", "fax.", "helpline", "toll", "whatsapp",
    "call", "sms", "isd", "std",
    "phone:", "ph:", "tel:", "mobile:", "mob:", "fax:", "contact:",
    "whatsapp:", "helpline:",
}


# ─────────────────────────────────────────────
# 5.  NLP SCORER
# ─────────────────────────────────────────────

def score_token_for_entity(token, entity_type):
    edef = ENTITY_DEFS[entity_type]

    # Strip trailing punctuation like periods from token before format check
    clean = token.text.strip().rstrip('.')
    # But keep the original for period-ending checks
    raw = token.text.strip()

    # Try both raw and cleaned against format
    if not (edef["format"].match(raw) or edef["format"].match(clean)):
        return 0  # Hard gate

    score = 50  # Base for format match

    surrounding = [t.lower().rstrip('.') for t in token.prev_tokens + token.next_tokens]
    line_words = [w.lower().rstrip('.') for w in token.line.split()]
    all_context = set(surrounding + line_words)

    # Negative context kills non-phone entities
    if all_context & NEGATIVE_CONTEXT and entity_type != "PHONE_NUMBER":
        return 0

    # Positive context
    for keyword, weight in edef["context_keywords"].items():
        kw = keyword.lower().rstrip('.')
        if kw in surrounding:
            score += weight
            if token.prev_tokens and token.prev_tokens[-1].lower().rstrip('.').startswith(kw):
                score += 5
            if token.next_tokens and token.next_tokens[0].lower().rstrip('.').startswith(kw):
                score += 5
        elif kw in line_words:
            score += weight * 0.4

    # Cross-entity penalty
    for other_type, odef in ENTITY_DEFS.items():
        if other_type == entity_type:
            continue
        if odef["format"].match(raw) or odef["format"].match(clean):
            other_score = 0
            for kw, w in odef["context_keywords"].items():
                if kw.lower().rstrip('.') in surrounding or kw.lower().rstrip('.') in line_words:
                    other_score += w
            if other_score > 5:
                score -= 20

    # Special: single-digit invoice numbers need strong context to win
    if entity_type == "INVOICE_NUMBER" and len(clean) == 1:
        score -= 20  # penalise heavily; only survives with strong "invoice"/"no" context

    return max(score, 0)


def find_entity(tokens, entity_type, top_n=3):
    candidates = []
    seen = set()
    for token in tokens:
        clean = token.text.strip()
        if clean in seen:
            continue
        seen.add(clean)
        s = score_token_for_entity(token, entity_type)
        if s > 0:
            candidates.append((token, s))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:top_n]


# ─────────────────────────────────────────────
# 6.  SPECIALIZED EXTRACTORS
# ─────────────────────────────────────────────

def extract_party_name(text, tokens):
    """
    Multi-strategy party name extraction (priority order):
      1. 'Account Holder' label
      2. 'Payee' / 'Beneficiary' / 'Supplier' / 'From' / 'Raised by' / 'Prepared by'
      3. NAME inside inline bank details line
      4. 'Bill To:' label  (extract the value after it)
      5. Name right after INVOICE heading
      6. Capitalised name in header lines 0–8
    """
    candidates = []
    lines = text.split("\n")

    # --- 1: Account Holder ---
    for i, line in enumerate(lines):
        if re.search(r'account\s+holder', line, re.IGNORECASE):
            for j in range(i, min(i + 3, len(lines))):
                c = re.sub(r'(account\s+holder|name|:|-)', '', lines[j], flags=re.IGNORECASE).strip()
                if c and re.match(r"^[A-Za-z][A-Za-z\s\.&'\-\d]*$", c) and 3 < len(c) < 80:
                    candidates.append((c, 50 + (10 if c.isupper() else 0)))

    # --- 2: Payee / Beneficiary / Supplier / From / Raised by / Prepared by ---
    for line in lines:
        m = re.search(
            r'(?:payee|beneficiary|supplier|from|raised\s+by|prepared\s+by)\s*[:\-]?\s*'
            r"([A-Za-z][A-Za-z\s\.&,'\-\d]+)",
            line, re.IGNORECASE
        )
        if m:
            c = m.group(1).strip().rstrip(',')
            if 3 < len(c) < 80:
                candidates.append((c, 42))

    # --- 3: NAME inside inline bank detail ---
    for line in lines:
        if re.search(r'bank\s+details|your\s+bank', line, re.IGNORECASE):
            m = re.search(r"NAME\s*[-–—:]\s*([A-Za-z][A-Za-z\s\.&'\-\d]+?)(?:,|$)", line, re.IGNORECASE)
            if m:
                c = m.group(1).strip()
                if 3 < len(c) < 80:
                    candidates.append((c, 38))

    # --- 4: Bill To: label ---
    for i, line in enumerate(lines):
        m = re.search(r"Bill\s+[Tt]o\s*[:\-]?\s*([A-Za-z][A-Za-z\s\.&,'\-\d]+)", line, re.IGNORECASE)
        if m:
            c = m.group(1).strip().rstrip(',')
            if 3 < len(c) < 80:
                candidates.append((c, 36))
            else:
                # Value might be on the next line
                for j in range(i + 1, min(i + 3, len(lines))):
                    c2 = lines[j].strip()
                    if c2 and re.match(r"^[A-Za-z][A-Za-z\s\.&,'\-\d]+$", c2) and 3 < len(c2) < 80:
                        candidates.append((c2, 34))
                        break

    # --- 5: Name after INVOICE heading ---
    for i, line in enumerate(lines):
        if re.match(r'^INVOICE\b', line.strip(), re.IGNORECASE):
            for j in range(i + 1, min(i + 3, len(lines))):
                c = lines[j].strip()
                if c and re.match(r"^[A-Z][A-Za-z\s\.&'\-\d]+$", c) and 3 < len(c) < 60:
                    candidates.append((c, 40))
                    break

    # --- 6: Header lines 0–8 (capitalised, no label keywords) ---
    skip_words = ['invoice', 'phone', 'email', 'address', 'gst', 'pan',
                  'date', 'total', 'amount', 'bank', 'ifsc',
                  'place', 'supply', 'description', 'service',
                  'bill to', 'ship to', 'payee', 'beneficiary']
    for i, line in enumerate(lines[:9]):
        line_s = line.strip()
        if not line_s:
            continue
        if re.match(r"^[A-Z][A-Za-z\s\.&,'\-\d]+$", line_s) and 3 < len(line_s) < 60:
            if any(sw in line_s.lower() for sw in skip_words):
                continue
            score = 25 + (8 if i <= 2 else 0) + (5 if line_s.isupper() else 0)
            candidates.append((line_s, score))

    # Words/phrases that are NEVER valid party names
    PARTY_BLOCKLIST = [
        'bank of india', 'hdfc bank', 'icici bank', 'sbi', 'axis bank',
        'kotak mahindra bank', 'punjab national bank', 'union bank',
        'bank of baroda', 'canara bank', 'indusind bank', 'yes bank',
        'federal bank', 'bandhan bank', 'rbl bank',
        'account', 'amount', 'total', 'invoice', 'bank', 'ifsc',
        'description', 'particulars', 'remarks', 'authorised signatory',
        'authorized signatory', 'signature',
    ]

    def is_blocked(name):
        n = name.lower().strip()
        return any(n == b or n.startswith(b) for b in PARTY_BLOCKLIST)

    # Filter all candidates through blocklist before returning
    candidates = [(n, s) for n, s in candidates if not is_blocked(n)]

    if not candidates:
        return "", 0
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]


def extract_alphanumeric_invoice(text):
    """
    Catch alphanumeric invoice numbers the pure-digit scorer misses:
      INV-1024, INV/2025/001, INV#1024, BILL-2025-99, REF-1001
      Invoice No. ABC-123, Bill No: 7
    Never returns plain words like 'Dated'.
    """
    LABEL_WORDS = {'dated', 'date', 'no', 'number', 'amount', 'total',
                   'description', 'particulars', 'service', 'qty', 'rate',
                   'tax', 'gst', 'pan', 'ifsc', 'bank', 'account'}

    patterns = [
        # INV/INVOICE/BILL/REF prefix → digits with optional / or -
        r'(?:INV(?:OICE)?|BILL|REF)\s*[-/#:]?\s*(\d[\d/\-]*\d)',
        # "Invoice No" or "Bill No" → code that must contain at least one digit
        r'(?:Invoice|Bill)\s+No\.?\s*[:\-]?\s*([A-Z0-9][\w/\-]{0,20})',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip('/-')
            # Must contain a digit and not be a blocked label word
            if val and re.search(r'\d', val) and val.lower() not in LABEL_WORDS:
                return val
    return ""


def extract_bare_amount(text):
    """
    Catch amounts that are bare integers OR comma-formatted but didn't score
    via NLP (e.g. near Total/Amount labels without being in a scored context).
    Handles: 'Total 10000', 'Total 10,000', 'Amount: ₹50000', 'Amount Chargeable 3000'
    """
    patterns = [
        # Grand Total / Total (with optional currency prefix)
        r'(?:Grand\s+)?Total\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)\b',
        # Net Amount / Amount
        r'(?:Net\s+)?Amount\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)\b',
        # Amount Payable
        r'Amount\s+Payable\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)\b',
        # Amount Chargeable
        r'Amount\s+Chargeable\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)\b',
        # Balance Due / Balance Amount
        r'Balance\s+(?:Due|Amount)\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)\b',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).replace(',', '')
            try:
                if float(val) >= 10:
                    return val
            except ValueError:
                continue
    return ""


def extract_amount_from_tables(tables):
    """
    Table-aware amount extraction.  Finds Amount/Total column headers,
    collects all numeric values below, scores by format quality.
    Prefers the largest value (usually the total row).
    """
    candidates = []

    for table in tables:
        if not table:
            continue

        # Find ALL amount-related column indices (don't stop at first)
        amount_col_indices = set()
        for row in table:
            if not row:
                continue
            for col_idx, cell in enumerate(row):
                if cell and re.search(r'amount|total', str(cell), re.IGNORECASE):
                    amount_col_indices.add(col_idx)

        # Pull values from every amount column
        for row in table:
            if not row:
                continue
            for col_idx in amount_col_indices:
                if col_idx < len(row) and row[col_idx]:
                    raw = str(row[col_idx]).strip()
                    # Strip /- suffix and currency prefixes
                    raw = re.sub(r'/[-–—]', '', raw)
                    raw = re.sub(r'(?:Rs\.?|₹|INR)\s*', '', raw)
                    raw = re.sub(r'[^\d,.]', '', raw)
                    if not raw:
                        continue
                    try:
                        val = float(raw.replace(',', ''))
                        if 10 <= val < 1e8:
                            score = 10
                            if '.' in raw:
                                score += 15
                            if re.match(r'^\d{1,3}(,\d{3})*\.\d{2}$', raw):
                                score += 20
                            candidates.append((raw.replace(',', ''), score, val))
                    except ValueError:
                        continue

    if not candidates:
        return "", 0
    # Sort by score desc, then value desc (totals are usually largest)
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return candidates[0][0], candidates[0][1]


# ─────────────────────────────────────────────
# 7.  DATE NORMALIZER
# ─────────────────────────────────────────────

MONTH_MAP = {
    'jan': '01', 'january': '01',
    'feb': '02', 'february': '02',
    'mar': '03', 'march': '03',
    'apr': '04', 'april': '04',
    'may': '05',
    'jun': '06', 'june': '06',
    'jul': '07', 'july': '07',
    'aug': '08', 'august': '08',
    'sep': '09', 'sept': '09', 'september': '09',
    'oct': '10', 'october': '10',
    'nov': '11', 'november': '11',
    'dec': '12', 'december': '12',
}


def normalize_date(date_str):
    """Normalize any detected date to DD-MM-YYYY."""
    if not date_str:
        return ""

    # YYYY-MM-DD (ISO)
    m = re.match(r'(\d{4})[-./](\d{1,2})[-./](\d{1,2})$', date_str)
    if m:
        year = m.group(1)
        month = m.group(2).zfill(2)
        day = m.group(3).zfill(2)
        return f"{day}-{month}-{year}"

    # DD-MonthName-YY/YYYY
    m = re.match(r'(\d{1,2})[-./]\s*([A-Za-z]{3,9})\s*[-./](\d{2,4})', date_str)
    if m:
        day = m.group(1).zfill(2)
        month = MONTH_MAP.get(m.group(2).lower(), '01')
        year = m.group(3)
        if len(year) == 2:
            year = '20' + year
        return f"{day}-{month}-{year}"

    # DD-MM-YY/YYYY
    m = re.match(r'(\d{1,2})[-./]\s*(\d{1,2})\s*[-./](\d{2,4})', date_str)
    if m:
        day = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        year = m.group(3)
        if len(year) == 2:
            year = '20' + year
        return f"{day}-{month}-{year}"

    return date_str


def extract_full_month_date(text):
    """
    Catch space-separated dates with full/partial month names:
      Day-first:   '28 November 2025', '1 Nov 2025', '28 November 25'
      Month-first: 'Nov 28, 2025', 'November 28, 2025', 'Nov 28 2025'
    Prefers dates near 'date'/'dated'/'invoice' labels.
    Falls back to last match (invoice dates tend to be later in doc).
    """
    # Collect all matches — both orderings
    patterns = [
        # Day Month Year:  28 Nov 2025
        (r'\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})\b', 'dmy'),
        # Month Day, Year: Nov 28, 2025  /  November 28 2025
        (r'\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{2,4})\b', 'mdy'),
    ]

    best = None
    best_score = -1

    for pattern, order in patterns:
        for m in re.finditer(pattern, text):
            if order == 'dmy':
                day_s, month_s, year_s = m.group(1), m.group(2), m.group(3)
            else:  # mdy
                month_s, day_s, year_s = m.group(1), m.group(2), m.group(3)

            month_key = month_s.lower()
            if month_key not in MONTH_MAP:
                continue

            # Score: prefer those near date/invoice keywords
            ctx = text[max(0, m.start() - 100):m.start()].lower()
            score = 0
            if 'date' in ctx or 'dated' in ctx:
                score += 10
            if 'invoice' in ctx:
                score += 5
            score += m.start() * 0.0001  # tiebreaker: later in doc wins

            if score > best_score:
                best_score = score
                best = (day_s, month_key, year_s)

    if best:
        day, month_key, year = best
        day = day.zfill(2)
        month = MONTH_MAP[month_key]
        if len(year) == 2:
            year = '20' + year
        return f"{day}-{month}-{year}"
    return ""


# ─────────────────────────────────────────────
# 8.  BANK NAME LOOKUP
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
    "BAND": "Bandhan Bank",
    "FEDL": "Federal Bank",
    "SIBL": "South Indian Bank",
    "KVBL": "KVB Bank",
    "TMBL": "TMB Bank",
    "CITI": "Citibank",
    "HSBC": "HSBC",
    "AUBL": "AU Small Finance Bank",
    "ESAF": "ESAF Small Finance Bank",
    "DCBL": "DCB Bank",
    "IBKL": "IDBI Bank",
    "UCOB": "UCO Bank",
    "JKBK": "J&K Bank",
    "PMCB": "PMC Bank",
    "NWOS": "North Western Co-op Bank",
    "MHCB": "Maharashtra Co-op Bank",
    "FIBL": "First International Bank",
}


def derive_bank_name(ifsc):
    if not ifsc or len(ifsc) < 4:
        return ""
    prefix = ifsc[:4].upper()
    return IFSC_PREFIX_MAP.get(prefix, prefix)


# ─────────────────────────────────────────────
# 9.  BANK DETAILS FALLBACK PARSER
# ─────────────────────────────────────────────

def parse_bank_details_fallback(text):
    """
    Catches bank details in every common Indian invoice format.
    Returns dict with any of: account_no, ifsc, bank_name.

    Formats handled:
      A) Single-line comma-separated:
           NAME - X, BANK NAME - AXIS BANK, BANK ACCOUNT NO - 123, IFSC CODE - UTIB0000378
      B) Multiline label-value (dash / colon / space):
           Account no.- 450010110017123
           IFSC Code: BKID0004500
           Bank Name- Bank of India
      C) All label variants:
           Ac No / A/C / A.C / Acct No / Account Number
           Saving A/C / Current A/C
           Beneficiary Account / Credit Account
      D) Pay to / Transfer to:
           Pay to: 450010110017123
      E) Bare IFSC anywhere in text (last resort)
    """
    result = {}

    # ── Account number ──
    acc_patterns = [
        # Explicit "BANK ACCOUNT NO"
        r'BANK\s+ACCOUNT\s+NO\.?\s*[-–—:]\s*(\d{9,18})',
        # "Account Number/No" with separator
        r'Account\s+(?:Number|No\.?)\s*[-–—:]\s*(\d{9,18})',
        # Saving/Current A/C
        r'(?:Saving|Current)\s+A\s*/?\s*C\s*(?:No\.?|Number)?\s*[-–—:]\s*(\d{9,18})',
        # A/C or AC or A.C variants
        r'A\s*[/\.]\s*C\s*(?:No\.?|Number)?\s*[-–—:]\s*(\d{9,18})',
        # Acc / Acct
        r'Acc(?:ount|t)?\s*(?:No\.?|Number)?\s*[-–—:]\s*(\d{9,18})',
        # Beneficiary Account
        r'Beneficiary\s+(?:Account|A\s*/?\s*C)\s*(?:No\.?)?\s*[-–—:]\s*(\d{9,18})',
        # Credit Account
        r'Credit\s+(?:Account|A\s*/?\s*C)\s*(?:No\.?)?\s*[-–—:]\s*(\d{9,18})',
        # Pay to / Transfer to
        r'(?:Pay|Transfer)\s+[Tt]o\s*[:\-]?\s*(\d{9,18})',
        # Last resort: any account/ac/acc keyword followed (loosely) by digits
        r'(?:account|a\s*/?\s*c|acc)\D{0,15}(\d{9,18})',
        # Spaced / dashed / dotted account number after a label
        # e.g. "Account no. 4500 1011 0017 123" — collapse and validate length
        r'(?:account|a\s*/?\s*c|acc)\D{0,15}([\d][\d\s\-\.]{8,25}[\d])',
    ]
    for p in acc_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            # Collapse any internal spaces/dashes/dots
            digits = re.sub(r'[\s\-\.]', '', raw)
            if 9 <= len(digits) <= 18 and digits.isdigit():
                result["account_no"] = digits
                break

    # ── IFSC ──
    ifsc_patterns = [
        r'IFSC\s*(?:Code)?\s*[-–—:]\s*([A-Z]{4}\d{7})',
        r'IFSC\s*[-–—:]?\s*([A-Z]{4}\d{7})',
        # Bare IFSC anywhere (last resort)
        r'\b([A-Z]{4}\d{7})\b',
    ]
    for p in ifsc_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            result["ifsc"] = m.group(1).upper().strip()
            break

    # ── Bank name ──
    bank_patterns = [
        r'BANK\s+NAME\s*[-–—:]\s*([A-Za-z\s\.&]+?)(?:,|\n|$)',
        r'Bank\s+Name\s*[-–—:]\s*([A-Za-z\s\.&]+?)(?:,|\n|$)',
        # Just "Bank:" or "Bank-" followed by name
        r'Bank\s*[-–—:]\s*([A-Za-z][A-Za-z\s\.&]+?)(?:,|\n|$)',
    ]
    for p in bank_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip(',')
            if len(name) > 2:
                result["bank_name"] = name
                break

    return result


# ─────────────────────────────────────────────
# 10. VALIDATION ENGINE
# ─────────────────────────────────────────────

def validate_extraction(record):
    warnings = []

    if record.get("Amount") and not record.get("Bank Account No"):
        warnings.append("Amount found but no Bank Account Number detected")

    if record.get("Bank Account No") and not record.get("IFSC Code"):
        warnings.append("Account Number found but no IFSC Code detected")

    # IFSC sanity: must be 4 letters + 7 digits
    ifsc = record.get("IFSC Code", "")
    if ifsc and not re.match(r'^[A-Z]{4}\d{7}$', ifsc):
        warnings.append(f"IFSC Code '{ifsc}' doesn't match expected format (4 letters + 7 digits)")

    inv = record.get("Invoice No.", "")
    if inv and len(inv) > 7:
        warnings.append(f"Invoice No. '{inv}' is unusually long — may be misclassified")

    # Duplicate value check: same number in acc + phone
    acc = record.get("Bank Account No", "")
    phone = record.get("Phone Number", "")
    if acc and phone and acc == phone:
        warnings.append("Bank Account No and Phone Number are identical — one may be wrong")

    if not record.get("Party name"):
        warnings.append("Party name could not be detected")

    if not record.get("Invoice Date"):
        warnings.append("Invoice Date could not be detected")

    if not record.get("Amount"):
        warnings.append("Amount could not be detected")

    return warnings


# ─────────────────────────────────────────────
# 11. MAIN EXTRACTION PIPELINE
# ─────────────────────────────────────────────

def extract_invoice_data(pdf_file, debug_mode=False):
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
                return None, ["No text extracted — PDF may be scanned/image-based"], "", ""

            # ── Pre-process ──
            cleaned = preprocess_text(full_text)

            # ── Tokenize ──
            tokens = tokenize(cleaned, context_window=5)

            # ── NLP Entity Detection ──
            ifsc_cands   = find_entity(tokens, "IFSC")
            pan_cands    = find_entity(tokens, "PAN")
            gst_cands    = find_entity(tokens, "GST")
            acc_cands    = find_entity(tokens, "ACCOUNT_NUMBER")
            inv_cands    = find_entity(tokens, "INVOICE_NUMBER")
            date_cands   = find_entity(tokens, "DATE")
            amt_cands    = find_entity(tokens, "AMOUNT")
            phone_cands  = find_entity(tokens, "PHONE_NUMBER")

            # ── Pick best per field ──
            ifsc  = ifsc_cands[0][0].text.upper().strip() if ifsc_cands else ""
            pan   = pan_cands[0][0].text.upper().strip() if pan_cands else ""
            gst   = gst_cands[0][0].text.upper().strip() if gst_cands else ""

            # Invoice: NLP → alphanumeric fallback
            # Sanity: reject NLP pick if it has no real invoice context
            inv_no = ""
            if inv_cands:
                top_tok, top_score = inv_cands[0]
                candidate = top_tok.text.strip()
                near_amount = re.search(
                    r'(?:total|amount|grand|net|balance)\s*[:\-]?\s*' + re.escape(candidate),
                    cleaned, re.IGNORECASE
                )
                # Strong context (keywords boosted score above 50)
                if top_score > 50 and not near_amount:
                    inv_no = candidate
                # Single/double digit: allow if previous line has invoice/bill header
                elif len(candidate) <= 2 and not near_amount:
                    lines = cleaned.split("\n")
                    line_idx = top_tok.line_idx
                    prev_line = lines[line_idx - 1].lower() if line_idx > 0 else ""
                    if re.search(r'invoice|bill\s+no|inv\s+no', prev_line):
                        inv_no = candidate
            if not inv_no:
                inv_no = extract_alphanumeric_invoice(cleaned)

            # Account: skip if same as invoice number
            acc_no = ""
            for tok, sc in acc_cands:
                if tok.text.strip() != inv_no:
                    acc_no = tok.text.strip()
                    break

            # Date: NLP → full-month fallback
            inv_date = normalize_date(date_cands[0][0].text.strip()) if date_cands else ""
            if not inv_date:
                inv_date = extract_full_month_date(cleaned)

            # Amount: NLP → table → bare fallback
            amount = ""
            if amt_cands:
                raw = amt_cands[0][0].text.strip().replace(',', '')
                try:
                    if float(raw) >= 10:
                        amount = raw
                except ValueError:
                    pass
            if not amount and all_tables:
                amount, _ = extract_amount_from_tables(all_tables)
            if not amount:
                amount = extract_bare_amount(cleaned)

            # Party name
            party_name, _ = extract_party_name(cleaned, tokens)

            # Phone: skip if same as account number
            phone_no = ""
            for tok, sc in phone_cands:
                if tok.text.strip() != acc_no:
                    phone_no = tok.text.strip()
                    break

            # ── Bank details fallback (fills gaps NLP missed) ──
            fallback = parse_bank_details_fallback(cleaned)
            if not acc_no and fallback.get("account_no"):
                acc_no = fallback["account_no"]
            if not ifsc and fallback.get("ifsc"):
                ifsc = fallback["ifsc"]

            # Bank name: IFSC lookup → fallback parser
            bank_name = derive_bank_name(ifsc)
            if not bank_name and fallback.get("bank_name"):
                bank_name = fallback["bank_name"]

            # PAN preferred over GST
            pan_gst = pan if pan else gst

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

            warnings = validate_extraction(record)

            debug_info = ""
            if debug_mode:
                debug_info = (
                    f"--- IFSC candidates:  {[(t.text, s) for t, s in ifsc_cands]}\n"
                    f"--- PAN  candidates:  {[(t.text, s) for t, s in pan_cands]}\n"
                    f"--- GST  candidates:  {[(t.text, s) for t, s in gst_cands]}\n"
                    f"--- Acc  candidates:  {[(t.text, s) for t, s in acc_cands]}\n"
                    f"--- Inv  candidates:  {[(t.text, s) for t, s in inv_cands]}\n"
                    f"--- Date candidates:  {[(t.text, s) for t, s in date_cands]}\n"
                    f"--- Amt  candidates:  {[(t.text, s) for t, s in amt_cands]}\n"
                    f"--- Phone candidates: {[(t.text, s) for t, s in phone_cands]}\n"
                    f"--- Alphanumeric inv: '{extract_alphanumeric_invoice(cleaned)}'\n"
                    f"--- Full-month date:  '{extract_full_month_date(cleaned)}'\n"
                    f"--- Bare amount:      '{extract_bare_amount(cleaned)}'\n"
                    f"--- Bank fallback:    {fallback}\n"
                    f"--- Warnings:         {warnings}\n"
                    f"\n--- RAW TEXT (first 3000 chars) ---\n{full_text[:3000]}"
                )

            return record, warnings, debug_info, full_text

    except Exception as e:
        import traceback
        return None, [f"Error: {str(e)}\n{traceback.format_exc()}"], "", ""


# ─────────────────────────────────────────────
# 12. STREAMLIT UI
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
        with st.sidebar:
            st.markdown("### 📝 How It Works")
            st.markdown("""
            1. **Pre-process** — Normalise phones, collapse spaced accounts, strip currency prefixes & suffixes
            2. **Tokenize** — Split into context-aware tokens, expand glued label:value pairs
            3. **NLP Score** — Each token scored using format gate + context keyword signals
            4. **Fallback chain** — Alphanumeric invoices → full-month dates → bare amounts → inline bank details
            5. **Validate** — Cross-field checks flag missing, suspicious, or duplicate values
            6. **Export** — Clean data to Excel with optional Warnings sheet

            **Supported fields:** Party Name, Invoice No., Date, Amount, Phone, Bank Account, IFSC, PAN/GST

            **Date formats:** DD-Mon-YY, DD.MM.YYYY, DD/MM/YYYY, YYYY-MM-DD, 28 November 2025

            **Bank formats:** Multiline labels, dash/colon glued, inline comma-separated, spaced/dotted accounts, Saving/Current A/C, Beneficiary, Pay to
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

                # Excel export
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Invoices')
                    ws = writer.sheets['Invoices']
                    for col_idx, col_name in enumerate(columns):
                        max_len = max(
                            df[col_name].astype(str).apply(len).max(),
                            len(col_name)
                        ) + 2
                        ws.column_dimensions[chr(65 + col_idx)].width = min(max_len, 50)

                    if all_warnings:
                        warn_rows = [{"File": f, "Warning": w}
                                     for f, ws_list in all_warnings.items() for w in ws_list]
                        pd.DataFrame(warn_rows).to_excel(writer, index=False, sheet_name='Warnings')
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

                # Debug text download if there were warnings
                if all_warnings and all_raw_texts:
                    debug_txt = ""
                    for fname, raw in all_raw_texts.items():
                        debug_txt += f"{'='*60}\nFILE: {fname}\n{'='*60}\n{raw}\n\n"
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

    st.markdown("---")
    st.markdown(
        "**Pipeline:** PDF → Pre-process → Tokenize → NLP Scoring → "
        "Fallback Chain → Validation → Excel"
    )


if __name__ == "__main__":
    main()
