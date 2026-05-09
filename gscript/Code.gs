// ============================================================================
// GOOGLE APPS SCRIPT - FINANCE BOT BACKEND
// ============================================================================
// This script handles all bot logic and Google Sheets operations
// Telegram Bot sends messages via webhook to doPost() function

const TELEGRAM_BOT_TOKEN = PropertiesService.getScriptProperties().getProperty('TELEGRAM_BOT_TOKEN');
const GROQ_API_KEY = PropertiesService.getScriptProperties().getProperty('GROQ_API_KEY');
const GROQ_MODEL = PropertiesService.getScriptProperties().getProperty('GROQ_MODEL') || 'llama-3.3-70b-versatile';
const SHEET_ID = PropertiesService.getScriptProperties().getProperty('SHEET_ID');

const EXPENSES_SHEET = 'Expense Journal';
const CATEGORY_SHEET = 'Item & Category';
const BUDGET_SHEET = 'Category Budget';
const NEXT_MONTH_SHEET = 'Next Month Budget';
const USER_STATE_SHEET = 'User States';

// ============================================================================
// WEBHOOK HANDLER - Entry Point for Telegram Bot
// ============================================================================
function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const chatId = payload.message.chat.id;
    const userId = payload.message.from.id;
    const userMessage = payload.message.text;

    Logger.log(`Chat ID: ${chatId}, User Message: ${userMessage}`);

    // Process the message
    const response = processUserMessage(userId, chatId, userMessage);
    
    // Send response back to Telegram
    sendTelegramMessage(chatId, response);

    return ContentService.createTextOutput(JSON.stringify({ ok: true })).setMimeType(ContentService.MimeType.JSON);
  } catch (error) {
    Logger.log('Error in doPost: ' + error.toString());
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: error.toString() })).setMimeType(ContentService.MimeType.JSON);
  }
}

// ============================================================================
// MAIN MESSAGE PROCESSOR
// ============================================================================
function processUserMessage(userId, chatId, userMessage) {
  try {
    // Load or create user state
    let userState = loadUserState(userId);
    
    // Classify intent
    let intent = classifyIntent(userMessage, userState);
    
    // Route to appropriate handler
    let response;
    if (intent === 'query') {
      response = handleQuery(userMessage, userId);
    } else if (intent === 'write') {
      response = handleWrite(userMessage, userState, userId);
    } else {
      response = "I didn't understand. Try 'Add 100 for coffee' or 'How much did I spend on food?'";
    }
    
    return response;
  } catch (error) {
    Logger.log('Error processing message: ' + error.toString());
    return '❌ An error occurred: ' + error.toString();
  }
}

// ============================================================================
// INTENT CLASSIFICATION
// ============================================================================
function classifyIntent(message, userState) {
  if (userState.pending_step) {
    return 'write'; // Continue multi-step write process
  }

  const lower = message.toLowerCase();
  
  const queryIndicators = ['how much', 'what is', 'show me', 'total', 'report', 'budget', 'spent'];
  if (queryIndicators.some(ind => lower.includes(ind))) {
    return 'query';
  }
  
  const writeKeywords = ['add', 'spent', 'bought', 'paid', '₹'];
  if (writeKeywords.some(kw => lower.includes(kw)) || /\d+\s*(rs|rupees|₹)/i.test(message)) {
    return 'write';
  }
  
  return 'query'; // Default
}

// ============================================================================
// QUERY HANDLER
// ============================================================================
function handleQuery(message, userId) {
  try {
    // Load data
    const expenses = loadExpenseData();
    const itemToCategory = loadItemCategoryMapping();
    const budget = loadBudgetData(BUDGET_SHEET);
    
    // Calculate actual spending
    const actual = calculateActualSpending(expenses, itemToCategory);
    
    // Build data context
    let dataText = formatDataContext(actual, budget);
    
    // Use Groq LLM to answer
    const answer = queryGroqLLM(message, dataText);
    
    return answer || 'Could not process your query.';
  } catch (error) {
    Logger.log('Error in handleQuery: ' + error.toString());
    return '❌ Error querying data: ' + error.toString();
  }
}

// ============================================================================
// WRITE HANDLER - Multi-step expense addition
// ============================================================================
function handleWrite(message, userState, userId) {
  try {
    const step = userState.pending_step || 'init';
    
    if (step === 'init') {
      // Step 1: Parse initial message and ask for missing info
      return handleWriteInit(message, userState, userId);
    } else if (step === 'amount') {
      // Step 2: Got amount, now ask for category
      return handleWriteAmount(message, userState, userId);
    } else if (step === 'category') {
      // Step 3: Got category, add expense
      return handleWriteCategory(message, userState, userId);
    }
    
    return 'Unexpected state. Please start over.';
  } catch (error) {
    Logger.log('Error in handleWrite: ' + error.toString());
    clearUserState(userId);
    return '❌ Error adding expense: ' + error.toString();
  }
}

