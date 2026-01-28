import streamlit as st
import pdfplumber
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

# Page config
st.set_page_config(page_title="Invoice PDF → Excel Converter", layout="wide")

def derive_bank_name(ifsc):
    """Derive bank name from IFSC code"""
    if not ifsc:
        return ""
    
    # Clean the IFSC code first - remove any prefix
    ifsc = re.sub(r'^(Code[-:\s]*|IFSC[-:\s]*)', '', ifsc, flags=re.IGNORECASE).strip()
    
    ifsc_upper = ifsc.upper().strip()
    if ifsc_upper.startswith("HDFC"):
        return "HDFC Bank"
    elif ifsc_upper.startswith("ICIC"):
        return "ICICI Bank"
    elif ifsc_upper.startswith("SBIN"):
        return "SBI"
    elif ifsc_upper.startswith("AXIS"):
        return "Axis Bank"
    elif ifsc_upper.startswith("KKBK"):
        return "Kotak Mahindra Bank"
    elif ifsc_upper.startswith("BKID"):
        return "Bank of India"
    elif ifsc_upper.startswith("PUNB"):
        return "Punjab National Bank"
    elif ifsc_upper.startswith("UBIN"):
        return "Union Bank of India"
    elif ifsc_upper.startswith("BARB"):
        return "Bank of Baroda"
    elif ifsc_upper.startswith("CNRB"):
        return "Canara Bank"
    else:
        # Return first 4 characters as bank identifier
        return ifsc[:4].upper() if len(ifsc) >= 4 else ifsc.upper()

def normalize_date(date_str):
    """Convert any date format to DD-MM-YYYY"""
    if not date_str:
        return ""
    
    # Month abbreviations mapping
    month_map = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
        'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
        'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
    }
    
    # Pattern 1: 22-Dec-25 or 22-Dec-2025 (with month name)
    match = re.match(r'(\d{1,2})[-\./]([A-Za-z]{3})[-\./](\d{2,4})', date_str)
    if match:
        day = match.group(1).zfill(2)
        month_name = match.group(2).lower()
        year = match.group(3)
        
        # Convert 2-digit year to 4-digit
        if len(year) == 2:
            year = '20' + year
        
        # Convert month name to number
        month = month_map.get(month_name, '01')
        
        return f"{day}-{month}-{year}"
    
    # Pattern 2: 22-11-25 or 22-11-2025 (numeric)
    match = re.match(r'(\d{1,2})[-\./](\d{1,2})[-\./](\d{2,4})', date_str)
    if match:
        day = match.group(1).zfill(2)
        month = match.group(2).zfill(2)
        year = match.group(3)
        
        # Convert 2-digit year to 4-digit
        if len(year) == 2:
            year = '20' + year
        
        return f"{day}-{month}-{year}"
    
    # If no pattern matches, return as-is
    return date_str

def clean_field_value(value):
    """Remove common prefixes like 'Name :', 'Code-', etc."""
    if not value:
        return ""
    
    # Remove common prefixes with various separators
    value = re.sub(r'^(Name|Code|Number|Account\s+Number|A/?c\s+No\.?|IFSC|PAN|GST|Tin\s+No)[-:\s]+', '', value, flags=re.IGNORECASE)
    
    return value.strip()

def extract_party_name(text):
    """Extract Party Name - tries multiple methods"""
    
    # Method 1: Look for business name right after "INVOICE" header
    invoice_name_pattern = r'INVOICE\s*\n\s*([A-Z][A-Za-z\s]+)\s*\n'
    match = re.search(invoice_name_pattern, text)
    if match:
        name = match.group(1).strip()
        if len(name) > 2 and len(name) < 100:
            return name
    
    # Method 2: Look for "Account Holder" with various formats
    acc_holder_patterns = [
        r'Account\s+Holder\s*:\s*(?:Name\s*:\s*)?([A-Z][A-Za-z\s]+?)(?:\n|Account\s+Number)',
        r'Account\s+Holder\s*:\s*([^\n]+)',
        r'Name\s*:\s*([A-Z][A-Z\s]+?)(?:\n)',
    ]
    
    for pattern in acc_holder_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            name = match.group(1).strip()
            name = clean_field_value(name)
            if name and len(name) > 2 and len(name) < 100:
                return name
    
    # Method 3: Look for name in header section (first 500 chars)
    header = text[:500]
    lines = header.split('\n')
    for line in lines[1:6]:  # Check lines 2-6
        line = line.strip()
        # If line looks like a name (mostly letters, spaces, reasonable length)
        if line and re.match(r'^[A-Z][A-Za-z\s\.]+$', line) and 3 < len(line) < 50:
            if not any(keyword in line.upper() for keyword in ['INVOICE', 'PHONE', 'EMAIL', 'ADDRESS', 'GST']):
                return line
    
    return ""

