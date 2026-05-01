# 🏦 Bank Statement Separator - Web App

A production-ready Flask web application for extracting and filtering transactions from PDF bank statements. Deployable on Koyeb, Railway, Render, Heroku, AWS, GCP, Azure, or any Docker-compatible platform.

## 🚀 Quick Deploy to Koyeb

[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=docker&name=bank-statement-separator&ports=8000;http;/)

### Manual Deploy Steps:

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/bank-statement-separator.git
   git push -u origin main
   ```

2. **Deploy on Koyeb**
   - Go to [koyeb.com](https://app.koyeb.com)
   - Click "Create App"
   - Select "GitHub" and choose your repository
   - Builder: Select "Dockerfile"
   - Port: `8000`
   - Click "Deploy"
   - Done! 🎉

## 📁 Project Structure

```
statement_separator_web/
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── Procfile               # Heroku/Koyeb process definition
├── Dockerfile             # Container configuration
├── docker-compose.yml     # Local development
├── koyeb.yaml            # Koyeb deployment config
├── templates/
│   └── index.html        # Web interface
├── uploads/              # Uploaded PDFs (temporary)
└── outputs/              # Generated files (temporary)
```

## 🛠️ Local Development

### Option 1: Python Direct
```bash
# Clone repository
git clone https://github.com/YOUR_USERNAME/bank-statement-separator.git
cd statement_separator_web

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run application
python app.py
# Open http://localhost:5000
```

### Option 2: Docker
```bash
# Build and run
docker-compose up --build

# Open http://localhost:5000
```

### Option 3: Docker Direct
```bash
# Build image
docker build -t bank-statement-separator .

# Run container
docker run -p 5000:8000 -e PORT=8000 bank-statement-separator

# Open http://localhost:5000
```

## 🌐 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Server port |
| `SECRET_KEY` | `dev-secret-key` | Flask secret key |
| `MAX_CONTENT_LENGTH` | `16777216` | Max upload size (16MB) |
| `FLASK_DEBUG` | `False` | Debug mode |

## ✨ Features

- 📤 **Drag & Drop Upload** - Modern file upload with progress
- 📊 **Real-time Statistics** - Instant overview of your statement
- 🔍 **Smart Filtering**:
  - By Branch (clickable tags)
  - By Date Range
  - By Amount Range
  - By Transaction Type (CR/DR)
  - By Keyword in Description
- 📄 **PDF Export** - Professional bank-style formatted statements
- 📊 **CSV Export** - Excel-ready data
- 💾 **JSON Export** - Structured data for developers
- 📱 **Responsive Design** - Works on mobile, tablet, desktop
- 🎨 **Modern UI** - Clean, professional interface

## 🏦 Supported Banks

- Sonali Bank PLC
- Janata Bank
- Agrani Bank
- Rupali Bank
- And most PDF statements with tabular transaction data

## 🔒 Security

- File type validation (PDF only)
- File size limits (16MB default)
- Secure filename handling
- Session-based file management
- Automatic cleanup of temporary files

## 📄 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface |
| `/upload` | POST | Upload PDF file |
| `/filter` | POST | Apply filters |
| `/download/<format>` | GET | Download results (pdf/csv/json) |
| `/preview` | GET | Paginated preview |
| `/clear` | GET | Clear session |

## 📝 Example API Usage

```bash
# Upload PDF
curl -X POST -F "file=@statement.pdf" https://your-app.koyeb.app/upload

# Filter transactions
curl -X POST -H "Content-Type: application/json" \
  -d '{"branch":"Jamalkhan","transaction_type":"CR"}' \
  https://your-app.koyeb.app/filter

# Download PDF
curl https://your-app.koyeb.app/download/pdf -o filtered.pdf
```

## 🚀 Deployment Platforms

### Koyeb (Recommended)
- Native Docker support
- Automatic HTTPS
- Global edge network
- Free tier available

### Railway
- Connect GitHub repo
- Auto-deploy on push
- Environment variables in dashboard

### Render
- Web Service deployment
- Free tier with sleep
- Custom domains

### Heroku
- Uses Procfile
- Add buildpack: `heroku/python`
- Set config vars in dashboard

### AWS/GCP/Azure
- Use Dockerfile
- Deploy to ECS/Cloud Run/Container Instances
- Set environment variables

## 🐛 Troubleshooting

**"No transactions found"**
- Ensure PDF is text-based (not scanned image)
- Check if bank statement format is supported

**"Upload failed"**
- Check file size (max 16MB)
- Ensure file is PDF format

**"Session expired"**
- Upload timeout: re-upload the file
- Server restart: start fresh

## 📄 License

MIT License - Free for personal and commercial use.

## 🤝 Contributing

1. Fork the repository
2. Create feature branch
3. Commit changes
4. Push to branch
5. Open Pull Request

---

**Made with ❤️ for Bangladeshi banks**