// Step 1: Initialize write - parse and ask for missing fields
function handleWriteInit(message, userState, userId) {
  // Parse the message
  const parsed = parseExpenseMessage(message);
  
  // Check if amount is missing
  if (!parsed.amount || parsed.amount === 0) {
    // Ask for amount
    saveUserState(userId, {
      pending_step: 'amount',
      parsed: parsed
    });
    return `💰 How much did you spend on ${parsed.item}? (e.g., 150)`;
  }
  
  // Amount provided, ask for category
  const categories = getExistingCategories();
  const suggested = suggestCategory(parsed.item, categories);
  
  if (suggested) {
    saveUserState(userId, {
      pending_step: 'category',
      parsed: parsed,
      suggested: suggested
    });
    return `Match '${parsed.item}' to '${suggested}'? Reply: y (yes), n (no), or type a category name`;
  } else {
    saveUserState(userId, {
      pending_step: 'category',
      parsed: parsed
    });
    return `What category for '${parsed.item}'?`;
  }
}

// Step 2: Process amount and ask for category
function handleWriteAmount(message, userState, userId) {
  const amountMatch = message.match(/(\d+(?:\.\d+)?)/);
  if (!amountMatch) {
    return '❌ Invalid amount. Please send a number, e.g., 150';
  }
  
  userState.parsed.amount = parseFloat(amountMatch[1]);
  
  // Ask for category
  const categories = getExistingCategories();
  const item = userState.parsed.item;
  const suggested = suggestCategory(item, categories);
  
  userState.pending_step = 'category';
  userState.suggested = suggested;
  saveUserState(userId, userState);
  
  if (suggested) {
    return `Match '${item}' to '${suggested}'? Reply: y (yes), n (no), or type a category name`;
  } else {
    return `What category for '${item}'?`;
  }
}

// Step 3: Process category and add expense
function handleWriteCategory(message, userState, userId) {
  let category;
  
  if (message.toLowerCase().startsWith('y')) {
    category = userState.suggested;
  } else if (message.toLowerCase().startsWith('n')) {
    // User rejected, ask again
    userState.pending_step = 'category';
    userState.suggested = null;
    saveUserState(userId, userState);
    return "What category should we use instead?";
  } else {
    category = message.trim();
  }
  
  // Add category mapping if new
  const existing = loadItemCategoryMapping();
  if (!existing[userState.parsed.item]) {
    addCategoryMapping(userState.parsed.item, category);
  }
  
  // Add expense
  const now = new Date();
  const day = now.getDate();
  const month = now.getMonth() + 1;
  const year = now.getFullYear();
  
  const result = addExpense(
    year,
    userState.parsed.item,
    userState.parsed.amount,
    day,
    month,
    category
  );
  
  // Clear user state
  clearUserState(userId);
  
  return result;
}

// ============================================================================
// PARSE EXPENSE MESSAGE
// ============================================================================
function parseExpenseMessage(message) {
  const parsed = {
    item: 'Unknown',
    amount: 0,
    day: new Date().getDate(),
    month: new Date().getMonth() + 1,
    year: new Date().getFullYear()
  };
  
  // Extract amount
  const amountMatch = message.match(/(\d+(?:\.\d+)?)/);
  if (amountMatch) {
    parsed.amount = parseFloat(amountMatch[1]);
  }
  
  // Extract item name (first non-number word)
  const words = message.split(/\s+/);
  for (let word of words) {
    if (!/^(\d+|for|rs|rupees|₹|on|at|add|spent|bought|paid)$/i.test(word)) {
      parsed.item = word.charAt(0).toUpperCase() + word.slice(1);
      break;
    }
  }
  
  return parsed;
}

// ============================================================================
// CATEGORY SUGGESTION
// ============================================================================
function suggestCategory(item, categories) {
  const itemLower = item.toLowerCase();
  
  const keywordMap = {
    'Grocery': ['apple', 'orange', 'banana', 'milk', 'bread', 'rice', 'vegetable', 'fruit'],
    'Commute': ['bus', 'train', 'taxi', 'uber', 'petrol', 'fuel', 'metro', 'auto'],
    'Entertainment': ['movie', 'netflix', 'spotify', 'youtube', 'game', 'concert'],
    'Food': ['restaurant', 'cafe', 'dinner', 'lunch', 'pizza', 'burger'],
    'Bills': ['electricity', 'water', 'gas', 'bill', 'broadband', 'internet'],
    'Rent': ['rent', 'lease', 'apartment'],
  };
  
  for (let [cat, keywords] of Object.entries(keywordMap)) {
    for (let kw of keywords) {
      if (itemLower.includes(kw)) {
        return cat;
      }
    }
  }
  
  // Return first existing category if no match
  return categories.length > 0 ? categories[0] : null;
}

