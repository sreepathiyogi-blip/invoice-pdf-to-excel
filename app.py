import streamlit as st
import pdfplumber
import pandas as pd
import re
from io import BytesIO
from datetime import datetime
from collections import defaultdict
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 1.  ENHANCED TOKENIZER with better splitting
# ─────────────────────────────────────────────

class Token:
    """A single token carrying its text, position, and surrounding context."""
    __slots__ = ("text", "pos", "line_idx", "line", "prev_tokens", "next_tokens", "font_size", "is_bold")

    def __init__(self, text, pos, line_idx, line, prev_tokens, next_tokens, font_size=None, is_bold=False):
        self.text = text
        self.pos = pos
        self.line_idx = line_idx
        self.line = line
        self.prev_tokens = prev_tokens
        self.next_tokens = next_tokens
        self.font_size = font_size
        self.is_bold = is_bold


def _split_glued_patterns(token_text):
    """
    Enhanced splitting for various glued patterns:
    - Label-value: 'no.-450010110017123', 'Code-BKID0004500'
    - Label:value: 'Invoice:12345', 'Date:01-Jan-24'
    - Label/value: 'GST/15AABCD1234E1Z5'
    - Preserves: '3,000.00', 'email@domain.com', normal hyphenated words
    """
    parts = []
    
    # Pattern 1: Label-Value (with dash)
    m = re.match(r'^(.*?[A-Za-z.])[-–—]+([A-Za-z0-9].*)$', token_text)
    if m:
        label = m.group(1).strip()
        value = m.group(2).strip()
        if label and value:
            return [label, value]
        if label:
            return [label]
    
    # Pattern 2: Label:Value (with colon)
    m = re.match(r'^(.*?[A-Za-z.]):([A-Za-z0-9].*)$', token_text)
    if m:
        label = m.group(1).strip()
        value = m.group(2).strip()
        if label and value:
            return [label, value]
    
    # Pattern 3: Label/Value (with slash, not email/URL)
    if '/' in token_text and '@' not in token_text and '://' not in token_text:
        m = re.match(r'^([A-Za-z]+)/(.+)$', token_text)
        if m:
            label = m.group(1).strip()
            value = m.group(2).strip()
            if label and value and len(label) > 1:
                return [label, value]
    
    # Pattern 4: Trailing dash/colon only
    m = re.match(r'^(.*?[A-Za-z.])[:–—-]+$', token_text)
    if m:
        label = m.group(1).strip()
        return [label] if label else [token_text]
    
    return [token_text]


def tokenize(text, context_window=6):
    """
    Enhanced tokenization with better pattern recognition and context preservation.
    """
    tokens = []
    lines = text.split("\n")
    char_offset = 0

    for line_idx, line in enumerate(lines):
        if not line.strip():
            char_offset += len(line) + 1
            continue
            
        # Split on whitespace runs
        parts = re.split(r'(\s+)', line)
        raw_tokens = [p for p in parts if p.strip()]

        # Expand any glued patterns
        line_tokens_text = []
        for t in raw_tokens:
            line_tokens_text.extend(_split_glued_patterns(t))

        for tok_idx, tok_text in enumerate(line_tokens_text):
            # Find exact char position
            pos = text.find(tok_text, char_offset)
            if pos == -1:
                pos = char_offset

            prev = line_tokens_text[max(0, tok_idx - context_window):tok_idx]
            nxt = line_tokens_text[tok_idx + 1:tok_idx + 1 + context_window]

            # Detect if this might be bold/header (ALL CAPS, or line position)
            is_bold = tok_text.isupper() and len(tok_text) > 2

            tokens.append(Token(
                text=tok_text,
                pos=pos,
                line_idx=line_idx,
                line=line,
                prev_tokens=prev,
                next_tokens=nxt,
                is_bold=is_bold
            ))
            char_offset = pos + len(tok_text)

        char_offset += 1  # newline

    return tokens


# ─────────────────────────────────────────────
# 2.  ENHANCED ENTITY DEFINITIONS with more patterns
# ─────────────────────────────────────────────

