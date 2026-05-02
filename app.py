#!/usr/bin/env python3
"""
PDF Bank Statement Transaction Separator - Web App
==================================================
A Flask web application for extracting and filtering transactions from PDF bank statements.
Deployable on Koyeb, Railway, Render, Heroku, etc.

Modern Hybrid Parser:
- pdfplumber for layout-aware table extraction
- Camelot for lattice/grid table fallback
- Advanced regex with multi-line description handling
- Indian number format support (1,26,480.00)
- OCR artifact cleanup (cR->CR, whitespace normalization)
"""

import os
import re
import json
import uuid
import shutil
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from functools import wraps

from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for, session
from werkzeug.utils import secure_filename

# PDF and Report generation
try:
    import pdfplumber
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor, black, white, Color
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
    from reportlab.pdfgen import canvas
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.piecharts import Pie
except ImportError:
    os.system("pip install pdfplumber reportlab -q")
    import pdfplumber
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor, black, white, Color
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY

# Optional: Camelot for advanced table extraction (install with: pip install camelot-py[cv])
try:
    import camelot
    CAMELOT_AVAILABLE = True
except ImportError:
    CAMELOT_AVAILABLE = False

# Optional: pandas for DataFrame handling
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}

# Ensure directories exist
for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
    os.makedirs(folder, exist_ok=True)


@dataclass
class Transaction:
    """Represents a single bank transaction."""
    date: str
    value_date: str
    description: str
    amount: float
    transaction_type: str
    balance: float
    branch: str
    raw_text: str = ""

    def to_dict(self):
        return asdict(self)


