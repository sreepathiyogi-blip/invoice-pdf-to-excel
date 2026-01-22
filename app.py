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
    
    ifsc_upper = ifsc.upper()
    if ifsc_upper.startswith("HDFC"):
        return "HDFC Bank"
    elif ifsc_upper.startswith("ICIC"):
        return "ICICI Bank"
    elif ifsc_upper.startswith("SBIN"):
        return "SBI"
    else:
        # Return first 4 characters as bank identifier
        return ifsc[:4].upper()

def extract_value_after_keyword(text, keyword):
    """Extract value after a keyword"""
    pattern = rf"{re.escape(keyword)}\s*[:\-]?\s*([^\n]+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""

def extract_invoice_data(pdf_file):
    """Extract data from a single PDF invoice"""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            # Extract text from all pages
            full_text = ""
            for page in pdf.pages:
                full_text += page.extract_text() + "\n"
            
            # Extract fields
            invoice_no = extract_value_after_keyword(full_text, "INVOICE No")
            if not invoice_no:
                invoice_no = extract_value_after_keyword(full_text, "Invoice No")
            
            invoice_date = extract_value_after_keyword(full_text, "Dated")
            
            # Extract Amount (Total)
            amount = extract_value_after_keyword(full_text, "Total")
            # Clean amount - remove currency symbols and extra text
            if amount:
                amount_match = re.search(r'[\d,]+\.?\d*', amount)
                if amount_match:
                    amount = amount_match.group(0).replace(',', '')
            
            # Extract bank details
            account_no = extract_value_after_keyword(full_text, "Account Number")
            if not account_no:
                account_no = extract_value_after_keyword(full_text, "Account No")
            
            ifsc = extract_value_after_keyword(full_text, "IFSC")
            if not ifsc:
                ifsc = extract_value_after_keyword(full_text, "IFSC Code")
            
            # Extract PAN or GST
            pan = extract_value_after_keyword(full_text, "PAN :")
            if not pan:
                pan = extract_value_after_keyword(full_text, "PAN")
            
            gst = extract_value_after_keyword(full_text, "GST Tin No")
            if not gst:
                gst = extract_value_after_keyword(full_text, "GSTIN")
            
            # Use PAN if available, otherwise GST
            pan_gst = pan if pan else gst
            
            # Derive bank name from IFSC
            bank_name = derive_bank_name(ifsc)
            
            return {
                "Party name": "Tushar Chutani",
                "Invoice Date": invoice_date,
                "Invoice No.": invoice_no,
                "Amount": amount,
                "Bank Name": bank_name,
                "Bank Account No": account_no,
                "IFSC Code": ifsc,
                "PAN Number / GST": pan_gst
            }
    
    except Exception as e:
        st.error(f"Error processing PDF: {str(e)}")
        return None

def main():
    st.title("📄 Invoice PDF → Excel Converter")
    st.markdown("Convert multiple invoice PDFs into a single Excel file")
    
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
                
                # Progress bar
                progress_bar = st.progress(0)
                
                for idx, pdf_file in enumerate(uploaded_files):
                    data = extract_invoice_data(pdf_file)
                    if data:
                        all_data.append(data)
                    
                    # Update progress
                    progress_bar.progress((idx + 1) / len(uploaded_files))
                
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
                            )
                            worksheet.column_dimensions[chr(65 + idx)].width = max_length + 2
                    
                    excel_data = output.getvalue()
                    
                    # Download button
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    st.download_button(
                        label="⬇️ Download Excel File",
                        data=excel_data,
                        file_name=f"invoices_{timestamp}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                else:
                    st.error("❌ No data could be extracted from the uploaded PDFs")
    
    else:
        st.info("👆 Please upload one or more invoice PDFs to get started")
    
    # Footer
    st.markdown("---")
    st.markdown("**Business Rules:**")
    st.markdown("• Party name is always: `Tushar Chutani`")
    st.markdown("• PAN is extracted if available, otherwise GST")
    st.markdown("• Bank name is derived from IFSC code")

if __name__ == "__main__":
    main()