ENTITY_DEFS = {
    "IFSC": {
        "format": [
            re.compile(r'^[A-Z]{4}0[0-9A-Z]{6}$', re.IGNORECASE),  # Standard IFSC
            re.compile(r'^[A-Z]{4}[0-9]{7}$', re.IGNORECASE),      # Common variant
        ],
        "context_keywords": {
            "ifsc": 10, "code": 4, "bank": 5, "branch": 4,
            "rtgs": 3, "neft": 3, "imps": 3, "swift": 2
        },
        "validate": lambda v: len(v) == 11 and re.match(r'^[A-Z]{4}[0-9A-Z]{7}$', v.upper()),
        "priority": 9
    },
    "PAN": {
        "format": [
            re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$', re.IGNORECASE),
            re.compile(r'^[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]$', re.IGNORECASE),  # Strict PAN pattern
        ],
        "context_keywords": {
            "pan": 10, "permanent": 6, "account": 4, "number": 3,
            "tin": 5, "tax": 3, "payer": 2
        },
        "validate": lambda v: len(v) == 10 and re.match(r'^[A-Z]{5}\d{4}[A-Z]$', v.upper()),
        "priority": 8
    },
    "GST": {
        "format": [
            re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][Z][0-9A-Z]$', re.IGNORECASE),
            re.compile(r'^[0-9]{2}[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$', re.IGNORECASE),
        ],
        "context_keywords": {
            "gst": 10, "gstin": 12, "tin": 6, "tax": 5,
            "identification": 4, "vat": 3, "registration": 3
        },
        "validate": lambda v: len(v) == 15 and v[2:12].isalnum(),
        "priority": 8
    },
    "ACCOUNT_NUMBER": {
        "format": [
            re.compile(r'^\d{9,18}$'),                    # Standard account numbers
            re.compile(r'^[A-Z]{2}\d{7,16}$'),           # Some banks prefix with letters
            re.compile(r'^\d{4}[-\s]?\d{4}[-\s]?\d{4,10}$'),  # Formatted with separators
        ],
        "context_keywords": {
            "account": 10, "number": 5, "ac": 8, "a/c": 10, "acc": 8,
            "bank": 6, "holder": 4, "no": 4, "saving": 3, "current": 3,
            "beneficiary": 5, "payee": 4
        },
        "validate": lambda v: 9 <= len(re.sub(r'[-\s]', '', v)) <= 18 and re.sub(r'[-\s]', '', v).isdigit(),
        "priority": 7
    },
    "INVOICE_NUMBER": {
        "format": [
            re.compile(r'^\d{2,7}$'),                           # Pure numeric
            re.compile(r'^[A-Z]{1,4}[-/]?\d{2,7}$', re.IGNORECASE),  # Prefix-Number
            re.compile(r'^\d{2,4}[-/]\d{2,4}[-/]?\d{0,4}$'),   # Date-style invoice nums
            re.compile(r'^INV[-/]?\d{2,7}$', re.IGNORECASE),   # INV prefix
            re.compile(r'^[A-Z0-9]{4,12}$', re.IGNORECASE),    # Alphanumeric
        ],
        "context_keywords": {
            "invoice": 12, "no": 6, "number": 5, "bill": 8, "#": 6,
            "dated": 4, "inv": 10, "voucher": 3, "ref": 3, "reference": 3
        },
        "validate": lambda v: 2 <= len(re.sub(r'[-/\s]', '', v)) <= 15,
        "priority": 6
    },
    "AMOUNT": {
        "format": [
            re.compile(r'^(\d{1,3}(,\d{3})+(\.\d{1,2})?|\d+\.\d{1,2})$'),  # With comma/decimal
            re.compile(r'^₹?\s*(\d{1,3}(,\d{3})+(\.\d{1,2})?|\d+\.\d{1,2})$'),  # With rupee symbol
            re.compile(r'^(Rs\.?|INR)?\s*(\d{1,3}(,\d{3})+(\.\d{1,2})?|\d+\.\d{1,2})$', re.IGNORECASE),
            re.compile(r'^\d{4,}$'),  # Large numbers without formatting (backup)
        ],
        "context_keywords": {
            "amount": 12, "total": 10, "grand": 8, "net": 6, "payable": 10,
            "balance": 7, "due": 6, "rs": 5, "inr": 5, "₹": 8,
            "rate": 3, "value": 4, "sum": 5, "payment": 6,
            "taxable": 4, "subtotal": 5, "outstanding": 5
        },
        "validate": lambda v: float(re.sub(r'[^\d.]', '', v)) >= 1,
        "priority": 8
    },
    "DATE": {
        "format": [
            re.compile(r'^\d{1,2}[-./]\s*([A-Za-z]{3}|\d{1,2})\s*[-./]\d{2,4}$'),  # DD-Mon-YY
            re.compile(r'^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}$'),                        # DD/MM/YYYY
            re.compile(r'^\d{4}[-./]\d{1,2}[-./]\d{1,2}$'),                          # YYYY-MM-DD
            re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}', re.IGNORECASE),  # Mon DD, YYYY
            re.compile(r'^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2,4}', re.IGNORECASE),  # DD Mon YYYY
        ],
        "context_keywords": {
            "date": 10, "dated": 12, "invoice": 5, "on": 3, "day": 4,
            "issued": 6, "created": 4, "generated": 4, "dt": 6
        },
        "validate": lambda v: True,
        "priority": 7
    },
    "PHONE_NUMBER": {
        "format": [
            re.compile(r'^[6-9]\d{9}$'),                              # Indian mobile
            re.compile(r'^0\d{10}$'),                                 # With leading 0
            re.compile(r'^\+91[-\s]?[6-9]\d{9}$'),                   # With +91
            re.compile(r'^91[-\s]?[6-9]\d{9}$'),                     # With 91
            re.compile(r'^(\+91|91|0)?[-\s]?\d{5}[-\s]?\d{5}$'),    # Formatted
        ],
        "context_keywords": {
            "phone": 12, "ph": 9, "tel": 9, "mobile": 12, "mob": 10,
            "cell": 8, "contact": 7, "fax": 7, "whatsapp": 9,
            "helpline": 5, "toll": 5, "call": 5,
            "phone:": 12, "ph:": 9, "tel:": 9, "mobile:": 12, "mob:": 10,
            "fax:": 7, "contact:": 7, "whatsapp:": 9,
        },
        "validate": lambda v: 10 <= len(re.sub(r'[^\d]', '', v)) <= 12,
        "priority": 5
    },
    "EMAIL": {
        "format": [
            re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'),
        ],
        "context_keywords": {
            "email": 12, "e-mail": 10, "mail": 6, "contact": 4, "@": 10
        },
        "validate": lambda v: '@' in v and '.' in v.split('@')[1],
        "priority": 6
    },
    "PINCODE": {
        "format": [
            re.compile(r'^\d{6}$'),
        ],
        "context_keywords": {
            "pin": 10, "pincode": 12, "postal": 8, "zip": 6, "code": 4
        },
        "validate": lambda v: len(v) == 6 and v.isdigit(),
        "priority": 4
    },
}

# ─────────────────────────────────────────────
# 2b. ENHANCED NEGATIVE CONTEXT
# ─────────────────────────────────────────────