def extract_invoice_number_and_date(text):
    """Extract Invoice Number and Date - handles multiple formats"""
    invoice_no = ""
    invoice_date = ""
    
    # Try to find invoice number and date together
    combined_patterns = [
        # Pattern: INVOICE No <space> Dated in header, then number and date in table
        r'INVOICE\s+No.*?Dated.*?\n.*?(\d+)\s+([\d]{1,2}[-\.\/][A-Za-z]{3}[-\.\/][\d]{2,4})',
        r'INVOICE\s+No.*?Dated.*?\n.*?(\d+)\s+([\d]{1,2}[-\.\/][\d]{1,2}[-\.\/][\d]{2,4})',
        # Pattern: GST line followed by number and date
        r'GST\s+Tin\s+No[:\-]*[A-Z0-9]+\s+(\d+)\s+([\d]{1,2}[-\.\/][A-Za-z]{3}[-\.\/][\d]{2,4})',
        r'GST\s+Tin\s+No[:\-]*[A-Z0-9]+\s+(\d+)\s+([\d]{1,2}[-\.\/][\d]{1,2}[-\.\/][\d]{2,4})',
        # Pattern: standalone in table format with month names like 22-Dec-25
        r'(\d+)\s+([\d]{1,2}-[A-Za-z]{3}-[\d]{2,4})',
        r'(\d+)\s+([\d]{1,2}\.[A-Za-z]{3}\.[\d]{2,4})',
        r'(\d+)\s+([\d]{1,2}/[A-Za-z]{3}/[\d]{2,4})',
        # Pattern: numeric dates
        r'(\d+)\s+([\d]{1,2}[-\./][\d]{1,2}[-\./][\d]{2,4})',
    ]
    
    for pattern in combined_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            invoice_no = match.group(1).strip()
            invoice_date = match.group(2).strip()
            # Normalize to DD-MM-YYYY format
            invoice_date = normalize_date(invoice_date)
            return invoice_no, invoice_date
    
    # Extract separately if combined search fails
    
    # Invoice Number patterns
    inv_patterns = [
        r'Invoice\s+(?:No|Number)\s*[:\-]?\s*(\d+)',
        r'INVOICE\s+No\s+Dated\s*\n.*?(\d+)',
        r'Invoice\s*#?\s*(\d+)',
        r'Bill\s+No\s*[:\-]?\s*(\d+)',
    ]
    
    for pattern in inv_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            invoice_no = match.group(1).strip()
            break
    
    # Date patterns - handle multiple formats including 22-Dec-25
    date_patterns = [
        # Month name formats (22-Dec-25, 22.Nov.25, 22/Jan/2025)
        r'Dated?\s*[:\-]?\s*([\d]{1,2}[-\.\/][A-Za-z]{3}[-\.\/][\d]{2,4})',
        r'Date\s*[:\-]?\s*([\d]{1,2}[-\.\/][A-Za-z]{3}[-\.\/][\d]{2,4})',
        # Numeric formats (22-11-2025, 22.11.25, 22/11/25)
        r'Dated?\s*[:\-]?\s*([\d]{1,2}[-\.\/][\d]{1,2}[-\.\/][\d]{2,4})',
        r'Date\s*[:\-]?\s*([\d]{1,2}[-\.\/][\d]{1,2}[-\.\/][\d]{2,4})',
        # Generic patterns (without keyword)
        r'([\d]{1,2}-[A-Za-z]{3}-[\d]{2,4})',
        r'([\d]{1,2}\.[A-Za-z]{3}\.[\d]{2,4})',
        r'([\d]{1,2}/[A-Za-z]{3}/[\d]{2,4})',
        r'([\d]{1,2}[-\.\/][\d]{1,2}[-\.\/][\d]{2,4})',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            invoice_date = match.group(1).strip()
            # Normalize to DD-MM-YYYY format
            invoice_date = normalize_date(invoice_date)
            break
    
    return invoice_no, invoice_date

def extract_amount_from_tables(tables):
    """Extract amount from table structure - prioritizes proper currency format"""
    
    candidates = []  # Store potential amounts with their scores
    
    for table in tables:
        if not table:
            continue
        
        # Look for Amount column
        for row_idx, row in enumerate(table):
            if not row:
                continue
            
            # Check if this row has "Amount" header
            row_str = ' '.join([str(cell) for cell in row if cell])
            if 'Amount' in row_str.upper():
                # Found header row, now look for amount values in subsequent rows
                amount_col_idx = None
                for idx, cell in enumerate(row):
                    if cell and 'Amount' in str(cell).upper():
                        amount_col_idx = idx
                        break
                
                if amount_col_idx is not None:
                    # Look for values in Amount column from this point onwards
                    for check_row in table[row_idx+1:]:
                        if amount_col_idx < len(check_row) and check_row[amount_col_idx]:
                            amount_val = str(check_row[amount_col_idx]).strip()
                            # Clean - remove any non-numeric except comma and decimal
                            amount_val = re.sub(r'[^\d,\.]', '', amount_val)
                            if amount_val:
                                try:
                                    amt = float(amount_val.replace(',', ''))
                                    # Score: higher for amounts with decimal points (standard currency format)
                                    score = 10 if '.' in amount_val else 5
                                    # Higher score for amounts > 100
                                    if amt >= 100:
                                        score += 5
                                    # Higher score for proper decimal format (.00, .50, etc)
                                    if re.match(r'^\d{1,3}(,\d{3})*\.\d{2}$', amount_val):
                                        score += 10
                                    
                                    if amt >= 10 and amt < 100000000:
                                        candidates.append((score, amount_val.replace(',', ''), amt))
                                except:
                                    continue
    
    # If we found candidates, return the one with highest score
    if candidates:
        candidates.sort(reverse=True, key=lambda x: (x[0], x[2]))  # Sort by score, then amount
        return candidates[0][1]
    
    # Fallback: Look for any cell with proper currency format (X,XXX.XX)
    for table in tables:
        if not table:
            continue
        for row in reversed(table):  # Start from bottom (totals usually at end)
            if not row:
                continue
            for cell in row:
                if cell:
                    cell_str = str(cell).strip()
                    # Check if it matches proper currency pattern with decimal
                    if re.match(r'^\d{1,3}(,\d{3})*\.\d{2}$', cell_str):
                        try:
                            amt = float(cell_str.replace(',', ''))
                            if amt >= 100 and amt < 100000000:  # Reasonable amount range
                                return cell_str.replace(',', '')
                        except:
                            continue
    
    return ""

def extract_amount(text):
    """Extract total amount - handles multiple formats including table layouts"""
    
    # Patterns for amount - ordered from most specific to most general
    amount_patterns = [
        # Pattern 1: Amount column in table with value (handles "Amount\n3,000.00" or "Amount\n3000.00")
        r'Amount\s*\n\s*([\d,]+\.[\d]{2})',  # Must have decimal point
        
        # Pattern 2: RATE and Amount columns with value at end (handles table rows)
        r'RATE\s+Amount\s*\n[\s\S]*?([\d,]+\.[\d]{2})\s*$',
        
        # Pattern 3: Total/Grand Total with currency symbol
        r'(?:Grand\s+)?Total\s*[:\-]?\s*(?:Rs\.?|INR|₹)\s*([\d,]+\.?[\d]*)',
        r'(?:Net\s+)?(?:Amount|Total)\s*[:\-]?\s*(?:Rs\.?|INR|₹)\s*([\d,]+\.?[\d]*)',
        r'Amount\s+Payable\s*[:\-]?\s*(?:Rs\.?|INR|₹)\s*([\d,]+\.?[\d]*)',
        r'Balance\s+(?:Amount|Due)\s*[:\-]?\s*(?:Rs\.?|INR|₹)\s*([\d,]+\.?[\d]*)',
        
        # Pattern 4: Total without currency symbol (must have decimal or comma formatting)
        r'(?:Grand\s+)?Total\s*[:\-]?\s*([\d,]+\.[\d]{2})',
        r'(?:Net\s+)?(?:Amount|Total)\s*[:\-]?\s*([\d,]+\.[\d]{2})',
        
        # Pattern 5: After QTY, RATE columns, look for amount with decimal
        r'QTY\s+RATE\s+Amount[^\d]*([\d,]+\.[\d]{2})',
        
        # Pattern 6: Amount at the end of service description line
        r'(?:Description|Service).*?Amount.*?\n.*?([\d,]+\.[\d]{2})',
    ]
    
    for pattern in amount_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            amount = match.group(1).replace(',', '').strip()
            # Validate it's a reasonable number
            try:
                amount_float = float(amount)
                # Additional validation: amount should be positive and reasonable
                # Also should not be too small (avoid picking partial numbers)
                if amount_float >= 10 and amount_float < 100000000:  # Between 10 and 10 crore
                    return amount
            except:
                continue
    
    # Fallback: Look for comma-formatted numbers with 2 decimal places (standard currency format)
    # This catches cases where amount is isolated like "3,000.00"
    fallback_pattern = r'\b([\d]{1,3}(?:,[\d]{3})+\.[\d]{2})\b'
    matches = re.findall(fallback_pattern, text)
    if matches:
        # Return the largest reasonable amount
        amounts = []
        for match in matches:
            try:
                amt = float(match.replace(',', ''))
                if amt >= 100 and amt < 100000000:  # Reasonable invoice amount
                    amounts.append((amt, match.replace(',', '')))
            except:
                continue
        if amounts:
            # Return the largest amount (likely to be the total)
            amounts.sort(reverse=True)
            return amounts[0][1]
    
    # Last resort: Look for any number with decimal point (not starting with leading zeros)
    final_pattern = r'\b([1-9][\d,]*\.[\d]{2})\b'
    matches = re.findall(final_pattern, text)
    if matches:
        for match in matches:
            try:
                amt = float(match.replace(',', ''))
                if amt >= 100 and amt < 100000000:
                    return match.replace(',', '')
            except:
                continue
    
    return ""

def extract_account_number(text):
    """Extract bank account number"""
    
    acc_patterns = [
        r'Account\s+Number\s*[:\-]?\s*(\d+)',
        r'A/?c\s+No\.?\s*[:\-]?\s*(\d+)',
        r'Bank\s+Account\s+No\.?\s*[:\-]?\s*(\d+)',
        r'Account\s+No\.?\s*[:\-]?\s*(\d+)',
        r'Acc\.?\s+No\.?\s*[:\-]?\s*(\d+)',
    ]
    
    for pattern in acc_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            account_no = match.group(1).strip()
            account_no = clean_field_value(account_no)
            return account_no
    
    return ""

def extract_ifsc(text):
    """Extract IFSC code - handles multiple formats"""
    
    ifsc_patterns = [
        # Standard IFSC format with label
        r'IFSC\s*(?:Code)?\s*[:\-]?\s*([A-Z]{4}[0-9]{7})',
        # Code- prefix (like "Code- BKID0004500")
        r'Code[-:\s]+([A-Z]{4}[0-9]{7})',
        # Just the IFSC code itself (11 chars: 4 letters + 7 digits)
        r'\b([A-Z]{4}[0-9]{7})\b',
    ]
    
    for pattern in ifsc_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            ifsc = match.group(1).upper().strip()
            # Validate IFSC format
            if len(ifsc) == 11 and re.match(r'^[A-Z]{4}[0-9]{7}$', ifsc):
                return ifsc
    
    return ""

def extract_pan(text):
    """Extract PAN number"""
    
    pan_patterns = [
        # Standard PAN format with label
        r'PAN\s*(?:No\.?)?\s*[:\-]?\s*([A-Z]{5}[0-9]{4}[A-Z])',
        # Just the PAN itself (10 chars: 5 letters + 4 digits + 1 letter)
        r'\b([A-Z]{5}[0-9]{4}[A-Z])\b',
    ]
    
    for pattern in pan_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            pan = match.group(1).upper().strip()
            # Validate PAN format
            if len(pan) == 10 and re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan):
                return pan
    
    return ""

