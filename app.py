# Write the file directly with proper escaping
# I'll construct it carefully to avoid double-escaping issues

with open('/mnt/agents/output/app.py', 'w', encoding='utf-8') as f:
    f.write(r'''#!/usr/bin/env python3
"""
PDF Bank Statement Transaction Separator - Web App
Ultra-robust version: app defined first, everything else wrapped.
"""

import os
import sys

# ============================================================
# CRITICAL: Define app IMMEDIATELY before any imports that could fail
# ============================================================
from flask import Flask, render_template, request, jsonify, send_file, session
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}

for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
    os.makedirs(folder, exist_ok=True)

# CORS headers
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ============================================================
# Now import optional libraries with maximum safety
# ============================================================

import re
import json
import uuid
import csv
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any

# PDF extraction - try multiple options
PDF_EXTRACTOR = None

try:
    import pdfplumber
    PDF_EXTRACTOR = 'pdfplumber'
except ImportError:
    pass

if not PDF_EXTRACTOR:
    try:
        import PyPDF2
        PDF_EXTRACTOR = 'pypdf2'
    except ImportError:
        pass

# ReportLab for PDF generation
REPORTLAB_OK = False
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor, black, white
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_OK = True
except ImportError as e:
    print(f"[WARNING] reportlab not available: {e}", file=sys.stderr)

# ============================================================
# Data models
# ============================================================

@dataclass
class Transaction:
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
    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self.raw_text = ""
        self.transactions: List[Transaction] = []
        self.account_info = {}
        self.parse_errors = []

    def extract_text(self) -> str:
        text = ""
        if PDF_EXTRACTOR == 'pdfplumber':
            try:
                text = self._extract_pdfplumber()
            except Exception as e:
                self.parse_errors.append(f"pdfplumber: {str(e)}")
        elif PDF_EXTRACTOR == 'pypdf2':
            try:
                text = self._extract_pypdf2()
            except Exception as e:
                self.parse_errors.append(f"pypdf2: {str(e)}")
        else:
            self.parse_errors.append("No PDF extraction library available. Install pdfplumber or PyPDF2.")
        self.raw_text = text
        return text

    def _extract_pdfplumber(self) -> str:
        import pdfplumber
        text = ""
        with pdfplumber.open(self.pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                page_text = page.extract_text()
                if page_text:
                    text += f"\n--- PAGE {page_num} ---\n" + page_text + "\n"
        return text

    def _extract_pypdf2(self) -> str:
        import PyPDF2
        text = ""
        with open(self.pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            for page_num, page in enumerate(pdf_reader.pages, 1):
                page_text = page.extract_text()
                if page_text:
                    text += f"\n--- PAGE {page_num} ---\n" + page_text + "\n"
        return text

    def parse_transactions(self) -> List[Transaction]:
        self.transactions = []
        if not self.raw_text:
            self.parse_errors.append("No text extracted from PDF")
            return self.transactions
        self._parse_advanced()
        if not self.transactions:
            self._parse_simple()
        self._post_process()
        return self.transactions

    def _parse_advanced(self):
        date_pat = r'\d{2}-[A-Za-z]{3}-\d{4}'
        pages = re.split(r'--- PAGE (\d+) ---', self.raw_text)
        for idx in range(1, len(pages), 2):
            if idx >= len(pages):
                break
            page_content = pages[idx + 1] if idx + 1 < len(pages) else ""
            if not page_content.strip():
                continue
            lines = page_content.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if self._is_noise(line):
                    i += 1
                    continue
                m = re.match(rf'^({date_pat})\s+({date_pat})(.*)', line)
                if not m:
                    i += 1
                    continue
                txn_date = m.group(1)
                value_date = m.group(2)
                remainder = m.group(3).strip()
                desc_lines = [remainder] if remainder else []
                amounts = []
                branch = "Unknown"
                txn_type = 'CR'
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if self._is_boundary(next_line, date_pat):
                        break
                    amts = re.findall(r'\d{1,3}(?:,\d{3})*\.\d{2}', next_line)
                    amounts.extend(amts)
                    if re.search(r'\bCR\b', next_line):
                        txn_type = 'CR'
                    elif re.search(r'\bDR\b', next_line):
                        txn_type = 'DR'
                    b = self._extract_branch(next_line)
                    if b:
                        branch = b
                    if not self._is_amount_only(next_line):
                        desc_lines.append(next_line)
                    j += 1
                full_text = ' '.join(desc_lines)
                amount, balance = self._parse_amounts(amounts)
                if amount is None:
                    i = j if j > i else i + 1
                    continue
                description = self._clean_desc(full_text, amount, balance)
                if branch == "Unknown" and desc_lines:
                    b = self._extract_branch(desc_lines[0])
                    if b:
                        branch = b
                self.transactions.append(Transaction(
                    date=txn_date, value_date=value_date, description=description,
                    amount=amount, transaction_type=txn_type, balance=balance,
                    branch=branch, raw_text=full_text[:200]
                ))
                i = j
                continue

    def _parse_simple(self):
        date_pat = r'\d{2}-[A-Za-z]{3}-\d{4}'
        pattern = re.compile(
            rf'({date_pat})\s+({date_pat})\s+(.*?)\s+(\d{{1,3}}(?:,\d{{3}})*\.\d{{2}})\s+(\d{{1,3}}(?:,\d{{3}})*\.\d{{2}})',
            re.DOTALL
        )
        for m in pattern.findall(self.raw_text):
            txn_date, value_date, desc, amt_str, bal_str = m
            amount = float(amt_str.replace(',', ''))
            balance = float(bal_str.replace(',', ''))
            txn_type = 'CR' if 'CR' in desc else 'DR'
            description = self._clean_desc(desc, amount, balance)
            self.transactions.append(Transaction(
                date=txn_date, value_date=value_date, description=description,
                amount=amount, transaction_type=txn_type, balance=balance,
                branch="Unknown", raw_text=desc[:200]
            ))

    def _is_noise(self, line: str) -> bool:
        patterns = [
            r'^User ID\.', r'^Print Date', r'^Print Time', r'^Generated By',
            r'^STATEMENT OF ACCOUNT', r'^SonaliBank', r'^MR\.\s+[A-Z]',
            r'^JAMALKHAN', r'^ANDERKILLA', r'^GPO\s+\d+', r'^CTG$',
            r'^Branch\s*:', r'^Currency\s*:', r'^Opening Date\s*:',
            r'^Account Number\s*:', r'^Interest Rate\s*:', r'^Account Type\s*:',
            r'^Period\s*:', r'^Status\s*:', r'^Routing Number\s*:',
            r'^Page \d+ of \d+', r'^\*+End of Report\*+', r'^Total$',
            r'^Balance [BC]/F', r'^Grand Total', r'^Date\s+Value',
            r'^\|?Date\|', r'^\|?Value Date\|',
        ]
        for p in patterns:
            if re.match(p, line, re.I):
                return True
        return False

    def _is_boundary(self, line: str, date_pat: str) -> bool:
        if re.match(rf'^{date_pat}', line):
            return True
        patterns = [
            r'^Total\s*$', r'^Page \d+ of \d+', r'^Balance [BC]/F',
            r'^Grand Total', r'^User ID\.', r'^Generated By',
            r'^STATEMENT OF ACCOUNT', r'^\*+End of Report\*+', r'^--- PAGE',
        ]
        for p in patterns:
            if re.match(p, line, re.I):
                return True
        return False

    def _is_amount_only(self, line: str) -> bool:
        s = line.strip()
        if re.match(r'^\d{1,3}(?:,\d{3})*\.\d{2}$', s):
            return True
        if s in ['CR', 'DR', 'Balance', 'Total', 'Credit', 'Debit']:
            return True
        return False

    def _extract_branch(self, text: str) -> Optional[str]:
        patterns = [
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Chattogram)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Khagrachari)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Coxs Bazar)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*Dhaka)',
            r'([A-Za-z\s]+Branch[A-Za-z\s,]*)',
            r'(HEAD OFFICE)', r'(Local Office[^,\n]*)',
            r'(G\.M\. OFFICE[^,\n]*)', r'(P\.O\. [^,\n]*)',
        ]
        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                return m.group(1).strip()
        return None

    def _parse_amounts(self, amounts: List[str]) -> tuple:
        if len(amounts) >= 2:
            try:
                return float(amounts[-2].replace(',', '')), float(amounts[-1].replace(',', ''))
            except ValueError:
                return None, None
        elif len(amounts) == 1:
            try:
                return float(amounts[0].replace(',', '')), 0.0
            except ValueError:
                return None, None
        return None, None

    def _clean_desc(self, text: str, amount: float, balance: float) -> str:
        if not text:
            return ""
        text = re.sub(r'\d{1,3}(?:,\d{3})*\.\d{2}', '', text)
        text = re.sub(r'\b(CR|DR|cr|dr)\b', '', text)
        noise = [
            'Balance B/F', 'Balance C/F', 'Opening Balance', 'Closing Balance',
            'Total', 'Page', 'User ID', 'Generated By', 'Print Date', 'Print Time',
            'STATEMENT OF ACCOUNT', 'SonaliBank', 'Jamalkhan Road Branch',
            'Chattogram', 'HEAD OFFICE', 'Local Office', 'G.M. OFFICE'
        ]
        for n in noise:
            text = text.replace(n, '')
        text = re.sub(r'\s+', ' ', text).strip()
        text = text.strip('.,;:- ')
        return text[:150]

    def _post_process(self):
        for txn in self.transactions:
            txn.date = self._norm_date(txn.date)
            txn.value_date = self._norm_date(txn.value_date)
            txn.branch = self._norm_branch(txn.branch)
            txn.transaction_type = txn.transaction_type.upper()
            txn.amount = round(txn.amount, 2)
            txn.balance = round(txn.balance, 2)

    def _norm_date(self, d: str) -> str:
        if not d:
            return ""
        m = re.match(r'(\d{2})-([A-Za-z]{3})-(\d{4})', d)
        if m:
            return f"{m.group(1)}-{m.group(2).title()}-{m.group(3)}"
        return d

    def _norm_branch(self, b: str) -> str:
        if not b or b == "Unknown":
            return "Unknown"
        b = b.strip().rstrip('.,;')
        b = re.sub(r'Chatt?ogram', 'Chattogram', b, flags=re.I)
        return b

    def parse_account_info(self):
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
            m = re.search(pattern, self.raw_text, re.I)
            if m:
                if m.lastindex == 1:
                    self.account_info[key] = m.group(1).strip()
                else:
                    groups = m.groups()
                    self.account_info[key] = [g.strip() for g in groups] if len(groups) > 1 else groups[0].strip()

    def get_all_branches(self) -> List[str]:
        branches = set()
        for txn in self.transactions:
            branches.add(txn.branch)
        return sorted(list(branches))

    def get_date_range(self) -> tuple:
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
    def __init__(self, account_info: dict, output_path: str):
        self.account_info = account_info
        self.output_path = Path(output_path)

    def generate_pdf(self, transactions: List[Transaction], title: str = "Filtered Statement"):
        if not REPORTLAB_OK:
            raise ImportError("reportlab not available")
        doc = SimpleDocTemplate(
            str(self.output_path), pagesize=A4,
            rightMargin=15*mm, leftMargin=15*mm,
            topMargin=15*mm, bottomMargin=15*mm
        )
        elements = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('Title', fontSize=14, textColor=HexColor('#1a5490'),
                                     spaceAfter=6, alignment=TA_CENTER, fontName='Helvetica-Bold')
        header_style = ParagraphStyle('Header', fontSize=9, textColor=HexColor('#333333'),
                                      spaceAfter=2, alignment=TA_CENTER, fontName='Helvetica')
        normal_style = ParagraphStyle('Normal', fontSize=8, textColor=black, spaceAfter=2, fontName='Helvetica')
        elements.append(Paragraph("Sonali Bank PLC", title_style))
        elements.append(Paragraph("Statement Transaction Separator", header_style))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(f"Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}", header_style))
        elements.append(Spacer(1, 10))
        elements.append(HRFlowable(width="100%", thickness=1, color=HexColor('#1a5490')))
        elements.append(Spacer(1, 8))
        elements.append(Paragraph(title.upper(), ParagraphStyle(
            'StatementTitle', fontSize=16, textColor=HexColor('#1a5490'),
            spaceAfter=8, alignment=TA_CENTER, fontName='Helvetica-Bold'
        )))
        elements.append(Spacer(1, 6))
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


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Only PDF files are allowed'}), 400
    unique_id = str(uuid.uuid4())[:8]
    filename = secure_filename(f"{unique_id}_{file.filename}")
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    try:
        parser = PDFStatementParser(filepath)
        parser.extract_text()
        parser.parse_account_info()
        parser.parse_transactions()
    except Exception as e:
        import traceback
        traceback.print_exc()
        os.remove(filepath)
        return jsonify({
            'error': f'PDF parsing failed: {str(e)}',
            'debug_info': {'exception_type': type(e).__name__, 'exception_message': str(e)}
        }), 400
    if not parser.transactions:
        error_detail = "No transactions found in PDF."
        if parser.parse_errors:
            error_detail += f" Errors: {'; '.join(parser.parse_errors[:3])}"
        os.remove(filepath)
        return jsonify({
            'error': error_detail,
            'debug_info': {
                'text_length': len(parser.raw_text),
                'sample_text': parser.raw_text[:500] if parser.raw_text else "No text extracted"
            }
        }), 400
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


@app.route('/filter', methods=['POST', 'OPTIONS'])
def filter_transactions():
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    if 'uploaded_file' not in session:
        return jsonify({'error': 'No file uploaded. Please upload a PDF first.'}), 400
    filepath = session['uploaded_file']
    if not os.path.exists(filepath):
        return jsonify({'error': 'Uploaded file expired. Please upload again.'}), 400
    try:
        parser = PDFStatementParser(filepath)
        parser.extract_text()
        parser.parse_account_info()
        parser.parse_transactions()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to re-parse PDF: {str(e)}'}), 500
    data = {}
    content_type = request.content_type or ''
    try:
        if 'application/json' in content_type:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form.to_dict()
    except Exception as e:
        return jsonify({'error': f'Failed to parse request data: {str(e)}'}), 400
    filtered = parser.transactions[:]
    branch = data.get('branch', '').strip()
    if branch:
        filtered = [t for t in filtered if branch.lower() in t.branch.lower()]
    start_date = data.get('start_date', '').strip()
    end_date = data.get('end_date', '').strip()
    if start_date and end_date:
        try:
            start = datetime.strptime(start_date, '%Y-%m-%d')
            end = datetime.strptime(end_date, '%Y-%m-%d')
            filtered = [t for t in filtered if start <= datetime.strptime(t.date, '%d-%b-%Y') <= end]
        except ValueError:
            pass
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
    txn_type = data.get('transaction_type', '').strip().upper()
    if txn_type in ['CR', 'DR']:
        filtered = [t for t in filtered if t.transaction_type == txn_type]
    keyword = data.get('keyword', '').strip()
    if keyword:
        filtered = [t for t in filtered if keyword.lower() in t.description.lower()]
    session['filtered_transactions'] = [t.to_dict() for t in filtered]
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
    if 'filtered_transactions' not in session:
        return jsonify({'error': 'No filtered data available'}), 400
    transactions_data = session['filtered_transactions']
    transactions = [Transaction(**t) for t in transactions_data]
    account_info = session.get('account_info', {})
    unique_id = str(uuid.uuid4())[:8]
    if format == 'pdf':
        if not REPORTLAB_OK:
            return jsonify({'error': 'PDF generation not available. Install reportlab.'}), 500
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"filtered_{unique_id}.pdf")
        generator = StatementGenerator(account_info, output_path)
        generator.generate_pdf(transactions, title="Filtered Statement")
        return send_file(output_path, as_attachment=True, download_name="filtered_statement.pdf")
    elif format == 'csv':
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
''')

print("Fixed app.py written successfully!")

# Verify by reading back and checking for syntax errors
import ast
try:
    with open('/mnt/agents/output/app.py', 'r') as f:
        code = f.read()
    ast.parse(code)
    print("Syntax check: PASSED")
    print(f"Total lines: {len(code.splitlines())}")
except SyntaxError as e:
    print(f"Syntax error: {e}")
