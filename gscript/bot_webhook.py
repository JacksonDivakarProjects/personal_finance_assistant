#!/usr/bin/env python3
"""
Simplified Telegram Bot - Webhook Version

This bot receives messages from Telegram and forwards them to the
Google Apps Script backend via webhook.

Note: This script is optional. The Apps Script handles everything.
You can skip this and use @BotFather to set webhook directly.
"""

import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GAS_WEBHOOK_URL = os.getenv('GAS_WEBHOOK_URL')  # Your Apps Script URL
ALLOWED_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')  # Optional

if not TELEGRAM_TOKEN:
    raise ValueError('TELEGRAM_BOT_TOKEN not set')

if not GAS_WEBHOOK_URL:
    raise ValueError('GAS_WEBHOOK_URL not set')

# ============================================================================
# TELEGRAM HANDLERS
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    await update.message.reply_text(
        "💰 **Personal Finance Bot Ready!**\n\n"
        "I can help you track expenses, add new spending, and analyse your budget.\n\n"
        "**Examples:**\n"
        "- 'How much did I spend on Rent?'\n"
        "- 'Add ₹500 for Grocery today'\n"
        "- 'Budget status'\n\n"
        "Just send me a message.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward message to Apps Script (optional - webhook handles this)."""
    user_input = update.message.text
    chat_id = update.effective_chat.id

    # Optional: restrict to specific chat
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        await update.message.reply_text("Sorry, you are not authorized.")
        return

    # Note: If using webhook directly (recommended), the Apps Script
    # handles the message. This handler is optional for polling mode.
    
    await update.message.reply_text(
        "Message received. Processing...\n"
        f"(In webhook mode, Apps Script handles this directly)"
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors."""
    print(f"Update {update} caused error {context.error}")

# ============================================================================
# MAIN - POLLING MODE (Alternative to webhook)
# ============================================================================

def main():
    """
    Run bot in polling mode.
    
    NOTE: For production, use webhook mode:
    1. Set webhook via Telegram API
    2. Apps Script receives messages directly
    3. No polling needed
    
    To set webhook (one-time):
    curl -X POST https://api.telegram.org/bot{TOKEN}/setWebhook \
      -H 'Content-Type: application/json' \
      -d '{"url": "{YOUR_GAS_URL}"}'
    """
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    print("Bot is polling... (Polling mode - webhook is recommended for production)")
    application.run_polling()

if __name__ == "__main__":
    main()