def extract_gst(text):
    """Extract GST number"""
    
    gst_patterns = [
        # Standard GST format with label
        r'GST\s+Tin\s+No[:\-\s]*([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[Z]{1}[0-9A-Z]{1})',
        r'GSTIN\s*[:\-]?\s*([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[Z]{1}[0-9A-Z]{1})',
        r'GST\s*(?:No\.?)?\s*[:\-]?\s*([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[Z]{1}[0-9A-Z]{1})',
        # Just the GST itself (15 chars)
        r'\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[Z]{1}[0-9A-Z]{1})\b',
    ]
    
    for pattern in gst_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            gst = match.group(1).upper().strip()
            # Validate GST format
            if len(gst) == 15:
                return gst
    
    return ""

def extract_invoice_data(pdf_file):
    """Extract data from a single PDF invoice"""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            # Extract text from all pages
            full_text = ""
            all_tables = []
            
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"
                
                # Also extract tables for better amount detection
                tables = page.extract_tables()
                if tables:
                    all_tables.extend(tables)
            
            if not full_text.strip():
                st.warning(f"⚠️ No text found in {pdf_file.name}. It might be a scanned PDF.")
                return None
            
            # Extract all fields using flexible patterns
            party_name = extract_party_name(full_text)
            invoice_no, invoice_date = extract_invoice_number_and_date(full_text)
            amount = extract_amount(full_text)
            
            # If amount not found in text, try to get it from tables
            if not amount and all_tables:
                amount = extract_amount_from_tables(all_tables)
            
            account_no = extract_account_number(full_text)
            ifsc = extract_ifsc(full_text)
            pan = extract_pan(full_text)
            gst = extract_gst(full_text)
            
            # Clean all extracted values
            party_name = clean_field_value(party_name)
            account_no = clean_field_value(account_no)
            ifsc = clean_field_value(ifsc)
            pan = clean_field_value(pan)
            gst = clean_field_value(gst)
            
            # Use PAN if available, otherwise GST
            pan_gst = pan if pan else gst
            
            # Derive bank name from IFSC (after cleaning)
            bank_name = derive_bank_name(ifsc)
            
            return {
                "Party name": party_name,
                "Invoice Date": invoice_date,
                "Invoice No.": invoice_no,
                "Amount": amount,
                "Bank Name": bank_name,
                "Bank Account No": account_no,
                "IFSC Code": ifsc,
                "PAN Number / GST": pan_gst
            }
    
    except Exception as e:
        st.error(f"❌ Error processing {pdf_file.name}: {str(e)}")
        import traceback
        st.text(traceback.format_exc())
        return None