NEGATIVE_CONTEXT = {
    # Phone / contact related
    "phone", "ph", "ph.", "tel", "tel.", "mobile", "mob", "mob.",
    "cell", "contact", "fax", "fax.", "helpline", "toll", "whatsapp",
    "call", "sms", "isd", "std",
    "phone:", "ph:", "tel:", "mobile:", "mob:", "fax:", "contact:",
    "whatsapp:", "helpline:",
    
    # Document metadata (should not be account numbers)
    "page", "of", "copy", "original", "duplicate", "triplicate",
    
    # Pin codes (should not be invoice numbers)
    "pin", "pincode", "postal", "zip",
}

# ─────────────────────────────────────────────
# 3.  ENHANCED NLP SCORER with priority weighting
# ─────────────────────────────────────────────

def score_token_for_entity(token, entity_type):
    """
    Enhanced scoring with:
    - Multiple format pattern support
    - Priority-based scoring
    - Position-aware bonuses
    - Stricter negative context checking
    """
    edef = ENTITY_DEFS[entity_type]
    score = 0

    # --- Format match (try all patterns) ---
    format_matched = False
    format_patterns = edef["format"] if isinstance(edef["format"], list) else [edef["format"]]
    
    for pattern in format_patterns:
        if pattern.match(token.text.strip()):
            format_matched = True
            score += 50
            break
    
    if not format_matched:
        return 0

    # --- Negative context check ---
    surrounding = [t.lower() for t in token.prev_tokens + token.next_tokens]
    line_words = [w.lower() for w in re.split(r'\W+', token.line)]
    all_context = set(surrounding + line_words)

    # Kill score if negative context found (unless it's the matching entity type)
    if entity_type != "PHONE_NUMBER":
        if all_context & NEGATIVE_CONTEXT:
            return 0
    
    # For phone numbers, require positive context
    if entity_type == "PHONE_NUMBER":
        has_phone_context = any(kw in all_context for kw in 
                               ["phone", "mobile", "tel", "contact", "mob", "fax", "whatsapp"])
        if not has_phone_context:
            return 0

    # --- Positive context keywords ---
    for keyword, weight in edef["context_keywords"].items():
        kw = keyword.lower()
        
        # Check in surrounding tokens (high value)
        if kw in [t.lower() for t in surrounding]:
            score += weight
            
            # Immediate adjacency bonus
            if token.prev_tokens and token.prev_tokens[-1].lower() == kw:
                score += 8
            if token.next_tokens and token.next_tokens[0].lower() == kw:
                score += 8
        
        # Check in line words (medium value)
        elif kw in line_words:
            score += weight * 0.5
    
    # --- Position bonuses ---
    # Values at start of line often indicate labels
    if token.pos == 0 or (token.prev_tokens and token.prev_tokens[-1] in [':', '-', '–']):
        score += 5
    
    # Bold/header text bonus (for party names, dates, invoice numbers)
    if token.is_bold and entity_type in ["DATE", "INVOICE_NUMBER"]:
        score += 10

    # --- Cross-entity penalty (avoid ambiguity) ---
    for other_type, odef in ENTITY_DEFS.items():
        if other_type == entity_type:
            continue
        
        # Check if this token matches another entity's format
        other_patterns = odef["format"] if isinstance(odef["format"], list) else [odef["format"]]
        for pattern in other_patterns:
            if pattern.match(token.text.strip()):
                # Calculate context score for other entity
                other_context_score = 0
                for kw, w in odef["context_keywords"].items():
                    if kw.lower() in all_context:
                        other_context_score += w
                
                # Apply penalty if other entity has strong context
                if other_context_score > 10:
                    score -= 25
                elif other_context_score > 5:
                    score -= 15
                break

    # --- Priority boost ---
    priority_boost = edef.get("priority", 5)
    score += priority_boost

    return max(score, 0)


def find_entity(tokens, entity_type, top_n=5):
    """
    Enhanced entity finder with deduplication and better ranking.
    """
    candidates = []
    seen_values = set()

    for token in tokens:
        clean = token.text.strip()
        clean_normalized = re.sub(r'[-\s]', '', clean).upper()
        
        # Skip duplicates
        if clean_normalized in seen_values:
            continue
        seen_values.add(clean_normalized)

        s = score_token_for_entity(token, entity_type)
        if s > 0:
            candidates.append((token, s))

    # Sort by score descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:top_n]


# ─────────────────────────────────────────────
# 4.  ENHANCED SPECIALIZED EXTRACTORS
# ─────────────────────────────────────────────

