# Tender Scraper Agent V2

A modern, fast, and structured web scraper for B2B/B2G tenders, powered by Playwright and Groq LlaMa models. 

## Features
- **Config-Driven Scraping:** Understands website structures via `sites_config.json`.
- **Keyword Loop:** Ingests `Keywords.json` and automatically searches down the line.
- **Agentic Summarization:** Uses lightning-fast Groq models to summarize massive tender descriptors.
- **Beautiful Output:** Automatically dumps and formats results into an Excel table with strict column width sizing and text-wrapping.

## Getting Started

### 1. Requirements
Ensure Python is installed, then install the modern dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

*(Note: Playwright requires installing its own browser binary the first time via `playwright install chromium`).*

### 2. Environment Setup
1. Copy `.env.example` -> `.env`
2. Open `.env` and paste your genuine Groq API Key:
   ```env
   GROQ_API_KEY=gsk_your_key_here...
   ```

### 3. Usage
Simply run the master pipeline. It acts as a CLI backend tool:
```bash
python main.py
```

The scraper will read `Keywords.json`, execute searches on `ngobox.org`, summarize texts, and store everything gracefully into `data/outputs/Scrape_Results.xlsx`.
