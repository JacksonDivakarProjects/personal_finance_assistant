# Telegram Bot + Google Apps Script Integration Guide

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ User sends message to Telegram Bot                                  │
└─────────────────────────┬──────────────────────────────────────────┘
                          │
                          │ (Telegram servers)
                          ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Google Apps Script Web App (Backend)                                │
│ ├─ doPost() - Receives webhook                                     │
│ ├─ classifyIntent() - Parse user query                             │
│ ├─ handleQuery() - Answer finance questions                        │
│ ├─ handleWrite() - Multi-step expense addition                     │
│ ├─ queryGroqLLM() - Call Groq API                                  │
│ └─ Google Sheets integration                                       │
└─────────────────────────┬──────────────────────────────────────────┘
                          │
                          │ (Telegram API)
                          ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Bot sends response back to Telegram                                 │
└─────────────────────────────────────────────────────────────────────┘
```

## Why This Architecture?

| Component | Why |
|-----------|-----|
| **Google Apps Script** | Direct Google Sheets access, serverless, free tier |
| **Telegram Webhook** | Real-time message delivery, no polling overhead |
| **Groq LLM** | Fast, free API for AI responses |
| **No Python backend** | Everything runs in Google Apps Script |

## Quick Start (5 minutes)

### 1. Deploy Google Apps Script

**Copy `Code.gs` to your Apps Script project:**

1. Go to [script.google.com](https://script.google.com) → New Project
2. Paste the content of `Code.gs`
3. Save (Ctrl+S)
4. Click **Deploy** → **New Deployment**
5. Select "Web app"
6. Set "Execute as" to your account
7. Set "Who has access" to "Anyone"
8. Click **Deploy**
9. Copy the URL: `https://script.google.com/macros/d/{ID}/userweb`

### 2. Set Script Properties

In Apps Script:
1. Click **Project Settings** (gear icon)
2. Add these properties:
   - `TELEGRAM_BOT_TOKEN` (from @BotFather)
   - `GROQ_API_KEY` (from console.groq.com)
   - `SHEET_ID` (from your Google Sheet URL)

### 3. Configure Telegram Webhook

**Option A: Using cURL**
```bash
curl -X POST https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://script.google.com/macros/d/<DEPLOYMENT_ID>/userweb"}'
```

**Option B: Using Python**
```python
import requests

token = "your_token"
url = "https://script.google.com/macros/d/DEPLOYMENT_ID/userweb"

requests.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={"url": url}
)
```

**Verify:**
```bash
curl https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo
```

Should show:
```json
{
  "ok": true,
  "result": {
    "url": "https://script.google.com/macros/d/.../userweb",
    "has_custom_certificate": false,
    "pending_update_count": 0
  }
}
```

### 4. Test Your Bot

Send to your bot on Telegram:
```
/start
```

Then:
```
Add 100 for coffee
```

Bot should ask for confirmation.

## Message Flow Examples

### Example 1: Add Expense
```
User: "Add 100 for coffee"
  ↓
Apps Script: classifyIntent() → "write"
  ↓
Apps Script: parseExpenseMessage() → {item: "Coffee", amount: 100}
  ↓
Bot: "Match 'Coffee' to 'Food'? (y/n)"
  ↓
User: "y"
  ↓
Apps Script: addExpense() → Write to Google Sheets
  ↓
Bot: "✅ Added expense: Coffee for ₹100"
```

### Example 2: Query Budget
```
User: "How much did I spend on food?"
  ↓
Apps Script: classifyIntent() → "query"
  ↓
Apps Script: loadExpenseData() + calculateActualSpending()
  ↓
Apps Script: queryGroqLLM() → "You spent ₹2500 on food this month"
  ↓
Bot: "You spent ₹2500 on food this month"
```

## Environment Variables

**Store these in Apps Script Project Settings, NOT in code:**

```
TELEGRAM_BOT_TOKEN = 123456789:ABCDefghijklmnop...
GROQ_API_KEY = gsk_abc123def456...
SHEET_ID = 1a2b3c4d5e6f7g8h9i0j...
GROQ_MODEL = llama-3.3-70b-versatile (optional)
```

## Google Sheet Setup

Your Google Sheet must have these sheets:

### "Expense Journal" sheet
| Year | Item | Amount (₹) | Day | Month | Category | Subcategory | Notes |
|------|------|-----------|-----|-------|----------|-------------|-------|
| 2026 | Coffee | 100 | 8 | 5 | Food | | Added via bot |

### "Item & Category" sheet
| Item name | Category |
|-----------|----------|
| Coffee | Food |
| Milk | Grocery |

### "Category Budget" sheet
| Category | Amount |
|----------|--------|
| Food | 5000 |
| Rent | 20000 |

## Common Issues & Solutions

### Issue: Bot not responding

**Solution:**
1. Check webhook is set: 
   ```bash
   curl https://api.telegram.org/bot{TOKEN}/getWebhookInfo
   ```
2. Check Apps Script logs for errors (in editor: Ctrl+Enter)
3. Test webhook manually:
   ```bash
   curl -X POST https://script.google.com/macros/d/{ID}/userweb \
     -H 'Content-Type: application/json' \
     -d '{"message": {"text": "test", "chat": {"id": 123}}}'
   ```

### Issue: "SHEET_ID not set" error

**Solution:**
1. Get Sheet ID from URL: `docs.google.com/spreadsheets/d/**SHEET_ID**/edit`
2. Add to Project Settings → Script Properties
3. Redeploy web app

### Issue: Groq API errors

**Solution:**
1. Verify API key: https://console.groq.com
2. Check rate limits
3. Verify model name: `llama-3.3-70b-versatile`

### Issue: Multi-step conversation broken

**Solution:**
1. Check "User States" sheet was created
2. Check user ID stored correctly
3. Manually delete the user's row in "User States"
4. Test again

## Security Checklist

- [ ] TELEGRAM_BOT_TOKEN in Script Properties (not in code)
- [ ] GROQ_API_KEY in Script Properties (not in code)
- [ ] SHEET_ID in Script Properties (not in code)
- [ ] Webhook URL set in Telegram API
- [ ] Apps Script deployed as "Anyone" (webhook needs public access)
- [ ] Input validation in `parseExpenseMessage()`
- [ ] Rate limiting on Groq API calls

## Monitoring

### Check webhook delivery
```bash
curl https://api.telegram.org/bot{TOKEN}/getWebhookInfo | jq .
```

### View Apps Script logs
1. In Apps Script editor: Ctrl+Enter
2. Filter by function/timestamp
3. Look for errors

### Monitor Groq usage
1. Go to [console.groq.com](https://console.groq.com)
2. Check API usage/rate limits
3. View logs

## Cost

| Service | Free Tier | Cost |
|---------|-----------|------|
| Google Apps Script | 20K API calls/day | Free |
| Telegram Bot API | Unlimited | Free |
| Groq LLM | ~100 req/min | Free (paid plans available) |
| Google Sheets | Unlimited rows | Free |
| **Total** | - | **FREE** 🎉 |
