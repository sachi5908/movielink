#!/bin/bash

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install

# Run the bot
python main.py
