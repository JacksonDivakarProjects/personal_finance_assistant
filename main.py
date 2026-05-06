from sheet_client import SheetClient
from data_loader import load_expense_journal, load_item_category, load_budget, get_actual_spending
from data_writer import DataWriter
from agent import build_graph, AgentState

def main():
    sheet_client = SheetClient()

    # Load initial data
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
        "writer": None
    }

    writer = DataWriter(sheet_client)
    run_agent = build_graph(writer, sheet_client, data_context)

    print("\n📊 Finance Bot Ready. I answer questions and add expenses using your Google Sheet.")
    print("Examples:")
    print("  - 'How much did I spend on Rent?'")
    print("  - 'Add ₹500 for Grocery on May 10th'")
    print("  - 'Update Commute budget to ₹600'")
    print("Type 'exit' to quit.\n")

    while True:
        user_input = input("❓ You: ").strip()
        if user_input.lower() in ["exit", "quit"]:
            break
        if not user_input:
            continue

        state: AgentState = {
            "user_query": user_input,
            "intent": None,
            "parsed_write": None,
            "data_context": data_context,
            "final_answer": ""
        }
        result = run_agent(state)
        print(f"🤖 Bot: {result['final_answer']}\n")

if __name__ == "__main__":
    main()