def extract_party_name(text, tokens):
    """
    Enhanced party name extraction with multiple strategies:
    1. Account Holder label detection
    2. Header company name (lines 1-8)
    3. Post-INVOICE keyword extraction
    4. Bold/ALL CAPS text in header
    5. Pattern-based company name detection (Pvt Ltd, LLP, etc.)
    """
    candidates = []
    lines = text.split("\n")

    # --- Strategy 1: Account Holder ---
    for i, line in enumerate(lines):
        if re.search(r'account\s+holder|beneficiary|payee', line, re.IGNORECASE):
            for j in range(i, min(i + 3, len(lines))):
                candidate = re.sub(
                    r'(account\s+holder|beneficiary|payee|name|:|-)', 
                    '', lines[j], flags=re.IGNORECASE
                ).strip()
                if candidate and re.match(r'^[A-Za-z\s\.&,\-]+$', candidate) and 3 < len(candidate) < 100:
                    score = 50
                    if candidate.isupper():
                        score += 15
                    candidates.append((candidate, score))

    # --- Strategy 2: Company suffixes (Pvt Ltd, LLP, etc.) ---
    company_suffixes = [
        r'pvt\.?\s*ltd\.?', r'private\s+limited', r'llp', 
        r'limited', r'ltd\.?', r'inc\.?', r'corporation',
        r'enterprises', r'industries', r'traders'
    ]
    for i, line in enumerate(lines[:15]):  # Check first 15 lines
        line = line.strip()
        for suffix_pattern in company_suffixes:
            if re.search(suffix_pattern, line, re.IGNORECASE):
                # This line likely contains company name
                clean = re.sub(r'(INVOICE|BILL|TAX|GST|PAN).*$', '', line, flags=re.IGNORECASE).strip()
                if 5 < len(clean) < 100:
                    score = 60
                    if line.isupper():
                        score += 10
                    if i <= 5:
                        score += 15  # Early in document
                    candidates.append((clean, score))
                    break

    # --- Strategy 3: Header name (lines 1-8) ---
    for i, line in enumerate(lines[:8]):
        line = line.strip()
        if not line or len(line) < 5:
            continue
        
        # Must be mostly alphabetic
        if re.match(r'^[A-Z][A-Za-z\s\.&,\-()]+$', line) and 5 < len(line) < 80:
            # Skip obvious labels/headers
            skip_words = [
                'invoice', 'phone', 'email', 'address', 'gst', 'pan',
                'date', 'total', 'amount', 'bank', 'ifsc', 'bill',
                'tax', 'original', 'copy', 'page'
            ]
            if any(sw in line.lower() for sw in skip_words):
                continue
            
            score = 35
            if i <= 2:
                score += 20  # Very early lines
            if line.isupper():
                score += 10
            if len(line) > 15:  # Reasonable company name length
                score += 5
            
            candidates.append((line, score))

    # --- Strategy 4: Post-INVOICE keyword ---
    for i, line in enumerate(lines):
        if re.match(r'^\s*INVOICE\b', line.strip(), re.IGNORECASE):
            for j in range(i + 1, min(i + 4, len(lines))):
                candidate = lines[j].strip()
                if (candidate and 
                    re.match(r'^[A-Z][A-Za-z\s\.&,\-]+$', candidate) and 
                    5 < len(candidate) < 80):
                    candidates.append((candidate, 40))
                    break

    # --- Strategy 5: ALL CAPS lines in header ---
    for i, line in enumerate(lines[:10]):
        if line.strip().isupper() and 8 < len(line.strip()) < 80:
            clean = re.sub(r'(INVOICE|BILL|ORIGINAL|COPY|PAGE|\d+)', '', line).strip()
            if len(clean) > 5:
                score = 30
                if i <= 3:
                    score += 15
                candidates.append((clean, score))

    if not candidates:
        return "", 0

    # Remove duplicates and pick best
    seen = set()
    unique_candidates = []
    for name, score in candidates:
        normalized = name.upper().strip()
        if normalized not in seen:
            seen.add(normalized)
            unique_candidates.append((name, score))

    unique_candidates.sort(key=lambda x: x[1], reverse=True)
    return unique_candidates[0]


def extract_amount_from_tables(tables):
    """
    Enhanced table amount extraction:
    - Multiple column name variations
    - Better numeric parsing
    - Grand total / final total detection
    - Row position scoring (last row often = total)
    """
    candidates = []
    
    amount_keywords = [
        'amount', 'total', 'grand total', 'net amount', 
        'payable', 'balance', 'due', 'sum', 'value',
        'subtotal', 'invoice amount', 'bill amount'
    ]

    for table_idx, table in enumerate(tables):
        if not table or len(table) < 2:
            continue

        amount_col_idx = None
        header_row_idx = 0

        # Find amount column header
        for row_idx, row in enumerate(table[:5]):  # Check first 5 rows for headers
            if not row:
                continue

            for col_idx, cell in enumerate(row):
                cell_text = str(cell).lower().strip() if cell else ""
                if any(keyword in cell_text for keyword in amount_keywords):
                    amount_col_idx = col_idx
                    header_row_idx = row_idx
                    break
            
            if amount_col_idx is not None:
                break

        # Extract amounts from the column
        if amount_col_idx is not None:
            for row_idx, row in enumerate(table[header_row_idx + 1:], start=header_row_idx + 1):
                if amount_col_idx >= len(row) or not row[amount_col_idx]:
                    continue
                
                raw = re.sub(r'[^\d,.]', '', str(row[amount_col_idx]).strip())
                if not raw or raw.count('.') > 1:
                    continue
                
                try:
                    val = float(raw.replace(',', ''))
                    if 1 <= val < 1e10:
                        score = 15
                        
                        # Decimal formatting bonus
                        if '.' in raw:
                            score += 20
                        
                        # Perfect currency format
                        if re.match(r'^\d{1,3}(,\d{3})*\.\d{2}$', raw):
                            score += 30
                        
                        # Last row in table = often the total
                        if row_idx == len(table) - 1:
                            score += 40
                        
                        # Check if row label indicates total
                        row_text = ' '.join([str(c) for c in row if c]).lower()
                        if any(word in row_text for word in ['total', 'grand', 'net', 'payable']):
                            score += 50
                        
                        candidates.append((raw, score, val))
                except (ValueError, TypeError):
                    continue

    if not candidates:
        return "", 0

    # Sort by score desc, then value desc
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    best_amount = candidates[0][0].replace(',', '')
    
    # Ensure proper decimal formatting
    if '.' not in best_amount:
        best_amount = f"{best_amount}.00"
    
    return best_amount, candidates[0][1]


def extract_address(text, lines):
    """
    Extract address using common patterns:
    - After keywords like 'Address:', 'Bill To:', 'Ship To:'
    - Multi-line addresses with pin codes
    """
    candidates = []
    
    address_keywords = ['address', 'bill to', 'ship to', 'billing address', 'shipping address']
    
    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        
        # Check if this line contains an address keyword
        if any(kw in line_lower for kw in address_keywords):
            # Collect next 3-6 lines as potential address
            address_lines = []
            for j in range(i + 1, min(i + 7, len(lines))):
                addr_line = lines[j].strip()
                if addr_line and len(addr_line) > 3:
                    # Stop if we hit another section header
                    if re.match(r'^(INVOICE|BILL|GST|PAN|AMOUNT)', addr_line, re.IGNORECASE):
                        break
                    address_lines.append(addr_line)
            
            if address_lines:
                full_address = ', '.join(address_lines[:5])  # Max 5 lines
                candidates.append((full_address, 50))
    
    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]
    
    return ""