// ============================================================================
// GROQ LLM QUERY
// ============================================================================
function queryGroqLLM(userQuery, dataContext) {
  try {
    const url = 'https://api.groq.com/openai/v1/chat/completions';
    
    const prompt = `Answer this finance question using only the data provided. If not about finances, respond helpfully.\n\nData:\n${dataContext}\n\nQuestion: ${userQuery}`;
    
    const payload = {
      model: GROQ_MODEL,
      messages: [
        { role: 'system', content: 'You are a personal finance assistant.' },
        { role: 'user', content: prompt }
      ],
      temperature: 0.2,
      max_tokens: 500
    };
    
    const options = {
      method: 'post',
      contentType: 'application/json',
      headers: {
        'Authorization': `Bearer ${GROQ_API_KEY}`
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    };
    
    const response = UrlFetchApp.fetch(url, options);
    const result = JSON.parse(response.getContentText());
    
    if (result.choices && result.choices[0]) {
      return result.choices[0].message.content;
    }
    
    return 'Could not get response from LLM';
  } catch (error) {
    Logger.log('Error calling Groq: ' + error.toString());
    return 'Error processing query';
  }
}

// ============================================================================
// DATA LOADING FUNCTIONS
// ============================================================================
function loadExpenseData() {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sheet = ss.getSheetByName(EXPENSES_SHEET);
    const data = sheet.getDataRange().getValues();
    
    if (data.length < 2) return [];
    
    const headers = data[0];
    const expenses = [];
    
    for (let i = 1; i < data.length; i++) {
      if (data[i][1]) { // Item column
        expenses.push({
          year: data[i][0],
          item: data[i][1],
          amount: parseFloat(data[i][2]) || 0,
          day: data[i][3],
          month: data[i][4],
          category: data[i][5],
          subcategory: data[i][6],
          notes: data[i][7]
        });
      }
    }
    
    return expenses;
  } catch (error) {
    Logger.log('Error loading expenses: ' + error.toString());
    return [];
  }
}

function loadItemCategoryMapping() {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sheet = ss.getSheetByName(CATEGORY_SHEET);
    const data = sheet.getDataRange().getValues();
    
    const mapping = {};
    for (let i = 1; i < data.length; i++) {
      if (data[i][0]) {
        mapping[data[i][0]] = data[i][1];
      }
    }
    
    return mapping;
  } catch (error) {
    Logger.log('Error loading category mapping: ' + error.toString());
    return {};
  }
}

function loadBudgetData(sheetName) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    const data = sheet.getDataRange().getValues();
    
    const budget = {};
    for (let i = 1; i < data.length; i++) {
      if (data[i][0]) {
        const amount = parseFloat(data[i][1]) || 0;
        budget[data[i][0]] = amount;
      }
    }
    
    return budget;
  } catch (error) {
    Logger.log('Error loading budget: ' + error.toString());
    return {};
  }
}

function getExistingCategories() {
  const mapping = loadItemCategoryMapping();
  return [...new Set(Object.values(mapping))];
}

function calculateActualSpending(expenses, itemToCategory) {
  const actual = {};
  
  for (let expense of expenses) {
    let category = expense.category || itemToCategory[expense.item];
    if (!category) continue;
    
    if (!actual[category]) {
      actual[category] = 0;
    }
    actual[category] += expense.amount;
  }
  
  return actual;
}

function formatDataContext(actual, budget) {
  let text = '';
  let total = Object.values(actual).reduce((a, b) => a + b, 0);
  text += `Total expenses: ₹${total.toFixed(2)}\n\n`;
  text += 'Category breakdown (Actual vs Budget):\n';
  
  for (let cat of Object.keys(actual).sort()) {
    const act = actual[cat];
    const bud = budget[cat] || 0;
    const diff = act - bud;
    const status = diff > 0 ? 'over' : diff < 0 ? 'under' : 'on track';
    text += `  ${cat}: ₹${act.toFixed(2)} vs ₹${bud.toFixed(2)} (${status} by ₹${Math.abs(diff).toFixed(2)})\n`;
  }
  
  return text;
}