class PDFStatementParser:
    """
    Modern hybrid parser for Bangladeshi bank statements.

    Parsing strategy (in order of preference):
    1. pdfplumber table extraction (layout-aware)
    2. Camelot lattice extraction (grid-based tables)
    3. Advanced regex with multi-line description handling
    """

    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self.raw_text = ""
        self.transactions: List[Transaction] = []
        self.account_info = {}
        self.parse_errors = []
        self.df_pages: List[Dict[str, Any]] = []
        self.page_layouts: List[Dict] = []

    # ========================================================================
    # TEXT EXTRACTION
    # ========================================================================

    def extract_text(self) -> str:
        """Extract text using pdfplumber with layout preservation."""
        text = ""
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    # Extract with layout preservation
                    page_text = page.extract_text(layout=True)
                    if page_text:
                        text += f"\n--- PAGE {page_num} ---\n" + page_text + "\n"

                    # Store page metadata for table extraction
                    self.page_layouts.append({
                        'page': page_num,
                        'width': page.width,
                        'height': page.height,
                        'chars': len(page_text) if page_text else 0
                    })

                    # Extract tables using pdfplumber
                    tables = page.extract_tables()
                    for table in tables:
                        if table and len(table) > 1:
                            self.df_pages.append({
                                'page': page_num,
                                'type': 'pdfplumber_table',
                                'data': table
                            })
        except Exception as e:
            self.parse_errors.append(f"pdfplumber error: {str(e)}")

        # Fallback to Camelot for lattice tables
        if CAMELOT_AVAILABLE and not self.df_pages:
            try:
                tables = camelot.read_pdf(str(self.pdf_path), pages='all', flavor='lattice')
                for i, table in enumerate(tables):
                    self.df_pages.append({
                        'page': table.page,
                        'type': 'camelot_lattice',
                        'data': table.data
                    })
            except Exception as e:
                self.parse_errors.append(f"Camelot error: {str(e)}")

        self.raw_text = text
        return text

    # ========================================================================
    # MAIN PARSER
    # ========================================================================

    def parse_transactions(self) -> List[Transaction]:
        """Parse transactions using multi-strategy approach."""

        # Strategy 1: Parse from extracted tables (DataFrames)
        if self.df_pages:
            for page_data in self.df_pages:
                self._parse_from_table_data(page_data)

        # Strategy 2: If tables failed or incomplete, use advanced regex on raw text
        if not self.transactions:
            self._parse_from_text_advanced()

        # Strategy 3: If still no transactions, try simple regex fallback
        if not self.transactions:
            self._parse_from_text_simple()

        # Post-processing: clean up descriptions and normalize
        self._post_process_transactions()

        return self.transactions

    # ========================================================================
    # TABLE-BASED PARSING
    # ========================================================================

    def _parse_from_table_data(self, page_data: Dict):
        """Parse transactions from extracted table data."""
        table = page_data['data']

        if not table or len(table) < 2:
            return

        # Convert to DataFrame if pandas available
        if PANDAS_AVAILABLE:
            try:
                df = pd.DataFrame(table[1:], columns=table[0] if table[0] else None)
                df = df.replace('', None).dropna(how='all')
                if df.empty:
                    return
                self._parse_from_dataframe(df, page_data['page'])
                return
            except Exception as e:
                self.parse_errors.append(f"DataFrame parse error page {page_data['page']}: {str(e)}")

        # Manual parsing without pandas
        headers = table[0] if table[0] else []
        for row in table[1:]:
            if not row or all(not cell for cell in row):
                continue
            try:
                txn = self._parse_table_row(row, headers, page_data['page'])
                if txn:
                    self.transactions.append(txn)
            except Exception as e:
                self.parse_errors.append(f"Table row parse error: {str(e)}")

    def _parse_from_dataframe(self, df: pd.DataFrame, page_num: int):
        """Parse from pandas DataFrame."""
        # Normalize column names
        original_cols = list(df.columns)
        df.columns = [str(c).strip().upper().replace(' ', '_').replace('.', '') for c in df.columns]

        # Map Sonali Bank columns (flexible matching)
        column_map = {}
        for col in df.columns:
            col_upper = str(col).upper()
            if any(x in col_upper for x in ['DATE', 'VALUE_DATE', 'VALUEDATE']):
                if 'VALUE' in col_upper:
                    column_map[col] = 'value_date'
                else:
                    column_map[col] = 'date'
            elif any(x in col_upper for x in ['TRANS', 'DESC', 'PARTICULAR', 'NARRATION']):
                column_map[col] = 'description'
            elif any(x in col_upper for x in ['DEBIT', 'DR', 'WITHDRAWAL']):
                column_map[col] = 'debit'
            elif any(x in col_upper for x in ['CREDIT', 'CR', 'DEPOSIT']):
                column_map[col] = 'credit'
            elif any(x in col_upper for x in ['BALANCE', 'RUNNING']):
                column_map[col] = 'balance'
            elif any(x in col_upper for x in ['BRANCH', 'ORIGINATING', 'OFFICE']):
                column_map[col] = 'branch'

        if column_map:
            df = df.rename(columns=column_map)

        for _, row in df.iterrows():
            try:
                txn = self._parse_dataframe_row(row, page_num)
                if txn:
                    self.transactions.append(txn)
            except Exception as e:
                self.parse_errors.append(f"DataFrame row error page {page_num}: {str(e)}")

    def _parse_dataframe_row(self, row: Any, page_num: int) -> Optional[Transaction]:
        """Parse a single DataFrame row into Transaction."""
        row_dict = row.to_dict() if hasattr(row, 'to_dict') else dict(row)

        # Extract date
        date_str = self._extract_date(str(row_dict.get('date', '')))
        if not date_str:
            return None

        value_date = self._extract_date(str(row_dict.get('value_date', date_str)))

        # Extract amounts
        debit = self._parse_amount(str(row_dict.get('debit', '0')))
        credit = self._parse_amount(str(row_dict.get('credit', '0')))

        if credit > 0:
            amount = credit
            txn_type = 'CR'
        elif debit > 0:
            amount = debit
            txn_type = 'DR'
        else:
            # Check raw text for CR/DR indicators
            raw = str(row_dict)
            if 'CR' in raw:
                txn_type = 'CR'
            elif 'DR' in raw:
                txn_type = 'DR'
            else:
                txn_type = 'CR'
            amount = 0.0

        # Balance
        balance = self._parse_amount(str(row_dict.get('balance', '0')))

        # Description & Branch
        desc = str(row_dict.get('description', ''))
        branch = str(row_dict.get('branch', 'Unknown'))

        # Clean description
        desc = self._clean_description(desc, amount, balance)

        return Transaction(
            date=date_str,
            value_date=value_date,
            description=desc,
            amount=amount,
            transaction_type=txn_type,
            balance=balance,
            branch=branch,
            raw_text=str(row_dict)[:200]
        )

    def _parse_table_row(self, row: List, headers: List, page_num: int) -> Optional[Transaction]:
        """Parse a single table row (list format) into Transaction."""
        if not row or len(row) < 3:
            return None

        # Try to identify columns by content
        date_str = None
        value_date = None
        description = ""
        debit = 0.0
        credit = 0.0
        balance = 0.0
        branch = "Unknown"

        for i, cell in enumerate(row):
            if not cell:
                continue
            cell_str = str(cell).strip()

            # Date detection
            if not date_str:
                dm = re.match(r'(\d{2}-[A-Za-z]{3}-\d{4})', cell_str)
                if dm:
                    date_str = dm.group(1)
                    continue

            # Value date detection
            if date_str and not value_date:
                dm = re.match(r'(\d{2}-[A-Za-z]{3}-\d{4})', cell_str)
                if dm and dm.group(1) != date_str:
                    value_date = dm.group(1)
                    continue

            # Amount detection
            amts = re.findall(r'\d{1,3}(?:,\d{3})*\.\d{2}', cell_str)
            if amts:
                for amt_str in amts:
                    amt = float(amt_str.replace(',', ''))
                    if 'CR' in cell_str or 'cr' in cell_str:
                        credit = amt
                    elif 'DR' in cell_str or 'dr' in cell_str or 'Debit' in cell_str:
                        debit = amt
                    elif credit == 0 and debit == 0:
                        # Assume first amount is transaction, second is balance
                        if amount == 0:
                            amount = amt
                        else:
                            balance = amt

            # Branch detection
            bm = re.search(r'([A-Za-z\s]+Branch)', cell_str, re.I)
            if bm:
                branch = bm.group(1).strip()

            # Description accumulation
            if not re.match(r'^(CR|DR|Balance|Total|\d{1,3}(?:,\d{3})*\.\d{2})$', cell_str):
                description += " " + cell_str

        if not date_str:
            return None

        if credit > 0:
            amount = credit
            txn_type = 'CR'
        elif debit > 0:
            amount = debit
            txn_type = 'DR'
        else:
            amount = 0.0
            txn_type = 'CR'

        description = self._clean_description(description, amount, balance)

        return Transaction(
            date=date_str,
            value_date=value_date or date_str,
            description=description,
            amount=amount,
            transaction_type=txn_type,
            balance=balance,
            branch=branch,
            raw_text=str(row)[:200]
        )

    # ========================================================================
    # ADVANCED TEXT-BASED PARSING
    # ========================================================================

    def _parse_from_text_advanced(self):
        """
        Advanced regex parsing with multi-line description handling.
        Handles Sonali Bank's complex layout with wrapped descriptions.
        """
        date_pattern = r'\d{2}-[A-Za-z]{3}-\d{4}'

        # Split into pages
        pages = re.split(r'--- PAGE (\d+) ---', self.raw_text)

        for idx in range(1, len(pages), 2):
            if idx >= len(pages):
                break
            page_num = pages[idx]
            page_content = pages[idx + 1] if idx + 1 < len(pages) else ""

            if not page_content.strip():
                continue

            lines = page_content.split('\n')
            i = 0

            while i < len(lines):
                line = lines[i].strip()

                # Skip header/footer lines
                if self._is_header_footer(line):
                    i += 1
                    continue

                # Match date pattern at start: "06-Jan-2025  06-Jan-2025"
                date_match = re.match(
                    rf'^({date_pattern})\s+({date_pattern})(.*)', 
                    line
                )

                if date_match:
                    txn_date = date_match.group(1)
                    value_date = date_match.group(2)
                    remainder = date_match.group(3).strip()

                    desc_lines = []
                    if remainder:
                        desc_lines.append(remainder)

                    amounts = []
                    branch = "Unknown"
                    txn_type = 'CR'

                    # Look ahead for continuation lines
                    j = i + 1
                    while j < len(lines):
                        next_line = lines[j].strip()

                        # Stop conditions
                        if self._is_transaction_boundary(next_line, date_pattern):
                            break

                        # Extract amounts
                        amts = re.findall(r'\d{1,3}(?:,\d{3})*\.\d{2}', next_line)
                        amounts.extend(amts)

                        # Determine type
                        if re.search(r'\bCR\b', next_line):
                            txn_type = 'CR'
                        elif re.search(r'\bDR\b', next_line):
                            txn_type = 'DR'

                        # Extract branch
                        branch = self._extract_branch(next_line) or branch

                        # Add to description (filter out pure amounts/labels)
                        if not self._is_amount_only_line(next_line):
                            desc_lines.append(next_line)

                        j += 1

                    # Build full description
                    full_text = ' '.join(desc_lines)

                    # Parse amounts
                    amount, balance = self._parse_amounts_from_list(amounts, txn_type)

                    if amount is None:
                        i = j if j > i else i + 1
                        continue

                    # Clean description
                    description = self._clean_description(full_text, amount, balance)

                    # Fallback branch extraction
                    if branch == "Unknown" and desc_lines:
                        branch = self._extract_branch(desc_lines[0]) or "Unknown"

                    txn = Transaction(
                        date=txn_date,
                        value_date=value_date,
                        description=description,
                        amount=amount,
                        transaction_type=txn_type,
                        balance=balance,
                        branch=branch,
                        raw_text=full_text[:200]
                    )
                    self.transactions.append(txn)
                    i = j
                    continue

                i += 1

    def _parse_from_text_simple(self):
        """Simple fallback regex parser for basic cases."""
        date_pattern = r'\d{2}-[A-Za-z]{3}-\d{4}'

        # Find all date-date patterns and extract surrounding context
        pattern = re.compile(
            rf'({date_pattern})\s+({date_pattern})\s+(.*?)\s+(\d{{1,3}}(?:,\d{{3}})*\.\d{{2}})\s+(\d{{1,3}}(?:,\d{{3}})*\.\d{{2}})',
            re.DOTALL
        )

        matches = pattern.findall(self.raw_text)
        for match in matches:
            txn_date, value_date, desc, amt_str, bal_str = match

            amount = float(amt_str.replace(',', ''))
            balance = float(bal_str.replace(',', ''))

            txn_type = 'CR' if 'CR' in desc else 'DR'

            description = self._clean_description(desc, amount, balance)

            txn = Transaction(
                date=txn_date,
                value_date=value_date,
                description=description,
                amount=amount,
                transaction_type=txn_type,
                balance=balance,
                branch="Unknown",
                raw_text=desc[:200]
            )
            self.transactions.append(txn)

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def _is_header_footer(self, line: str) -> bool:
        """Check if line is a header or footer element."""
        header_patterns = [
            r'^User ID\.',
            r'^Print Date',
            r'^Print Time',
            r'^Generated By',
            r'^STATEMENT OF ACCOUNT',
            r'^SonaliBank',
            r'^MR\.\s+[A-Z]',
            r'^JAMALKHAN',
            r'^ANDERKILLA',
            r'^GPO\s+\d+',
            r'^CTG$',
            r'^Branch\s*:',
            r'^Currency\s*:',
            r'^Opening Date\s*:',
            r'^Account Number\s*:',
            r'^Interest Rate\s*:',
            r'^Account Type\s*:',
            r'^Period\s*:',
            r'^Status\s*:',
            r'^Routing Number\s*:',
            r'^Page \d+ of \d+',
            r'^\*+End of Report\*+',
            r'^Total$',
            r'^Balance [BC]/F',
            r'^Grand Total',
        ]

        for pattern in header_patterns:
            if re.match(pattern, line, re.IGNORECASE):
                return True

        return False

    def _is_transaction_boundary(self, line: str, date_pattern: str) -> bool:
        """Check if line marks the end of current transaction."""
        # Next transaction starts
        if re.match(rf'^{date_pattern}', line):
            return True

        # Page/footer markers
        boundary_patterns = [
            r'^Total\s*$',
            r'^Page \d+ of \d+',
            r'^Balance [BC]/F',
            r'^Grand Total',
            r'^User ID\.',
            r'^Generated By',
            r'^STATEMENT OF ACCOUNT',
            r'^\*+End of Report\*+',
            r'^--- PAGE',
        ]

        for pattern in boundary_patterns:
            if re.match(pattern, line, re.IGNORECASE):
                return True

        return False

    def _is_amount_only_line(self, line: str) -> bool:
        """Check if line contains only amounts/labels."""
        stripped = line.strip()

        # Pure amounts
        if re.match(r'^\d{1,3}(?:,\d{3})*\.\d{2}$', stripped):
            return True

        # Labels
        if stripped in ['CR', 'DR', 'Balance', 'Total', 'Credit', 'Debit']:
            return True

        return False

    def _extract_branch(self, text: str) -> Optional[str]:
        """Extract branch name from text."""
        branch_patterns = [
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Chattogram)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Khagrachari)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Coxs Bazar)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Dhaka)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*)',
            r'(HEAD OFFICE)',
            r'(Local Office[^,\n]*)',
            r'(G\.M\. OFFICE[^,\n]*)',
            r'(P\.O\. [^,\n]*)',
        ]

        for pattern in branch_patterns:
            bm = re.search(pattern, text, re.IGNORECASE)
            if bm:
                return bm.group(1).strip()

        return None

    def _parse_amounts_from_list(self, amounts: List[str], txn_type: str) -> tuple:
        """
        Parse amount and balance from list of amount strings.
        Returns (amount, balance) or (None, None) if invalid.
        """
        if len(amounts) >= 2:
            try:
                # Last two are typically amount and balance
                amount = float(amounts[-2].replace(',', ''))
                balance = float(amounts[-1].replace(',', ''))
                return amount, balance
            except ValueError:
                return None, None
        elif len(amounts) == 1:
            try:
                amount = float(amounts[0].replace(',', ''))
                return amount, 0.0
            except ValueError:
                return None, None
        else:
            return None, None

    def _extract_date(self, text: str) -> str:
        """Extract and normalize date from text."""
        match = re.search(r'(\d{2})-([A-Za-z]{3})-(\d{4})', text)
        if match:
            day, month, year = match.groups()
            # Normalize month to title case
            month = month.title()
            return f"{day}-{month}-{year}"
        return ""

    def _parse_amount(self, text: str) -> float:
        """Parse amount with comma handling and Indian numbering."""
        if not text or text.strip() in ['', '-', 'nan', 'None', 'null']:
            return 0.0

        try:
            # Remove all commas first
            cleaned = text.replace(',', '').strip()
            return float(cleaned)
        except ValueError:
            return 0.0

    def _clean_description(self, text: str, amount: float, balance: float) -> str:
        """Clean transaction description."""
        if not text:
            return ""

        # Remove amount strings
        text = re.sub(r'\d{1,3}(?:,\d{3})*\.\d{2}', '', text)

        # Remove CR/DR markers (case insensitive)
        text = re.sub(r'\b(CR|DR|cr|dr)\b', '', text)

        # Remove common noise words
        noise_words = [
            'Balance B/F', 'Balance C/F', 'Opening Balance', 'Closing Balance',
            'Total', 'Page', 'User ID', 'Generated By', 'Print Date', 'Print Time',
            'STATEMENT OF ACCOUNT', 'SonaliBank', 'Jamalkhan Road Branch',
            'Chattogram', 'HEAD OFFICE', 'Local Office', 'G.M. OFFICE'
        ]
        for word in noise_words:
            text = text.replace(word, '')

        # Clean whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Remove leading/trailing punctuation
        text = text.strip('.,;:- ')

        # Truncate to reasonable length
        return text[:150]

    def _post_process_transactions(self):
        """Post-process all transactions for consistency."""
        for txn in self.transactions:
            # Ensure date format is consistent
            txn.date = self._normalize_date(txn.date)
            txn.value_date = self._normalize_date(txn.value_date)

            # Clean up branch names
            txn.branch = self._normalize_branch(txn.branch)

            # Ensure transaction type is uppercase
            txn.transaction_type = txn.transaction_type.upper()

            # Round amounts
            txn.amount = round(txn.amount, 2)
            txn.balance = round(txn.balance, 2)

    def _normalize_date(self, date_str: str) -> str:
        """Normalize date string to DD-Mmm-YYYY format."""
        if not date_str:
            return ""

        match = re.match(r'(\d{2})-([A-Za-z]{3})-(\d{4})', date_str)
        if match:
            day, month, year = match.groups()
            month = month.title()
            return f"{day}-{month}-{year}"

        return date_str

    def _normalize_branch(self, branch: str) -> str:
        """Normalize branch name."""
        if not branch or branch == "Unknown":
            return "Unknown"

        # Clean up common variations
        branch = branch.strip()

        # Remove trailing punctuation
        branch = branch.rstrip('.,;')

        # Standardize "Chattogram" spellings
        branch = re.sub(r'Chatt?ogram', 'Chattogram', branch, flags=re.I)

        return branch

    # ========================================================================
    # ACCOUNT INFO PARSING
    # ========================================================================

    def parse_account_info(self):
        """Extract account holder information from statement."""
        patterns = {
            'account_holder': r'MR\.\s*([A-Z][A-Z\s]+MANIK|[A-Z][A-Z\s]+[A-Z])',
            'account_number': r'Account\s*Number\s*:?\s*(\d{10,})',
            'branch_code': r'Branch\s*:?\s*(\d{4,})',
            'branch_name': r'Branch\s*:?\s*\d*\s*-?\s*([^\n]+?)(?=\.|\n|Currency)',
            'account_type': r'Account\s*Type\s*:?\s*([^\n]+)',
            'currency': r'Currency\s*:?\s*(\w+)',
            'period': r'Period\s*:?\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*[-to]+\s*(\d{2}-[A-Za-z]{3}-\d{4})',
            'opening_date': r'Opening\s*Date\s*:?\s*(\d{2}-[A-Za-z]{3}-\d{4})',
            'interest_rate': r'Interest\s*Rate\s*:?\s*(\d+\.?\d*)',
            'routing_number': r'Routing\s*Number\s*:?\s*(\d+)',
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, self.raw_text, re.IGNORECASE)
            if match:
                if match.lastindex == 1:
                    self.account_info[key] = match.group(1).strip()
                else:
                    groups = match.groups()
                    self.account_info[key] = [g.strip() for g in groups] if len(groups) > 1 else groups[0].strip()

    # ========================================================================
    # STATISTICS
    # ========================================================================

    def get_all_branches(self) -> List[str]:
        """Get list of all unique branches in statement."""
        branches = set()
        for txn in self.transactions:
            branches.add(txn.branch)
        return sorted(list(branches))

    def get_date_range(self) -> tuple:
        """Get min and max dates from transactions."""
        if not self.transactions:
            return None, None
        dates = []
        for txn in self.transactions:
            try:
                d = datetime.strptime(txn.date, '%d-%b-%Y')
                dates.append(d)
            except:
                pass
        if dates:
            return min(dates), max(dates)
        return None, None

    def get_statistics(self) -> dict:
        """Get statement statistics."""
        if not self.transactions:
            return {}

        cr_total = sum(t.amount for t in self.transactions if t.transaction_type == 'CR')
        dr_total = sum(t.amount for t in self.transactions if t.transaction_type == 'DR')

        return {
            'total_transactions': len(self.transactions),
            'credit_count': len([t for t in self.transactions if t.transaction_type == 'CR']),
            'debit_count': len([t for t in self.transactions if t.transaction_type == 'DR']),
            'total_credit': cr_total,
            'total_debit': dr_total,
            'net_amount': cr_total - dr_total,
            'branches': len(self.get_all_branches()),
            'date_range': self.get_date_range(),
            'avg_transaction': (cr_total + dr_total) / len(self.transactions) if self.transactions else 0,
            'max_credit': max((t.amount for t in self.transactions if t.transaction_type == 'CR'), default=0),
            'max_debit': max((t.amount for t in self.transactions if t.transaction_type == 'DR'), default=0),
        }


