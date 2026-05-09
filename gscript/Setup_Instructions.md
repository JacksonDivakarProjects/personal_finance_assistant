# Google Apps Script Backend Setup Guide

## Overview
This Google Apps Script backend handles all logic for your Finance Bot:
- Manages multi-step conversations
- Reads/writes to Google Sheets
- Integrates with Groq LLM
- Receives messages from Telegram Bot via webhook

## Step-by-Step Setup

### 1. Create Google Apps Script Project

1. Go to [script.google.com](https://script.google.com)
2. Click **New Project**
3. Name it: `Finance Bot Backend`
4. Replace all content with `Code.gs` from this folder
5. Save (Ctrl+S)

### 2. Configure Script Properties

1. In Apps Script editor, go to **Project Settings** (gear icon)
2. Under "Script Properties", add these key-value pairs:

| Property Name | Value | Where to Get |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token | [@BotFather](https://t.me/botfather) on Telegram |
| `GROQ_API_KEY` | Your Groq API key | [console.groq.com](https://console.groq.com) |
| `SHEET_ID` | Your Google Sheet ID | From Sheet URL: `docs.google.com/spreadsheets/d/**[SHEET_ID]**/edit` |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | (Optional, defaults to this) |

### 3. Create Google Apps Script Web App

1. In Apps Script editor, click **Deploy** → **New Deployment**
2. Select deployment type: **Web app**
3. Configure:
   - **Execute as**: Your Google account
   - **Who has access**: "Anyone" (for Telegram webhook)
4. Click **Deploy**
5. Copy the deployment URL (you'll need this for Telegram webhook)

Example URL: `https://script.google.com/macros/d/{DEPLOYMENT_ID}/userweb`

### 4. Set Telegram Webhook

Instead of polling, configure webhook so Telegram sends messages to your script:

**Option A: Using cURL (Terminal/Command Prompt)**
```bash
curl -X POST https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook \
  -H 'Content-Type: application/json' \
  -d '{"url": "YOUR_APPS_SCRIPT_URL"}'
```

**Option B: Using Python**
```python
import requests

TOKEN = 'your_telegram_bot_token'
WEBHOOK_URL = 'https://script.google.com/macros/d/{DEPLOYMENT_ID}/userweb'

requests.post(
    f'https://api.telegram.org/bot{TOKEN}/setWebhook',
    json={'url': WEBHOOK_URL}
)
```

**Verify webhook:**
```bash
curl https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo
```

### 5. Create/Update Google Sheet

Ensure your Google Sheet has these sheets with proper headers:

**Sheet: "Expense Journal"**
```
| Year | Item | Amount (₹) | Day | Month | Category | Subcategory | Notes |
|------|------|------------|-----|-------|----------|-------------|-------|
```

**Sheet: "Item & Category"**
```
| Item name | Category |
|-----------|----------|
| Coffee    | Food     |
| Milk      | Grocery  |
```

**Sheet: "Category Budget"**
```
| Category | Amount |
|----------|--------|
| Food     | 5000   |
| Rent     | 20000  |
```

**Sheet: "Next Month Budget" (optional)**
```
| Category | Amount |
|----------|--------|
| Food     | 6000   |
```

**Sheet: "User States" (auto-created)**
- Stores multi-step conversation states
- Auto-created on first use

## Testing

### Test in Apps Script
1. In Apps Script editor, click **Execute** → **testBotLogic**
2. Check Logs (Ctrl+Enter) for results

### Test with Telegram Bot
1. Send message to your bot: `Add 100 for coffee`
2. Bot asks: "💰 How much did you spend on Coffee?"
3. Reply: `100`
4. Bot asks: "Match 'Coffee' to 'Food'? Reply: y (yes), n (no), or type a category name"
5. Reply: `y`
6. Bot confirms: "✅ Added expense..."

## How It Works

### Message Flow
```
┌──────────────────────────┐
│ Telegram Bot │──(webhook)──→ doPost() in Apps Script
└──────────────────────────┘                      │
                                                  ↓
                          processUserMessage()
                                                  │
                          ┌──────────────────────┴──────────────────┐
                          ↓                                          ↓
                    classifyIntent()                         handleQuery()
                          │                                          │
                    ┌─────┴─────┐                                    ↓
                    ↓           ↓                            queryGroqLLM()
              handleWrite()  route                                   │
                    │        logic                                   ↓
                    ↓                                            Groq API response
         (multi-step: init, amount,                                 │
          category, confirm, add)                                   ↓
                    │                                            sendTelegramMessage()
                    ↓                                                    │
            addExpense() to Sheet                        ┌──────────────┴──────────────┐
                    │                                    ↓                             ↓
                    └────→ sendTelegramMessage() ←── Telegram API
```

### State Management

For multi-step conversations, user state is stored in "User States" sheet:

```javascript
{
  "pending_step": "amount",        // Current step: init, amount, category
  "parsed": {
    "item": "Coffee",
    "amount": 0,
    "day": 8,
    "month": 5,
    "year": 2026
  },
  "suggested": "Food"              // Suggested category
}
```

## Troubleshooting

### Bot not receiving messages
- ✅ Check webhook is set: `curl https://api.telegram.org/bot{TOKEN}/getWebhookInfo`
- ✅ Check Apps Script logs for errors
- ✅ Verify Sheet ID in Script Properties

### Groq API errors
- ✅ Check GROQ_API_KEY is valid
- ✅ Check rate limits: https://console.groq.com
- ✅ Verify model name is correct

### Sheet not updating
- ✅ Check SHEET_ID is correct
- ✅ Verify Google account has access to sheet
- ✅ Check sheet names match exactly (case-sensitive)

### Multi-step conversation broken
- ✅ Check "User States" sheet exists
- ✅ Check user ID is stored correctly
- ✅ Clear user state by editing the row manually

## Environment Variables File (Optional)

If you want to keep a local `.env` for reference:

```
TELEGRAM_BOT_TOKEN=123456789:ABCDefghijklmnop
GROQ_API_KEY=gsk_abc123def456
SHEET_ID=1a2b3c4d5e6f7g8h9i0j
GROQ_MODEL=llama-3.3-70b-versatile
```

## Security Notes

1. **Never commit credentials** to GitHub
2. **Use Script Properties** for sensitive data
3. **Restrict webhook access** - Only Telegram sends requests
4. **Validate input** - Bot validates all user input
5. **Rate limiting** - Consider adding Groq rate limit handling

## Next Steps

1. ✅ Test basic messaging
2. ✅ Test expense addition (multi-step)
3. ✅ Test budget queries
4. ✅ Monitor Apps Script logs for issues
5. ✅ Adjust category keywords in `suggestCategory()`
6. ✅ Add error notifications

## API Limits

| Service | Limit | Plan |
|---------|-------|------|
| Groq LLM | Free tier available | [console.groq.com](https://console.groq.com) |
| Telegram | Unlimited | Free |
| Google Apps Script | 20K API calls/day | Free |
| Google Sheets | Unlimited rows | Free |

## Support

For issues:
1. Check Apps Script **Logs** (Ctrl+Enter)
2. Check Telegram bot **error messages**
3. Test with `testBotLogic()`
4. Review this guide's troubleshooting section
