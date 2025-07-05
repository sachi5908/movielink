#!/bin/bash

# Install all python dependencies
pip install -r requirements.txt

# Install Playwright's browser dependencies
python -m playwright install
python -m playwright install-deps

# Run the main application
python main.py