class StatementGenerator:
    """Generates PDF statement from filtered transactions."""

    def __init__(self, account_info: dict, output_path: str):
        self.account_info = account_info
        self.output_path = Path(output_path)

    def generate_pdf(self, transactions: List[Transaction], title: str = "Filtered Statement"):
        """Generate professional PDF statement."""
        doc = SimpleDocTemplate(
            str(self.output_path),
            pagesize=A4,
            rightMargin=15*mm,
            leftMargin=15*mm,
            topMargin=15*mm,
            bottomMargin=15*mm
        )

        elements = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('Title', fontSize=14, textColor=HexColor('#1a5490'),
                                     spaceAfter=6, alignment=TA_CENTER, fontName='Helvetica-Bold')
        header_style = ParagraphStyle('Header', fontSize=9, textColor=HexColor('#333333'),
                                      spaceAfter=2, alignment=TA_CENTER, fontName='Helvetica')
        normal_style = ParagraphStyle('Normal', fontSize=8, textColor=black, spaceAfter=2, fontName='Helvetica')

        # Bank Header
        elements.append(Paragraph("Sonali Bank PLC", title_style))
        elements.append(Paragraph("Statement Transaction Separator", header_style))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(f"Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}", header_style))
        elements.append(Spacer(1, 10))
        elements.append(HRFlowable(width="100%", thickness=1, color=HexColor('#1a5490')))
        elements.append(Spacer(1, 8))

        # Statement Title
        elements.append(Paragraph(title.upper(), ParagraphStyle(
            'StatementTitle', fontSize=16, textColor=HexColor('#1a5490'),
            spaceAfter=8, alignment=TA_CENTER, fontName='Helvetica-Bold'
        )))
        elements.append(Spacer(1, 6))

        # Account Info
        info_data = [
            [Paragraph("<b>Account Holder:</b>", normal_style),
             Paragraph(self.account_info.get('account_holder', 'N/A'), ParagraphStyle('Bold', fontSize=8, fontName='Helvetica-Bold'))],
            [Paragraph("<b>Account Number:</b>", normal_style),
             Paragraph(self.account_info.get('account_number', 'N/A'), normal_style)],
            [Paragraph("<b>Branch:</b>", normal_style),
             Paragraph(self.account_info.get('branch_name', 'N/A'), normal_style)],
            [Paragraph("<b>Account Type:</b>", normal_style),
             Paragraph(self.account_info.get('account_type', 'N/A'), normal_style)],
            [Paragraph("<b>Filtered Transactions:</b>", normal_style),
             Paragraph(str(len(transactions)), ParagraphStyle('BoldRed', fontSize=8, fontName='Helvetica-Bold', textColor=HexColor('#c41e3a')))],
        ]

        info_table = Table(info_data, colWidths=[40*mm, 135*mm])
        info_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#f8f9fa')),
            ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#dee2e6')),
        ]))
        elements.append(info_table)
        elements.append(Spacer(1, 12))

        if not transactions:
            elements.append(Paragraph("No transactions found matching the criteria.",
                                      ParagraphStyle('NoData', fontSize=10, textColor=HexColor('#6c757d'),
                                                     alignment=TA_CENTER, fontName='Helvetica-Oblique')))
        else:
            txn_data = [[
                Paragraph("<b>Date</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_CENTER)),
                Paragraph("<b>Value Date</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_CENTER)),
                Paragraph("<b>Description</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_CENTER)),
                Paragraph("<b>Type</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_CENTER)),
                Paragraph("<b>Amount</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_RIGHT)),
                Paragraph("<b>Balance</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_RIGHT)),
                Paragraph("<b>Branch</b>", ParagraphStyle('TH', fontSize=8, textColor=white, fontName='Helvetica-Bold', alignment=TA_CENTER)),
            ]]

            total_cr = total_dr = 0

            for txn in transactions:
                txn_data.append([
                    Paragraph(txn.date, ParagraphStyle('Cell', fontSize=7, fontName='Helvetica', alignment=TA_CENTER)),
                    Paragraph(txn.value_date, ParagraphStyle('Cell', fontSize=7, fontName='Helvetica', alignment=TA_CENTER)),
                    Paragraph(txn.description[:80], ParagraphStyle('Cell', fontSize=7, fontName='Helvetica', alignment=TA_LEFT)),
                    Paragraph(txn.transaction_type, ParagraphStyle('Cell', fontSize=7, fontName='Helvetica-Bold',
                                                                  alignment=TA_CENTER, textColor=HexColor('#28a745') if txn.transaction_type == 'CR' else HexColor('#dc3545'))),
                    Paragraph(f"{txn.amount:,.2f}", ParagraphStyle('Cell', fontSize=7, fontName='Helvetica', alignment=TA_RIGHT)),
                    Paragraph(f"{txn.balance:,.2f}", ParagraphStyle('Cell', fontSize=7, fontName='Helvetica', alignment=TA_RIGHT)),
                    Paragraph(txn.branch[:30], ParagraphStyle('Cell', fontSize=7, fontName='Helvetica', alignment=TA_CENTER)),
                ])
                if txn.transaction_type == 'CR':
                    total_cr += txn.amount
                else:
                    total_dr += txn.amount

            txn_data.append([
                Paragraph("", normal_style), Paragraph("", normal_style),
                Paragraph("<b>TOTALS</b>", ParagraphStyle('Total', fontSize=8, fontName='Helvetica-Bold', alignment=TA_RIGHT, textColor=HexColor('#1a5490'))),
                Paragraph("", normal_style),
                Paragraph(f"<b>{total_cr:,.2f}</b>", ParagraphStyle('Total', fontSize=8, fontName='Helvetica-Bold', alignment=TA_RIGHT, textColor=HexColor('#28a745'))),
                Paragraph("", normal_style), Paragraph("", normal_style),
            ])

            txn_table = Table(txn_data, colWidths=[18*mm, 18*mm, 55*mm, 12*mm, 25*mm, 25*mm, 32*mm])
            txn_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#1a5490')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8),
                ('TOPPADDING', (0, 0), (-1, 0), 5),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
                ('BACKGROUND', (0, 1), (-1, -2), HexColor('#ffffff')),
                ('TEXTCOLOR', (0, 1), (-1, -2), black),
                ('ALIGN', (0, 1), (1, -2), 'CENTER'),
                ('ALIGN', (2, 1), (2, -2), 'LEFT'),
                ('ALIGN', (3, 1), (3, -2), 'CENTER'),
                ('ALIGN', (4, 1), (5, -2), 'RIGHT'),
                ('ALIGN', (6, 1), (6, -2), 'CENTER'),
                ('FONTNAME', (0, 1), (-1, -2), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -2), 7),
                ('TOPPADDING', (0, 1), (-1, -2), 3),
                ('BOTTOMPADDING', (0, 1), (-1, -2), 3),
                ('LEFTPADDING', (0, 1), (-1, -2), 3),
                ('RIGHTPADDING', (0, 1), (-1, -2), 3),
                ('GRID', (0, 0), (-1, -2), 0.5, HexColor('#dee2e6')),
                ('LINEBELOW', (0, 0), (-1, 0), 1.5, HexColor('#1a5490')),
                ('BACKGROUND', (0, -1), (-1, -1), HexColor('#e9ecef')),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, HexColor('#1a5490')),
                ('LINEBELOW', (0, -1), (-1, -1), 1.5, HexColor('#1a5490')),
            ] + [('BACKGROUND', (0, i), (-1, i), HexColor('#f8f9fa')) for i in range(2, len(txn_data)-1, 2)]))
            elements.append(txn_table)

            elements.append(Spacer(1, 10))
            summary_data = [[Paragraph(
                f"<b>Summary:</b> Credit: BDT {total_cr:,.2f} | Debit: BDT {total_dr:,.2f} | Net: BDT {total_cr - total_dr:,.2f}",
                ParagraphStyle('Summary', fontSize=9, fontName='Helvetica', alignment=TA_CENTER, textColor=HexColor('#1a5490')))]
            ]
            summary_table = Table(summary_data, colWidths=[175*mm])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), HexColor('#f8f9fa')),
                ('BOX', (0, 0), (-1, -1), 1, HexColor('#1a5490')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
            elements.append(summary_table)

        elements.append(Spacer(1, 15))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=HexColor('#adb5bd')))
        elements.append(Spacer(1, 6))
        elements.append(Paragraph("This is a computer generated statement.",
                                  ParagraphStyle('Footer', fontSize=7, textColor=HexColor('#6c757d'), alignment=TA_CENTER, fontName='Helvetica-Oblique')))
        elements.append(Paragraph("******* End of Report *******", ParagraphStyle('End', fontSize=8, textColor=HexColor('#1a5490'), alignment=TA_CENTER, fontName='Helvetica-Bold')))

        doc.build(elements)
        return str(self.output_path)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


