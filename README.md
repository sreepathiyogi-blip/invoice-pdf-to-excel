Invoice PDF → Excel Converter
A Streamlit web application that converts multiple invoice PDFs (with the same format) into a single Excel file.

🚀 Features
📄 Upload multiple PDF invoices at once
🔄 Automatic data extraction from text-based PDFs
📊 Preview extracted data before export
💾 Export to Excel with auto-formatted columns
🏦 Automatic bank name derivation from IFSC code
📋 Extracted Fields
Party name (Fixed: Tushar Chutani)
Invoice Date
Invoice No.
Amount
Bank Name (Derived from IFSC)
Bank Account No
IFSC Code
PAN Number / GST
🛠️ Installation
bash
# Clone the repository
git clone https://github.com/sreepathiyogi-blip/invoice-pdf-to-excel.git
cd invoice-pdf-to-excel

# Install dependencies
pip install -r requirements.txt
💻 Usage
bash
# Run the Streamlit app
streamlit run app.py
The app will open in your browser at http://localhost:8501

📦 Requirements
Python 3.8+
streamlit
pdfplumber
pandas
openpyxl
🎯 Business Rules
Party name is always: Tushar Chutani
PAN is extracted if available, otherwise GST
Bank name is derived from IFSC code (HDFC → HDFC Bank, ICIC → ICICI Bank, SBIN → SBI)
Submission Date is not included in the output
📄 License
MIT License

👤 Author
Tushar Chutani

⭐ If you find this project useful, please consider giving it a star!

