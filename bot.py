# bot.py
import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import your existing modules
from sheet_client import SheetClient
from data_loader import load_expense_journal, load_item_category, load_budget, get_actual_spending
from data_writer import DataWriter
from agent import build_graph, AgentState

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 1. Load configuration
# ----------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # optional, for single‑user mode

if not TELEGRAM_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment variables")

# ----------------------------------------------------------------------
# 2. Initialise Google Sheets and your agent (same as main.py)
# ----------------------------------------------------------------------
sheet_client = SheetClient()

expense_df = load_expense_journal(sheet_client)
item_to_cat = load_item_category(sheet_client)
budget_dict = load_budget(sheet_client, "Category Budget")
next_budget_dict = load_budget(sheet_client, "Next Month Budget")

actual_spend, total_actual = get_actual_spending(expense_df, item_to_cat)

for cat in actual_spend:
    if cat not in budget_dict and cat in next_budget_dict:
        budget_dict[cat] = next_budget_dict[cat]
    elif cat not in budget_dict:
        budget_dict[cat] = 0.0

data_context = {
    "actual": actual_spend,
    "budget": budget_dict,
    "total_actual": total_actual,
    "expense_df": expense_df,
    "item_to_cat": item_to_cat,
    "sheet_client": sheet_client,
    "writer": None,          # will be filled inside build_graph
}

writer = DataWriter(sheet_client)
run_agent = build_graph(writer, sheet_client, data_context)

# ----------------------------------------------------------------------
# 3. Telegram Handlers
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    await update.message.reply_text(
        "💰 **Personal Finance Bot Ready!**\n\n"
        "I can help you track expenses, add new spending, and analyse your budget.\n\n"
        "**Examples:**\n"
        "- `How much did I spend on Rent?`\n"
        "- `Add ₹500 for Grocery on May 10th`\n"
        "Just send me a message.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process user message and reply with the agent's answer."""
    user_input = update.message.text
    chat_id = update.effective_chat.id

    # Optional: restrict to a specific chat
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        logger.warning(f"Unauthorised access from chat {chat_id}")
        await update.message.reply_text("Sorry, you are not authorised to use this bot.")
        return

    logger.info(f"User {chat_id}: {user_input}")

    # Check if there is a pending state for this user
    pending_state = context.user_data.get("pending_state")

    # Build the state for the agent
    state: AgentState = {
        "user_query": user_input,
        "intent": None,
        "parsed_write": None,
        "data_context": data_context,
        "final_answer": "",
        "pending_question": None,
        "pending_state": pending_state   # None or a dict
    }

    try:
        # Run the agent (synchronous – may return a pending question)
        result_state = run_agent(state)

        # Handle pending question
        if result_state.get("pending_question"):
            # Store the new pending state for this user
            context.user_data["pending_state"] = result_state["pending_state"]
            await update.message.reply_text(result_state["pending_question"])
        else:
            # No pending – clear any old pending state and send final answer
            context.user_data.pop("pending_state", None)
            # Split long messages if necessary
            answer = result_state["final_answer"]
            if len(answer) > 4000:
                for i in range(0, len(answer), 4000):
                    await update.message.reply_text(answer[i:i+4000])
            else:
                await update.message.reply_text(answer)

    except Exception as e:
        logger.exception("Agent error")
        await update.message.reply_text(f"❌ An error occurred: {str(e)}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors."""
    logger.warning(f"Update {update} caused error {context.error}")

# ----------------------------------------------------------------------
# 4. Main
# ----------------------------------------------------------------------
def main():
    """Start the Telegram bot."""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    # Start polling (for persistent hosting)
    logger.info("Bot is polling...")
    application.run_polling()

if __name__ == "__main__":
    main()