# ─────────────────────────────────────────────
# 5.  ENHANCED DATE NORMALIZER
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
    'dec': '12', 'december': '12'
}


def normalize_date(date_str):
    """Enhanced date normalization handling multiple formats."""
    if not date_str:
        return ""
    
    date_str = date_str.strip()
    
    # Pattern 1: DD-Mon-YY or DD-Mon-YYYY
    m = re.match(r'(\d{1,2})[-./\s]+([A-Za-z]{3,9})[-./\s]+(\d{2,4})', date_str, re.IGNORECASE)
    if m:
        day = m.group(1).zfill(2)
        month = MONTH_MAP.get(m.group(2).lower()[:3], '01')
        year = m.group(3)
        if len(year) == 2:
            year = '20' + year if int(year) <= 50 else '19' + year
        return f"{day}-{month}-{year}"
    
    # Pattern 2: Mon DD, YYYY
    m = re.match(r'([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})', date_str, re.IGNORECASE)
    if m:
        month = MONTH_MAP.get(m.group(1).lower()[:3], '01')
        day = m.group(2).zfill(2)
        year = m.group(3)
        return f"{day}-{month}-{year}"
    
    # Pattern 3: DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r'(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})', date_str)
    if m:
        day = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        year = m.group(3)
        if len(year) == 2:
            year = '20' + year if int(year) <= 50 else '19' + year
        return f"{day}-{month}-{year}"
    
    # Pattern 4: YYYY-MM-DD (ISO format)
    m = re.match(r'(\d{4})[-./](\d{1,2})[-./](\d{1,2})', date_str)
    if m:
        year = m.group(1)
        month = m.group(2).zfill(2)
        day = m.group(3).zfill(2)
        return f"{day}-{month}-{year}"
    
    return date_str


# ─────────────────────────────────────────────
# 6.  ENHANCED BANK NAME LOOKUP
# ─────────────────────────────────────────────

IFSC_PREFIX_MAP = {
    # Major Banks
    "HDFC": "HDFC Bank",
    "ICIC": "ICICI Bank",
    "SBIN": "State Bank of India",
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
    "BDBL": "Bandhan Bank",
    "FDRL": "Federal Bank",
    "SIBL": "South Indian Bank",
    "KVBL": "Karur Vysya Bank",
    "TMBL": "Tamilnad Mercantile Bank",
    "CITI": "Citibank",
    "HSBC": "HSBC",
    "SCBL": "Standard Chartered Bank",
    "DBSS": "DBS Bank",
    "IOBA": "Indian Overseas Bank",
    "IDIB": "Indian Bank",
    "MAHB": "Bank of Maharashtra",
    "UCBA": "UCO Bank",
    "CBIN": "Central Bank of India",
    "ALLA": "Allahabad Bank",
    "CORP": "Corporation Bank",
    "ANDB": "Andhra Bank",
    "VIJB": "Vijaya Bank",
    "AIRP": "Airtel Payments Bank",
    "PYTM": "Paytm Payments Bank",
    "JIOP": "Jio Payments Bank",
}


def derive_bank_name(ifsc):
    """Enhanced bank name derivation from IFSC code."""
    if not ifsc or len(ifsc) < 4:
        return ""
    prefix = ifsc[:4].upper()
    return IFSC_PREFIX_MAP.get(prefix, prefix)


# ─────────────────────────────────────────────
# 7.  ENHANCED VALIDATION ENGINE
# ─────────────────────────────────────────────

def validate_extraction(record):
    """
    Comprehensive cross-field validation with detailed warnings.
    """
    warnings = []
    errors = []

    # Critical validations
    if not record.get("Party name"):
        errors.append("⚠️ CRITICAL: Party name not detected")
    
    if not record.get("Invoice Date"):
        errors.append("⚠️ CRITICAL: Invoice date not detected")
    
    if not record.get("Amount"):
        errors.append("⚠️ CRITICAL: Amount not detected")

    # Banking detail validations
    has_account = bool(record.get("Bank Account No"))
    has_ifsc = bool(record.get("IFSC Code"))
    has_amount = bool(record.get("Amount"))

    if has_amount and not has_account:
        warnings.append("Amount found but no Bank Account Number detected")
    
    if has_account and not has_ifsc:
        warnings.append("Account Number found but no IFSC Code detected")
    
    if has_ifsc and not has_account:
        warnings.append("IFSC Code found but no Account Number detected")

    # Invoice number validation
    inv = record.get("Invoice No.", "")
    if inv:
        if len(inv) > 12:
            warnings.append(f"Invoice No. '{inv}' is unusually long — may be misclassified")
        if inv.isdigit() and len(inv) == 10:
            warnings.append(f"Invoice No. '{inv}' looks like a phone number")
    else:
        warnings.append("Invoice Number not detected")

    # Phone number validation
    phone = record.get("Phone Number", "")
    if phone and len(re.sub(r'[^\d]', '', phone)) != 10:
        warnings.append(f"Phone Number '{phone}' has unusual length")

    # PAN/GST validation
    if not record.get("PAN Number / GST"):
        warnings.append("PAN/GST not detected")

    # Amount validation
    amount = record.get("Amount", "")
    if amount:
        try:
            amt_val = float(amount)
            if amt_val < 1:
                warnings.append(f"Amount {amt_val} seems too small")
            if amt_val > 10000000:  # 1 crore
                warnings.append(f"Amount {amt_val} is very large — please verify")
        except ValueError:
            warnings.append(f"Amount '{amount}' is not a valid number")

    return errors + warnings