# ============== ROUTES ==============

@app.route('/')
def index():
    """Home page with upload form."""
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    """Handle PDF upload and initial parsing."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': 'Only PDF files are allowed'}), 400

    # Generate unique filename
    unique_id = str(uuid.uuid4())[:8]
    filename = secure_filename(f"{unique_id}_{file.filename}")
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    # Parse PDF with modern hybrid parser
    parser = PDFStatementParser(filepath)
    parser.extract_text()
    parser.parse_account_info()
    parser.parse_transactions()

    if not parser.transactions:
        # Provide detailed error info for debugging
        error_detail = "No transactions found in PDF."
        if parser.parse_errors:
            error_detail += f" Parse errors: {'; '.join(parser.parse_errors[:3])}"

        os.remove(filepath)
        return jsonify({
            'error': error_detail,
            'debug_info': {
                'text_length': len(parser.raw_text),
                'tables_found': len(parser.df_pages),
                'sample_text': parser.raw_text[:500] if parser.raw_text else "No text extracted"
            }
        }), 400

    # Store in session
    session['uploaded_file'] = filepath
    session['account_info'] = parser.account_info
    session['total_transactions'] = len(parser.transactions)

    stats = parser.get_statistics()
    branches = parser.get_all_branches()
    date_range = parser.get_date_range()

    return jsonify({
        'success': True,
        'file_id': unique_id,
        'account_info': parser.account_info,
        'statistics': {
            'total_transactions': stats['total_transactions'],
            'credit_count': stats['credit_count'],
            'debit_count': stats['debit_count'],
            'total_credit': stats['total_credit'],
            'total_debit': stats['total_debit'],
            'net_amount': stats['net_amount'],
            'branches': stats['branches'],
            'date_range': [d.strftime('%d-%b-%Y') if d else None for d in stats['date_range']],
            'avg_transaction': stats['avg_transaction'],
        },
        'branches': branches,
        'sample_transactions': [t.to_dict() for t in parser.transactions[:5]]
    })


@app.route('/filter', methods=['POST'])
def filter_transactions():
    """Apply filters and return results."""
    if 'uploaded_file' not in session:
        return jsonify({'error': 'No file uploaded. Please upload a PDF first.'}), 400

    filepath = session['uploaded_file']
    if not os.path.exists(filepath):
        return jsonify({'error': 'Uploaded file expired. Please upload again.'}), 400

    # Re-parse
    parser = PDFStatementParser(filepath)
    parser.extract_text()
    parser.parse_account_info()
    parser.parse_transactions()

    # Get filter parameters
    data = request.get_json() or request.form

    filtered = parser.transactions[:]

    # Branch filter
    branch = data.get('branch', '').strip()
    if branch:
        filtered = [t for t in filtered if branch.lower() in t.branch.lower()]

    # Date range filter
    start_date = data.get('start_date', '').strip()
    end_date = data.get('end_date', '').strip()
    if start_date and end_date:
        try:
            start = datetime.strptime(start_date, '%Y-%m-%d')
            end = datetime.strptime(end_date, '%Y-%m-%d')
            filtered = [t for t in filtered if start <= datetime.strptime(t.date, '%d-%b-%Y') <= end]
        except ValueError:
            pass

    # Amount filter
    min_amount = data.get('min_amount', '').strip()
    max_amount = data.get('max_amount', '').strip()
    if min_amount:
        try:
            filtered = [t for t in filtered if t.amount >= float(min_amount)]
        except ValueError:
            pass
    if max_amount:
        try:
            filtered = [t for t in filtered if t.amount <= float(max_amount)]
        except ValueError:
            pass

    # Transaction type filter
    txn_type = data.get('transaction_type', '').strip().upper()
    if txn_type in ['CR', 'DR']:
        filtered = [t for t in filtered if t.transaction_type == txn_type]

    # Keyword filter
    keyword = data.get('keyword', '').strip()
    if keyword:
        filtered = [t for t in filtered if keyword.lower() in t.description.lower()]

    # Store filtered results
    session['filtered_transactions'] = [t.to_dict() for t in filtered]

    # Calculate stats
    cr_total = sum(t.amount for t in filtered if t.transaction_type == 'CR')
    dr_total = sum(t.amount for t in filtered if t.transaction_type == 'DR')

    return jsonify({
        'success': True,
        'filtered_count': len(filtered),
        'total_credit': cr_total,
        'total_debit': dr_total,
        'net_amount': cr_total - dr_total,
        'transactions': [t.to_dict() for t in filtered]
    })


@app.route('/download/<format>')
def download_file(format):
    """Download filtered transactions in specified format."""
    if 'filtered_transactions' not in session:
        return jsonify({'error': 'No filtered data available'}), 400

    transactions_data = session['filtered_transactions']
    transactions = [Transaction(**t) for t in transactions_data]
    account_info = session.get('account_info', {})

    unique_id = str(uuid.uuid4())[:8]

    if format == 'pdf':
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"filtered_{unique_id}.pdf")
        generator = StatementGenerator(account_info, output_path)
        generator.generate_pdf(transactions, title="Filtered Statement")
        return send_file(output_path, as_attachment=True, download_name="filtered_statement.pdf")

    elif format == 'csv':
        import csv
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"filtered_{unique_id}.csv")
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Date', 'Value Date', 'Description', 'Type', 'Amount', 'Balance', 'Branch'])
            for t in transactions:
                writer.writerow([t.date, t.value_date, t.description, t.transaction_type,
                                t.amount, t.balance, t.branch])
        return send_file(output_path, as_attachment=True, download_name="filtered_transactions.csv")

    elif format == 'json':
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"filtered_{unique_id}.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump({
                'account_info': account_info,
                'generated_at': datetime.now().isoformat(),
                'total_transactions': len(transactions),
                'transactions': [t.to_dict() for t in transactions]
            }, f, indent=2, ensure_ascii=False)
        return send_file(output_path, as_attachment=True, download_name="filtered_transactions.json")

    else:
        return jsonify({'error': 'Invalid format'}), 400


@app.route('/preview')
def preview_transactions():
    """Return paginated preview of filtered transactions."""
    if 'filtered_transactions' not in session:
        return jsonify({'error': 'No data available'}), 400

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    transactions_data = session['filtered_transactions']
    total = len(transactions_data)
    start = (page - 1) * per_page
    end = start + per_page

    return jsonify({
        'transactions': transactions_data[start:end],
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page
    })


@app.route('/clear')
def clear_session():
    """Clear session and uploaded files."""
    if 'uploaded_file' in session:
        try:
            os.remove(session['uploaded_file'])
        except:
            pass

    session.clear()
    return jsonify({'success': True, 'message': 'Session cleared'})


@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large. Maximum size is 16MB.'}), 413


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