// ============================================================================
// DATA WRITING FUNCTIONS
// ============================================================================
function addExpense(year, item, amount, day, month, category) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sheet = ss.getSheetByName(EXPENSES_SHEET);
    
    const lastRow = sheet.getLastRow();
    const nextRow = lastRow + 1;
    
    const now = new Date();
    const notes = `Added via Telegram bot on ${now.toLocaleString()}`;
    
    sheet.getRange(nextRow, 1).setValue(year);
    sheet.getRange(nextRow, 2).setValue(item.charAt(0).toUpperCase() + item.slice(1));
    sheet.getRange(nextRow, 3).setValue(amount);
    sheet.getRange(nextRow, 4).setValue(day);
    sheet.getRange(nextRow, 5).setValue(month);
    sheet.getRange(nextRow, 6).setValue(category);
    sheet.getRange(nextRow, 8).setValue(notes);
    
    return `✅ Added expense:\n📦 Item: ${item}\n💰 Amount: ₹${amount.toFixed(2)}\n📂 Category: ${category}\n📅 Date: ${day}/${month}/${year}`;
  } catch (error) {
    Logger.log('Error adding expense: ' + error.toString());
    return '❌ Failed to add expense: ' + error.toString();
  }
}

function addCategoryMapping(itemName, category) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sheet = ss.getSheetByName(CATEGORY_SHEET);
    
    const lastRow = sheet.getLastRow();
    const nextRow = lastRow + 1;
    
    sheet.getRange(nextRow, 1).setValue(itemName.charAt(0).toUpperCase() + itemName.slice(1));
    sheet.getRange(nextRow, 2).setValue(category);
    
    return true;
  } catch (error) {
    Logger.log('Error adding category mapping: ' + error.toString());
    return false;
  }
}

// ============================================================================
// USER STATE MANAGEMENT (Multi-step conversations)
// ============================================================================
function loadUserState(userId) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    let sheet;
    try {
      sheet = ss.getSheetByName(USER_STATE_SHEET);
    } catch (e) {
      // Create sheet if it doesn't exist
      sheet = ss.insertSheet(USER_STATE_SHEET);
      sheet.appendRow(['User ID', 'State Data']);
    }
    
    const data = sheet.getDataRange().getValues();
    
    for (let i = 1; i < data.length; i++) {
      if (data[i][0] == userId) {
        return JSON.parse(data[i][1] || '{}');
      }
    }
    
    return {};
  } catch (error) {
    Logger.log('Error loading user state: ' + error.toString());
    return {};
  }
}

function saveUserState(userId, stateData) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    let sheet;
    try {
      sheet = ss.getSheetByName(USER_STATE_SHEET);
    } catch (e) {
      sheet = ss.insertSheet(USER_STATE_SHEET);
      sheet.appendRow(['User ID', 'State Data']);
    }
    
    const data = sheet.getDataRange().getValues();
    
    for (let i = 1; i < data.length; i++) {
      if (data[i][0] == userId) {
        sheet.getRange(i + 1, 2).setValue(JSON.stringify(stateData));
        return;
      }
    }
    
    // New user
    sheet.appendRow([userId, JSON.stringify(stateData)]);
  } catch (error) {
    Logger.log('Error saving user state: ' + error.toString());
  }
}

function clearUserState(userId) {
  saveUserState(userId, {});
}

// ============================================================================
// TELEGRAM COMMUNICATION
// ============================================================================
function sendTelegramMessage(chatId, text) {
  try {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
    
    const payload = {
      chat_id: chatId,
      text: text,
      parse_mode: 'Markdown'
    };
    
    const options = {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    };
    
    const response = UrlFetchApp.fetch(url, options);
    Logger.log('Telegram response: ' + response.getContentText());
  } catch (error) {
    Logger.log('Error sending Telegram message: ' + error.toString());
  }
}

// ============================================================================
// SETUP FUNCTION - Configure Script Properties
// ============================================================================
function setupProperties() {
  const props = PropertiesService.getScriptProperties();
  
  // Set these in Script Properties:
  // TELEGRAM_BOT_TOKEN - from @BotFather
  // GROQ_API_KEY - from Groq console
  // SHEET_ID - your Google Sheet ID
  
  Logger.log('Properties need to be set manually:');
  Logger.log('1. TELEGRAM_BOT_TOKEN');
  Logger.log('2. GROQ_API_KEY');
  Logger.log('3. SHEET_ID');
  Logger.log('4. GROQ_MODEL (optional, defaults to llama-3.3-70b-versatile)');
}

// ============================================================================
// TEST FUNCTION
// ============================================================================
function testBotLogic() {
  const testMessages = [
    'Add 500 for coffee',
    'How much did I spend?',
    'Budget status'
  ];
  
  for (let msg of testMessages) {
    Logger.log('Testing: ' + msg);
    const response = processUserMessage(123456, 789, msg);
    Logger.log('Response: ' + response);
  }
}