def parse_inline_bank_details(text):
    """
    Enhanced parsing for inline bank details with multiple formats:
    - Comma-separated: 'NAME - X, BANK NAME - Y, ACCOUNT - Z'
    - Colon-separated: 'Bank Name: X, Account No: Y'
    - Mixed formats
    """
    result = {}
    
    for line in text.split("\n"):
        line_lower = line.lower()
        
        if not any(kw in line_lower for kw in ['bank', 'account', 'ifsc']):
            continue
        
        # Account number patterns
        patterns = [
            r'(?:bank\s+)?account\s+(?:no|number|#)?\s*[:–—-]\s*(\d{9,18})',
            r'a/?c\s+(?:no|number)?\s*[:–—-]\s*(\d{9,18})',
            r'acc\s*[:–—-]\s*(\d{9,18})',
        ]
        for pattern in patterns:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                result["account_no"] = m.group(1).strip()
                break
        
        # IFSC code patterns
        patterns = [
            r'ifsc\s*(?:code)?\s*[:–—-]\s*([A-Z]{4}[0-9A-Z]{7})',
            r'rtgs\s*(?:code)?\s*[:–—-]\s*([A-Z]{4}[0-9A-Z]{7})',
        ]
        for pattern in patterns:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                result["ifsc"] = m.group(1).upper().strip()
                break
        
        # Bank name patterns
        patterns = [
            r'bank\s+name\s*[:–—-]\s*([A-Za-z\s&]+?)(?:,|$|\|)',
            r'bank\s*[:–—-]\s*([A-Za-z\s&]+?)(?:,|$|\|)',
        ]
        for pattern in patterns:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                bank_name = m.group(1).strip()
                if len(bank_name) > 2:
                    result["bank_name"] = bank_name
                break
        
        # If we found critical info, we can stop
        if "account_no" in result or "ifsc" in result:
            break
    
    return result


# ─────────────────────────────────────────────
# 8.  MAIN EXTRACTION PIPELINE
# ─────────────────────────────────────────────

def extract_invoice_data(pdf_file, debug_mode=False):
    """
    Enhanced full pipeline with comprehensive error handling and fallbacks.
    """
    debug_info = ""
    
    try:
        with pdfplumber.open(pdf_file) as pdf:
            full_text = ""
            all_tables = []
            page_count = len(pdf.pages)

            # Extract text and tables from all pages
            for page_num, page in enumerate(pdf.pages, 1):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        full_text += f"\n--- PAGE {page_num} ---\n{page_text}\n"
                    
                    tables = page.extract_tables()
                    if tables:
                        all_tables.extend(tables)
                except Exception as e:
                    logger.warning(f"Error extracting page {page_num}: {str(e)}")
                    continue

            if not full_text.strip():
                return None, ["No text extracted — PDF may be image-based or corrupted"], "", ""

            lines = full_text.split("\n")
            
            # ── Tokenize ──
            try:
                tokens = tokenize(full_text, context_window=6)
            except Exception as e:
                return None, [f"Tokenization error: {str(e)}"], "", full_text

            # ── NLP Entity Detection ──
            try:
                ifsc_candidates = find_entity(tokens, "IFSC")
                pan_candidates = find_entity(tokens, "PAN")
                gst_candidates = find_entity(tokens, "GST")
                acc_candidates = find_entity(tokens, "ACCOUNT_NUMBER")
                inv_candidates = find_entity(tokens, "INVOICE_NUMBER")
                date_candidates = find_entity(tokens, "DATE")
                amount_candidates = find_entity(tokens, "AMOUNT")
                phone_candidates = find_entity(tokens, "PHONE_NUMBER")
                email_candidates = find_entity(tokens, "EMAIL")
            except Exception as e:
                return None, [f"Entity detection error: {str(e)}"], "", full_text

            # ── Select Best Candidates ──
            
            # IFSC Code
            ifsc = ""
            if ifsc_candidates:
                ifsc = ifsc_candidates[0][0].text.upper().strip()
                # Validate format
                if not re.match(r'^[A-Z]{4}[0-9A-Z]{7}$', ifsc):
                    ifsc = ""
            
            # PAN Number
            pan = ""
            if pan_candidates:
                pan = pan_candidates[0][0].text.upper().strip()
                if not re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan):
                    pan = ""
            
            # GST Number
            gst = ""
            if gst_candidates:
                gst = gst_candidates[0][0].text.upper().strip()
                if len(gst) != 15:
                    gst = ""
            
            # Invoice Number (must not match account number)
            inv_no = ""
            if inv_candidates:
                for tok, score in inv_candidates:
                    candidate = tok.text.strip()
                    # Ensure it's not an account number or phone number
                    if len(candidate) <= 12 and candidate not in [t[0].text.strip() for t in acc_candidates]:
                        inv_no = candidate
                        break
            
            # Account Number (must not be invoice number)
            acc_no = ""
            if acc_candidates:
                for tok, score in acc_candidates:
                    candidate = tok.text.strip()
                    if candidate != inv_no and 9 <= len(candidate) <= 18:
                        acc_no = candidate
                        break
            
            # Invoice Date
            inv_date = ""
            if date_candidates:
                inv_date = normalize_date(date_candidates[0][0].text.strip())
            
            # Amount (prefer NLP, fallback to table)
            amount = ""
            if amount_candidates:
                for tok, score in amount_candidates:
                    raw = re.sub(r'[^\d.]', '', tok.text.strip())
                    try:
                        if float(raw) >= 1:
                            amount = raw
                            # Ensure .00 if no decimals
                            if '.' not in amount:
                                amount = f"{amount}.00"
                            break
                    except ValueError:
                        continue
            
            # Fallback to table extraction
            if not amount and all_tables:
                amount, _ = extract_amount_from_tables(all_tables)
            
            # Party Name
            party_name, _ = extract_party_name(full_text, tokens)
            
            # Phone Number
            phone_no = ""
            if phone_candidates:
                for tok, score in phone_candidates:
                    candidate = re.sub(r'[^\d]', '', tok.text.strip())
                    if len(candidate) == 10 and candidate != acc_no:
                        phone_no = candidate
                        break
            
            # Email
            email = ""
            if email_candidates:
                email = email_candidates[0][0].text.strip()
            
            # ── Inline Bank Details Fallback ──
            inline_bank = parse_inline_bank_details(full_text)
            if not acc_no and inline_bank.get("account_no"):
                acc_no = inline_bank["account_no"]
            if not ifsc and inline_bank.get("ifsc"):
                ifsc = inline_bank["ifsc"]
            
            # PAN/GST preference
            pan_gst = pan if pan else gst
            
            # Bank Name
            bank_name = derive_bank_name(ifsc)
            if not bank_name and inline_bank.get("bank_name"):
                bank_name = inline_bank["bank_name"]
            
            # Address (new field)
            address = extract_address(full_text, lines)
            
            # ── Build Record ──
            record = {
                "Party name": party_name,
                "Invoice Date": inv_date,
                "Invoice No.": inv_no,
                "Amount": amount,
                "Phone Number": phone_no,
                "Email": email,
                "Address": address,
                "Bank Name": bank_name,
                "Bank Account No": acc_no,
                "IFSC Code": ifsc,
                "PAN Number / GST": pan_gst,
            }
            
            # ── Validate ──
            warnings = validate_extraction(record)
            
            # ── Debug Info ──
            if debug_mode:
                debug_info = f"""
=== EXTRACTION DEBUG INFO ===
PDF Pages: {page_count}
Total Text Length: {len(full_text)} chars
Tokens Generated: {len(tokens)}

--- ENTITY CANDIDATES ---
IFSC: {[(t.text, s) for t, s in ifsc_candidates[:3]]}
PAN: {[(t.text, s) for t, s in pan_candidates[:3]]}
GST: {[(t.text, s) for t, s in gst_candidates[:3]]}
Account: {[(t.text, s) for t, s in acc_candidates[:3]]}
Invoice#: {[(t.text, s) for t, s in inv_candidates[:3]]}
Date: {[(t.text, s) for t, s in date_candidates[:3]]}
Amount: {[(t.text, s) for t, s in amount_candidates[:3]]}
Phone: {[(t.text, s) for t, s in phone_candidates[:3]]}
Email: {[(t.text, s) for t, s in email_candidates[:3]]}

--- FINAL EXTRACTION ---
{record}

--- VALIDATION WARNINGS ---
{warnings}

--- RAW TEXT (first 4000 chars) ---
{full_text[:4000]}
"""
            
            return record, warnings, debug_info, full_text
    
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Critical error in extract_invoice_data: {str(e)}\n{error_trace}")
        return None, [f"Critical Error: {str(e)}"], error_trace, ""


