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
    
    # Clean the IFSC code first - remove any "Code :" prefix
    ifsc = re.sub(r'^(Code|IFSC)\s*:\s*', '', ifsc, flags=re.IGNORECASE).strip()
    
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
    else:
        # Return first 4 characters as bank identifier
        return ifsc[:4].upper() if len(ifsc) >= 4 else ifsc.upper()

def clean_field_value(value):
    """Remove common prefixes like 'Name :', 'Code :', etc."""
    if not value:
        return ""
    
    # Remove common prefixes
    value = re.sub(r'^(Name|Code|Number|Account\s+Number|IFSC|PAN|GST)\s*:\s*', '', value, flags=re.IGNORECASE)
    
    return value.strip()

def extract_party_name(text):
    """Extract Account Holder name from bank details section"""
    # Look for "Account Holder:" pattern
    patterns = [
        r'Account\s+Holder\s*:\s*(?:Name\s*:\s*)?([A-Z\s]+?)(?:\n|Account)',
        r'Account\s+Holder\s*:\s*(?:Name\s*:\s*)?([^\n]+)',
        r'Name\s*:\s*([A-Z][A-Z\s]+?)(?:\n)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            # Clean the name
            name = clean_field_value(name)
            if name and len(name) > 2:
                return name
    
    return ""

def extract_value_after_keyword(text, keyword):
    """Extract value after a keyword with flexible matching"""
    patterns = [
        rf"{re.escape(keyword)}\s*[:\-]?\s*([^\n]+)",
        rf"{re.escape(keyword)}\s*[:\-]?\s*[\r\n]+\s*([^\n]+)",
        rf"{keyword}\s+([^\n]+)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            # Clean the value
            value = clean_field_value(value)
            if value and value != "":
                return value
    
    return ""

def extract_invoice_number_and_date(text):
    """Extract Invoice Number and Date from the header table"""
    # The invoice has this structure:
    # Bill to Place of Supply INVOICE No Dated
    # [Company info lines]
    # GST Tin No:-06AAFCI1834E1ZX 1 12-Nov-25
    
    # Pattern 1: Find "INVOICE No Dated" header, then look for number and date pattern after GST
    pattern1 = r'GST\s+Tin\s+No[:\-]*[A-Z0-9]+\s+(\d+)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})'
    match = re.search(pattern1, text, re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    
    # Pattern 2: Find standalone number and date at the end of a line after GST
    pattern2 = r'[A-Z0-9]{15}\s+(\d+)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})'
    match = re.search(pattern2, text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    
    # Pattern 3: Look for the header line, then search for invoice number and date in the next few lines
    lines = text.split('\n')
    found_header = False
    for i, line in enumerate(lines):
        if 'INVOICE No' in line and 'Dated' in line:
            found_header = True
            # Look in the next 10 lines for a number followed by a date
            for j in range(i+1, min(i+11, len(lines))):
                search_line = lines[j]
                # Find pattern: number followed by date format
                date_pattern = r'(\d+)\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})'
                match = re.search(date_pattern, search_line)
                if match:
                    return match.group(1).strip(), match.group(2).strip()
    
    # Fallback: extract separately
    invoice_no = ""
    invoice_date = ""
    
    # Extract just the invoice number (single digit or multi-digit)
    inv_patterns = [
        r'(?:INVOICE|Invoice)\s+(?:No|Number)\s+Dated\s+(\d+)',
        r'GST[^0-9]*[A-Z0-9]{15}\s+(\d+)\s+\d{1,2}-',
    ]
    for pattern in inv_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            invoice_no = match.group(1).strip()
            break
    
    # Extract Date (looking for format like "12-Nov-25" or "12-Nov-2025")
    date_patterns = [
        r'(\d{1,2}-[A-Za-z]{3}-\d{2,4})',  # Generic date pattern
        r'Dated\s+\d+\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            invoice_date = match.group(1).strip()
            break
    
    return invoice_no, invoice_date

def clean_amount(amount_str):
    """Clean and extract numeric amount"""
    if not amount_str:
        return ""
    
    # Remove common currency symbols and text
    amount_str = re.sub(r'Rs\.?|INR|₹|USD|\$', '', amount_str, flags=re.IGNORECASE)
    
    # Extract number (including decimals and commas)
    match = re.search(r'[\d,]+\.?\d*', amount_str)
    if match:
        return match.group(0).replace(',', '')
    
    return ""

def extract_invoice_data(pdf_file):
    """Extract data from a single PDF invoice"""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            # Extract text from all pages
            full_text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"
            
            if not full_text.strip():
                st.warning(f"⚠️ No text found in {pdf_file.name}. It might be a scanned PDF.")
                return None
            
            # Extract Party Name (Account Holder)
            party_name = extract_party_name(full_text)
            if not party_name:
                # Fallback: try to get from "Account Holder" line
                party_name = extract_value_after_keyword(full_text, "Account Holder")
            
            # Extract Invoice Number and Date
            invoice_no, invoice_date = extract_invoice_number_and_date(full_text)
            
            # Extract Total Amount
            # Look for "Total" followed by amount on same or next line
            total_pattern = r'Total\s+[\(\s]*(\d+[,\d]*\.?\d*)[\)\s]*'
            total_match = re.search(total_pattern, full_text, re.IGNORECASE)
            if total_match:
                amount = total_match.group(1).replace(',', '')
            else:
                amount_raw = extract_value_after_keyword(full_text, "Total")
                amount = clean_amount(amount_raw)
            
            # Extract Account Number
            account_no = extract_value_after_keyword(full_text, "Account Number")
            if not account_no:
                # Try pattern: "Account Number: 50100249073102"
                acc_pattern = r'Account\s+Number\s*:\s*(\d+)'
                acc_match = re.search(acc_pattern, full_text, re.IGNORECASE)
                if acc_match:
                    account_no = acc_match.group(1)
            
            # Clean account number
            account_no = clean_field_value(account_no)
            
            # Extract IFSC Code
            ifsc = extract_value_after_keyword(full_text, "IFSC")
            if not ifsc:
                # Try pattern: "IFSC: HDFC0001993" or "Code : HDFC0000148"
                ifsc_pattern = r'(?:IFSC|Code)\s*:\s*([A-Z0-9]+)'
                ifsc_match = re.search(ifsc_pattern, full_text, re.IGNORECASE)
                if ifsc_match:
                    ifsc = ifsc_match.group(1)
            
            # Clean IFSC
            ifsc = clean_field_value(ifsc)
            
            # Extract PAN
            pan = extract_value_after_keyword(full_text, "PAN :")
            if not pan:
                pan = extract_value_after_keyword(full_text, "PAN")
            if not pan:
                # Try pattern: "PAN : BNJPT1071M"
                pan_pattern = r'PAN\s*:\s*([A-Z0-9]+)'
                pan_match = re.search(pan_pattern, full_text, re.IGNORECASE)
                if pan_match:
                    pan = pan_match.group(1)
            
            # Clean PAN
            pan = clean_field_value(pan)
            
            # Extract GST
            gst = extract_value_after_keyword(full_text, "GST Tin No")
            if not gst:
                gst = extract_value_after_keyword(full_text, "GSTIN")
            if not gst:
                # Try pattern: "GST Tin No:-06AAFCI1834E1ZX"
                gst_pattern = r'GST\s+Tin\s+No[-:\s]*([A-Z0-9]+)'
                gst_match = re.search(gst_pattern, full_text, re.IGNORECASE)
                if gst_match:
                    gst = gst_match.group(1)
            
            # Clean GST
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
        help="Select one or more PDF invoices with the same format"
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
    st.markdown("• Party name is extracted from 'Account Holder' field")
    st.markdown("• PAN is extracted if available, otherwise GST")
    st.markdown("• Bank name is derived from IFSC code")
    
    # Sidebar info
    with st.sidebar:
        st.markdown("### 📝 Expected PDF Fields")
        st.markdown("""
        **From your invoice format:**
        - INVOICE No (top right)
        - Dated (top right)
        - Total (bottom)
        - Account Holder (bank details)
        - Account Number (bank details)
        - IFSC (bank details)
        - PAN (bank details)
        - GST Tin No (supplier info)
        """)

if __name__ == "__main__":
    main()