def main():
    st.title("📄 Invoice PDF → Excel Converter")
    st.markdown("Convert multiple invoice PDFs into a single Excel file")
    
    # Add debug mode toggle
    debug_mode = st.sidebar.checkbox("🔍 Debug Mode", value=False, help="Show extracted text for troubleshooting")
    
    # File uploader
    uploaded_files = st.file_uploader(
        "Upload Invoice PDFs",
        type=['pdf'],
        accept_multiple_files=True,
        help="Select one or more PDF invoices"
    )
    
    if uploaded_files:
        st.success(f"✅ {len(uploaded_files)} PDF(s) uploaded")
        
        if st.button("🔄 Process Invoices", type="primary"):
            with st.spinner("Processing invoices..."):
                all_data = []
                failed_files = []
                
                # Progress bar
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                for idx, pdf_file in enumerate(uploaded_files):
                    status_text.text(f"Processing: {pdf_file.name}")
                    
                    # Debug mode: show raw text
                    if debug_mode:
                        try:
                            with pdfplumber.open(pdf_file) as pdf:
                                debug_text = ""
                                for page in pdf.pages:
                                    debug_text += page.extract_text() + "\n"
                                with st.expander(f"📄 Raw text from {pdf_file.name}"):
                                    st.text_area(
                                        "Extracted Text",
                                        debug_text[:3000],
                                        height=300,
                                        key=f"debug_{idx}"
                                    )
                        except Exception as e:
                            st.error(f"Debug error: {e}")
                    
                    data = extract_invoice_data(pdf_file)
                    if data:
                        all_data.append(data)
                    else:
                        failed_files.append(pdf_file.name)
                    
                    # Update progress
                    progress_bar.progress((idx + 1) / len(uploaded_files))
                
                status_text.empty()
                
                if all_data:
                    # Create DataFrame with exact column order
                    df = pd.DataFrame(all_data, columns=[
                        "Party name",
                        "Invoice Date",
                        "Invoice No.",
                        "Amount",
                        "Bank Name",
                        "Bank Account No",
                        "IFSC Code",
                        "PAN Number / GST"
                    ])
                    
                    st.success(f"✅ Successfully processed {len(all_data)} invoice(s)")
                    
                    if failed_files:
                        st.warning(f"⚠️ Failed to process {len(failed_files)} file(s): {', '.join(failed_files)}")
                    
                    # Display table
                    st.subheader("📊 Extracted Data")
                    st.dataframe(df, use_container_width=True)
                    
                    # Export to Excel
                    output = BytesIO()
                    with pd.ExcelWriter(output, engine='openpyxl') as writer:
                        df.to_excel(writer, index=False, sheet_name='Invoices')
                        
                        # Auto-adjust column widths
                        worksheet = writer.sheets['Invoices']
                        for idx, col in enumerate(df.columns):
                            max_length = max(
                                df[col].astype(str).apply(len).max(),
                                len(col)
                            ) + 2
                            worksheet.column_dimensions[chr(65 + idx)].width = min(max_length, 50)
                    
                    excel_data = output.getvalue()
                    
                    # Download button
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    st.download_button(
                        label="⬇️ Download Excel File",
                        data=excel_data,
                        file_name=f"invoices_{timestamp}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.error("❌ No data could be extracted from any of the uploaded PDFs")
                    st.info("💡 Tips: Make sure your PDFs are text-based (not scanned images) and contain the expected fields.")
    
    else:
        st.info("👆 Please upload one or more invoice PDFs to get started")
    
    # Footer
    st.markdown("---")
    st.markdown("**Business Rules:**")
    st.markdown("• Party name is extracted from invoice header or account holder field")
    st.markdown("• PAN is extracted if available, otherwise GST")
    st.markdown("• Bank name is automatically derived from IFSC code")
    st.markdown("• Supports multiple invoice formats and layouts")
    
    # Sidebar info
    with st.sidebar:
        st.markdown("### 📝 Supported Fields")
        st.markdown("""
        **The app will automatically detect:**
        - Party Name / Account Holder
        - Invoice Number
        - Invoice Date (multiple formats)
        - Total Amount
        - Bank Account Number
        - IFSC Code
        - PAN Number
        - GST Number
        
        **Supported date formats:**
        - DD-MMM-YYYY (e.g., 12-Nov-25)
        - DD.MM.YYYY (e.g., 22.11.2025)
        - DD/MM/YYYY (e.g., 22/11/2025)
        """)
        
        st.markdown("---")
        st.markdown("**💡 Tips:**")
        st.markdown("• Enable Debug Mode to see extracted text")
        st.markdown("• Works with multiple invoice formats")
        st.markdown("• Upload multiple PDFs at once")

if __name__ == "__main__":
    main()
