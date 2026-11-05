"""Settings shared by the budget jobs (stored once, under Reconcile budget month)."""

from taskdeck.core import Field

DEFAULT_SHEET_ID = "1qVRjt1BA1VpbfzYN8dxxiJA8cziUkX6baMXHa0bm8fY"
DEFAULT_URL = "https://budget.tailc54221.ts.net"  # the Tailscale Service "budget" on the AI Workstation

SETTINGS = (
    Field.text("budget_url", "Budget service address", default=DEFAULT_URL, required=True,
               help="The budget upload page's address (the bank transactions come from there)."),
    Field.text("sheet_id", "Budget sheet ID", default=DEFAULT_SHEET_ID, required=True),
    Field.text("snapshot_tab", "Budget list tab", default="SnapShot",
               help="The tab listing income and expenses: Income and Expense sections, each with Account, "
                    "Periodicity, Day of Period, Monthly, Weekly, Annually."),
    Field.text("exceptions_tab", "Exceptions tab", default="Exceptions",
               help="Charges set aside or disputed: Match, Date, Amount, Status, Note. Optional."),
    Field.text("service_account_json", "Google service account key (JSON)", secret=True, importable=True, required=True,
               help="The same key as Daily Log works: import the same .json. Share the budget sheet with the key's "
                    "client_email as Editor."),
)
