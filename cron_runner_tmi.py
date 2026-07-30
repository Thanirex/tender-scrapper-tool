#!/usr/bin/env python3
"""
Dedicated standalone OS process runner for TMI Team Daily Scrapes.
Invoked via CLI or subprocess: python cron_runner_tmi.py
"""
import os
import sys
from pathlib import Path

# Ensure application root is in python path
APP_DIR = Path(__file__).parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import paths
import db
import cron_runner

def main():
    team_id = "tmi"
    print(f"[TAiQ TMI Runner] Starting dedicated process for TMI portal (PID: {os.getpid()})...")
    
    # Initialize DB connection for runner process
    database = db.TenderDB(paths.DB_PATH)
    cron_runner._db = database

    # Execute TMI daily scrape job in this isolated process
    cron_runner.run_daily_job(team_id=team_id)
    print(f"[TAiQ TMI Runner] Process complete for TMI portal (PID: {os.getpid()}).")

if __name__ == "__main__":
    main()
