#!/usr/bin/env python3
"""
PDF Bank Statement Transaction Separator - Web App
==================================================
A Flask web application for extracting and filtering bank statement transactions.
Deployable on Koyeb, Railway, Render, Heroku, etc.

Environment Variables:
    PORT: Server port (default: 5000)
    SECRET_KEY: Flask secret key
    MAX_CONTENT_LENGTH: Max upload size in bytes (default: 16MB)
"""

import os
import re
import json
import uuid
import shutil
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional
from functools import wraps

from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for, session
from werkzeug.utils import secure_filename

# PDF and Report generation
try:
    import PyPDF2
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
    os.system("pip install PyPDF2 reportlab -q")
    import PyPDF2
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor, black, white, Color
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY

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
    """Parses PDF bank statements and extracts transactions."""

    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self.raw_text = ""
        self.transactions: List[Transaction] = []
        self.account_info = {}
        self.parse_errors = []

    def extract_text(self) -> str:
        """Extract text from PDF file."""
        text = ""
        try:
            with open(self.pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                for page_num, page in enumerate(pdf_reader.pages, 1):
                    page_text = page.extract_text()
                    if page_text:
                        text += f"\n--- PAGE {page_num} ---\n" + page_text + "\n"
        except Exception as e:
            self.parse_errors.append(f"Error reading PDF: {str(e)}")
        self.raw_text = text
        return text

    def parse_account_info(self):
        """Extract account holder information from statement."""
        patterns = {
            'account_holder': r'(?:MR\.|MS\.|MRS\.)?\s*([A-Z][A-Z\s]+MANIK|[A-Z][A-Z\s]+[A-Z])',
            'account_number': r'Account Number\s*:?\s*(\d{10,})',
            'branch_code': r'Branch\s*:?\s*(\d+)',
            'branch_name': r'Branch\s*:?\s*\d*\s*-?\s*([^\n]+?)(?=\.|\n|Currency)',
            'account_type': r'Account Type\s*:?\s*([^\n]+)',
            'currency': r'Currency\s*:?\s*(\w+)',
            'period': r'Period\s*:?\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*[-to]+\s*(\d{2}-[A-Za-z]{3}-\d{4})',
            'opening_date': r'Opening Date\s*:?\s*(\d{2}-[A-Za-z]{3}-\d{4})',
            'interest_rate': r'Interest Rate\s*:?\s*(\d+\.?\d*)',
            'routing_number': r'Routing Number\s*:?\s*(\d+)',
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, self.raw_text, re.IGNORECASE)
            if match:
                self.account_info[key] = match.group(1).strip() if match.lastindex == 1 else [g.strip() for g in match.groups()]

    def parse_transactions(self) -> List[Transaction]:
        """Parse transactions from extracted text with improved accuracy."""
        date_pattern = r'\d{2}-[A-Za-z]{3}-\d{4}'
        pages = re.split(r'--- PAGE \d+ ---', self.raw_text)

        for page_content in pages:
            if not page_content.strip():
                continue

            lines = page_content.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()

                # Look for date pattern at start
                date_match = re.match(rf'({date_pattern})\s+({date_pattern})', line)
                if date_match:
                    txn_date = date_match.group(1)
                    value_date = date_match.group(2)

                    # Multi-line description handling
                    description_lines = [line[date_match.end():].strip()]
                    j = i + 1
                    while j < len(lines) and not re.match(rf'{date_pattern}', lines[j].strip()):
                        next_line = lines[j].strip()
                        if next_line and not re.match(r'^(CR|DR|Balance|Total|Page|User ID|Generated By)', next_line):
                            # Check if line contains amount/balance indicators
                            if re.search(r'\d{1,3}(?:,\d{3})*\.\d{2}', next_line):
                                description_lines.append(next_line)
                            else:
                                description_lines.append(next_line)
                        j += 1
                        # Stop if we hit next transaction or page footer
                        if j < len(lines) and re.match(rf'{date_pattern}', lines[j].strip()):
                            break

                    full_text = ' '.join(description_lines)

                    # Extract amounts
                    amounts = re.findall(r'\d{1,3}(?:,\d{3})*\.\d{2}', full_text)

                    # Determine transaction type
                    txn_type = 'CR' if 'CR' in full_text else ('DR' if 'DR' in full_text or 'Debit' in full_text else 'CR')

                    # Extract branch
                    branch_patterns = [
                        r'([A-Za-z\s]+Branch[^,\n]*Chattogram)',
                        r'([A-Za-z\s]+Branch[^,\n]*Khagrachari)',
                        r'([A-Za-z\s]+Branch[^,\n]*Coxs Bazar)',
                        r'([A-Za-z\s]+Branch[^,\n]*Dhaka)',
                        r'([A-Za-z\s]+Branch[^,\n]*)',
                        r'(HEAD OFFICE)',
                        r'(Local Office[^,\n]*)',
                        r'(G\.M\. OFFICE[^,\n]*)',
                        r'(P\.O\. [^,\n]*)',
                    ]

                    branch = "Unknown"
                    for bp in branch_patterns:
                        bm = re.search(bp, full_text, re.IGNORECASE)
                        if bm:
                            branch = bm.group(1).strip()
                            break

                    # Clean description
                    description = full_text
                    # Remove amount strings from description
                    for amt in amounts:
                        description = description.replace(amt, '')
                    description = re.sub(r'\b(CR|DR)\b', '', description)
                    description = re.sub(r'\s+', ' ', description).strip()
                    description = description[:150]

                    # Parse amounts
                    if len(amounts) >= 2:
                        try:
                            amount = float(amounts[-2].replace(',', ''))
                            balance = float(amounts[-1].replace(',', ''))
                        except ValueError:
                            i += 1
                            continue
                    elif len(amounts) == 1:
                        try:
                            amount = float(amounts[0].replace(',', ''))
                            balance = 0.0
                        except ValueError:
                            i += 1
                            continue
                    else:
                        i += 1
                        continue

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

        return self.transactions

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

    # Parse PDF
    parser = PDFStatementParser(filepath)
    parser.extract_text()
    parser.parse_account_info()
    parser.parse_transactions()

    if not parser.transactions:
        os.remove(filepath)
        return jsonify({'error': 'No transactions found in PDF. Ensure it is a text-based PDF.'}), 400

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

    # Re-parse (or load from cache in production)
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
    # Clean up files
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