# ─────────────────────────────────────────────
# 9.  STREAMLIT UI WITH ENHANCEMENTS
# ─────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="Advanced Invoice Extractor",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    st.title("📄 Advanced Invoice PDF → Excel Converter")
    st.markdown("""
    Extract structured data from **any invoice format** using advanced NLP and pattern recognition.
    Supports: Tax invoices, proforma invoices, credit notes, debit notes, and more.
    """)
    
    # Sidebar controls
    with st.sidebar:
        st.header("⚙️ Settings")
        debug_mode = st.checkbox(
            "🔍 Debug Mode",
            value=False,
            help="Show detailed NLP scoring and raw text extraction"
        )
        
        show_confidence = st.checkbox(
            "📊 Show Confidence Scores",
            value=False,
            help="Display confidence scores for each extracted field"
        )
        
        st.markdown("---")
        st.markdown("### 📝 Extraction Capabilities")
        st.markdown("""
        **Supported Fields:**
        - Party/Company Name
        - Invoice Number & Date
        - Amount (with table fallback)
        - Bank Details (Name, Account, IFSC)
        - PAN & GST Numbers
        - Phone, Email, Address
        
        **Supported Formats:**
        - PDF with searchable text
        - Multi-page invoices
        - Tables and formatted layouts
        - Various date formats
        - Indian and international formats
        """)
        
        st.markdown("---")
        st.markdown("### ℹ️ Tips")
        st.markdown("""
        - Upload clear, text-based PDFs
        - Scanned images should be OCR'd first
        - Check validation warnings carefully
        - Use debug mode for troubleshooting
        """)
    
    # File uploader
    uploaded_files = st.file_uploader(
        "📤 Upload Invoice PDFs",
        type=['pdf'],
        accept_multiple_files=True,
        help="Select one or more invoice PDFs for batch processing"
    )
    
    if not uploaded_files:
        st.info("👆 Upload invoice PDFs to begin extraction")
        
        # Show example
        with st.expander("📖 See Example"):
            st.markdown("""
            **Example Invoice Fields Extracted:**
            ```
            Party name: ACME TRADERS PVT LTD
            Invoice Date: 15-Jan-2024
            Invoice No.: INV-2024-001
            Amount: 125000.00
            Bank Name: HDFC Bank
            Bank Account No: 50100123456789
            IFSC Code: HDFC0001234
            PAN/GST: AABCA1234E
            ```
            """)
        return
    
    st.success(f"✅ {len(uploaded_files)} file(s) uploaded successfully")
    
    # Process button
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        process_btn = st.button(
            "🚀 Process All Invoices",
            type="primary",
            use_container_width=True
        )
    
    if process_btn:
        with st.spinner("🔄 Processing invoices... This may take a moment."):
            all_data = []
            all_warnings = {}
            all_raw_texts = {}
            failed_files = []
            
            progress_bar = st.progress(0)
            status_placeholder = st.empty()
            
            for idx, pdf_file in enumerate(uploaded_files):
                status_placeholder.info(f"📄 Processing: **{pdf_file.name}** ({idx + 1}/{len(uploaded_files)})")
                
                try:
                    record, warnings, debug_info, raw_text = extract_invoice_data(pdf_file, debug_mode)
                    
                    if debug_mode and debug_info:
                        with st.expander(f"🔍 Debug Details — {pdf_file.name}"):
                            st.code(debug_info, language="text")
                    
                    if record:
                        # Add filename to record
                        record["Source File"] = pdf_file.name
                        all_data.append(record)
                        
                        if warnings:
                            all_warnings[pdf_file.name] = warnings
                        if raw_text:
                            all_raw_texts[pdf_file.name] = raw_text
                    else:
                        failed_files.append(pdf_file.name)
                        if warnings:
                            st.error(f"❌ {pdf_file.name}: {'; '.join(warnings)}")
                
                except Exception as e:
                    failed_files.append(pdf_file.name)
                    st.error(f"❌ {pdf_file.name}: Critical error - {str(e)}")
                
                progress_bar.progress((idx + 1) / len(uploaded_files))
            
            status_placeholder.empty()
            progress_bar.empty()
            
            # ── Display Results ──
            
            if all_data:
                st.success(f"✅ Successfully processed **{len(all_data)}** invoice(s)")
                
                if failed_files:
                    st.warning(f"⚠️ Failed to process: {', '.join(failed_files)}")
                
                # Show warnings
                if all_warnings:
                    with st.expander("⚠️ Validation Warnings & Notices", expanded=True):
                        for fname, warns in all_warnings.items():
                            st.markdown(f"**{fname}:**")
                            for w in warns:
                                if "CRITICAL" in w:
                                    st.error(f"  {w}")
                                else:
                                    st.warning(f"  {w}")
                
                # Display extracted data
                st.subheader("📊 Extracted Invoice Data")
                
                columns = [
                    "Source File", "Party name", "Invoice Date", "Invoice No.",
                    "Amount", "Phone Number", "Email", "Address",
                    "Bank Name", "Bank Account No", "IFSC Code", "PAN Number / GST"
                ]
                
                df = pd.DataFrame(all_data, columns=columns)
                
                # Display with formatting
                st.dataframe(
                    df,
                    use_container_width=True,
                    height=400
                )
                
                # Statistics
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Total Invoices", len(all_data))
                with col2:
                    complete = sum(1 for r in all_data if r.get("Amount") and r.get("Party name"))
                    st.metric("Complete Records", complete)
                with col3:
                    with_warnings = len(all_warnings)
                    st.metric("With Warnings", with_warnings)
                with col4:
                    st.metric("Failed", len(failed_files))
                
                # ── Excel Export ──
                st.markdown("---")
                st.subheader("💾 Export Options")
                
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    # Main data sheet
                    df.to_excel(writer, index=False, sheet_name='Invoices')
                    ws = writer.sheets['Invoices']
                    
                    # Auto-adjust column widths
                    for col_idx, col_name in enumerate(columns):
                        max_len = max(
                            df[col_name].astype(str).apply(len).max(),
                            len(col_name)
                        ) + 3
                        col_letter = chr(65 + col_idx) if col_idx < 26 else f"A{chr(65 + col_idx - 26)}"
                        ws.column_dimensions[col_letter].width = min(max_len, 60)
                    
                    # Warnings sheet
                    if all_warnings:
                        warn_rows = []
                        for fname, warns in all_warnings.items():
                            for w in warns:
                                warn_rows.append({
                                    "File": fname,
                                    "Warning Type": "CRITICAL" if "CRITICAL" in w else "INFO",
                                    "Message": w
                                })
                        warn_df = pd.DataFrame(warn_rows)
                        warn_df.to_excel(writer, index=False, sheet_name='Warnings')
                        ws2 = writer.sheets['Warnings']
                        ws2.column_dimensions['A'].width = 40
                        ws2.column_dimensions['B'].width = 15
                        ws2.column_dimensions['C'].width = 80
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                col1, col2 = st.columns(2)
                with col1:
                    st.download_button(
                        label="📥 Download Excel File",
                        data=output.getvalue(),
                        file_name=f"invoices_{timestamp}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                
                # Debug text export
                if all_raw_texts and (debug_mode or all_warnings):
                    with col2:
                        debug_txt = ""
                        for fname, raw in all_raw_texts.items():
                            debug_txt += f"{'='*70}\n"
                            debug_txt += f"FILE: {fname}\n"
                            debug_txt += f"{'='*70}\n"
                            debug_txt += raw + "\n\n"
                        
                        st.download_button(
                            label="📝 Download Debug Text",
                            data=debug_txt,
                            file_name=f"debug_raw_text_{timestamp}.txt",
                            mime="text/plain",
                            use_container_width=True
                        )
            
            else:
                st.error("❌ No data could be extracted from any uploaded PDFs")
                st.info("""
                **Possible reasons:**
                - PDFs are image-based (need OCR)
                - PDFs are corrupted or password-protected
                - Invoice format is highly non-standard
                
                **Solutions:**
                - Use OCR software to convert scanned PDFs to searchable text
                - Ensure PDFs are not password-protected
                - Try debug mode to see what was extracted
                """)
    
    # Footer
    st.markdown("---")
    st.markdown("""
    <div style='text-align: center; color: #666;'>
    <small>
    <b>Pipeline:</b> PDF → Text Extraction → Enhanced Tokenization → NLP Entity Scoring → 
    Multi-Pattern Matching → Cross-Field Validation → Excel Export
    </small>
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
