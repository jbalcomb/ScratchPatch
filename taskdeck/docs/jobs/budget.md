# Budget jobs

Three jobs, in the **Money** category, that bring together:

* the budget sheet's **SnapShot** tab (income and expense lines) and **Exceptions** tab (charges set aside or
  disputed), which you edit in Google Sheets, and
* the **bank transactions** of the three Bank of America accounts, which the budget service on the AI Workstation
  collects from uploaded exports ([budget/README.md](../../../budget/README.md)).

The sheet's design, tab by tab, is in the **BudgetManagerGoogleSheetDesign** doc (Google Drive,
Projects/ScratchPatch/BudgetManager). This page is how the code follows it.

| Job | Run | Write to sheet |
|---|---|---|
| **Reconcile budget month** | A month (blank = the latest with bank data) against SnapShot | the **Reconcile (auto)** tab |
| **Update budget timeline** | Every transaction since the 1st of the month 3 months back, then what's coming through the end of next month | the **TimeLine** and **Projected** tabs |
| **Update ThisWeek** | This budget week (Wednesday to Tuesday) item by item, then the 3 weeks after, summed | the **ThisWeek** tab |

Running only reads. **Write to sheet** replaces the job's tab (created the first time). TimeLine is shared: rows you
type there are kept until the bank or the Cash tab has them (see below).

## Accounts

The nicknames from the sheet's Accounts tab: **Daily** (the budget service's `checking`), **Bills**, **Save**
(`savings`) and **Cash**. Cash isn't a bank account: an ATM withdrawal (category `Cash` in rules.txt) moves money
from its account into it, an ATM deposit moves it back, and the **Cash** tab records the rest by hand.
Rent's cash comes out of **Daily** (its ATM card): the timeline expects an ATM withdrawal from Bills on each rent day, unless one
(at least the rent) already happened in the week before.

**Cash tab** (hand-entered): Date, Description, Amount (+ in, − out), Line, Note. Line is the SnapShot line it
counts against (Rent, Groceries…), or blank. Its first row is the cash on hand when you start it; from that day on,
Cash is that plus every entry and every ATM withdrawal (minus deposits). With no entries Cash starts at $0.
A cash bill (rent) counts as paid only when the Cash tab has it on its line; one that's due and not entered shows
as *not paid* and isn't projected (it's a debt: the **Debts** tab, which the code doesn't read). Entries count
everywhere bank transactions do: against their line, in the weekly targets, and as actual rows on the timeline.

## How a transaction is tied to a budget line

By name. A SnapShot line's name is also a **category** in the budget service's `budget/rules.txt`:
`EXETERFINA = Car Payment` makes the Exeter Finance loan payment the "Car Payment" line, and
`INSTACART = Instacart` the Instacart income line. To add a line: add the row to SnapShot, then a rule with
exactly the same name, then `categorize` on the workstation (or upload anything). A rule can be limited to one
account, e.g. `[bills] Monthly Maintenance Fee = BofA Fee - Bills`.

SnapShot's top rows: **Expense totals**, **Bills total** (the lines whose Account is Bills) and **Bills funding**:
the weekly transfer from Daily to Bills, typed in the Weekly column. The code reads only Bills funding.

SnapShot: an **Income** section, then an **Expense** section, each starting with a header row whose first cell is
"Income" or "Expense". Columns used, found by their header: **Status**, **Account**, **Periodicity**,
**Day of Period**, **Monthly**, **Weekly**, **Annually**. A line whose Status is **Cancelled** is cancelled (its
strikethrough is for people; strikethrough only counts on a sheet without a Status column).

| Line looks like | Treated as |
|---|---|
| *Monthly*, a day of the month | a **bill**: expected once a month, about the Monthly amount, around that day |
| *Weekly*, a weekday (Instacart: Wed) | every week on that weekday, the Weekly amount (income: the baseline) |
| *Weekly*, Day "-" (Groceries, Motor Fuel) | **spending**: the Weekly amount is each week's target |
| *Annually*, a month/day (Amazon Prime: 4/15) | once a year on that day, the Annually amount |
| Status *Cancelled* | **cancelled**: not projected; reported if it still charges |

Budget weeks run **Wednesday to Tuesday** (payday to the day before); a week belongs to the month its Wednesday
is in.

Exceptions: rows of Match (text in the bank description), Date and Amount (optional, to pick out one charge),
Status (**Set aside** or **Disputed**), Note. A disputed charge is paired with the later credit for the same amount
in the same account (the bank's Temporary Credit Adjustment).

## Reconcile budget month

* **Income**: each income line, weekly ones week by week against the baseline ("so far" for a week the bank data
  hasn't finished, "not yet" for one that hasn't started); then other income (e.g. Income: Paycheck).
* **Bills**: each one *paid* (date, amount), *paid +$X vs the budget* (amount changed by more than 2% or $1),
  *due 10/25* (not past its day + 3 days in the bank data yet), **missing**, *disputed* (with the temporary credit),
  *cancelled* or *cancelled, but still charged*, or *paid in cash* for a Cash line. When a category has two charges
  (Claude's $106.75 and $21.35), the one closest to the budget amount is the bill and the other is listed below as
  an extra charge.
* **Spending**: each weekly line week by week; the month's target is Weekly × the Wednesdays in the month.
* **Not on SnapShot**: every other expense, grouped by category (or by merchant when no rule matches). *Repeats*
  means it was also charged in 2 of the 3 months before. Transfers, ATM moves to Cash, income and Exceptions are
  left out.
* **Disputed** and **Set aside**: the Exceptions tab's charges in the month.

## Update budget timeline

**TimeLine**: every real transaction, from the 1st of the month three months back up to now, newest first, with
each account's balance: Date, Account, Description, Category, Amount, Daily, Bills, Save, Cash, Total. It comes
from three places, and the more official one replaces the other:

1. the **bank exports** (and the cash side of each ATM withdrawal or deposit);
2. the **Cash tab**;
3. **rows typed on TimeLine** itself: a transaction still pending at the bank (the export only has posted ones),
   or cash spent that isn't on the Cash tab.

A row is replaced when a more official one is in the same account for the same amount, dated the same day or
later (a purchase posts a day or two after it's made). The rows the job wrote last time match their own bank
transactions, so they're replaced too; whatever is left over is yours and stays. A typed row's balance carries on
top of the bank's running balance, so the balances match the bank's screen rather than the export. A typed row
counts like any other transaction: against its SnapShot line (its Category, or a Description that is a line's
name) and in the weekly targets. Rows older than the window drop off.

**Projected**: the same history, then the expected rows through the end of next month (newest first), with an
**Actual / Expected** column:
* bills on their day, unless already paid (or disputed) that month; yearly lines on their date; a cash bill due
  and not paid isn't projected (it's a debt);
* income lines on their weekday at the baseline;
* **Bills funding** every Wednesday: a row out of Daily and a row into Bills (this week's only when no transfer from
  Daily to Bills is in yet);
* spending: each week's target on its Wednesday, and for the current week only what's left of it;
* rent: an ATM withdrawal from Daily into Cash, then the payment from Cash;
* cancelled lines and lines without an amount are left out.

The result says each account's lowest point, and for one that goes negative, the day and how much to move in (or
take out in cash) before then.

## Update ThisWeek

Run it after the Wednesday-morning update (exports uploaded). This budget week, Wednesday to Tuesday: each income,
expense and transfer with every account's balance (actual where the bank has it, expected otherwise), then the
three weeks after: income, expenses and the balances at the end of each week. It replaces the whole ThisWeek tab.

## One-time setup

1. **Share the budget sheet** with the service account's `client_email` (inside its `.json` key: the same one
   Daily Log uses) as **Editor**.
2. On the phone, open any of the three jobs → settings (tune icon):

   | Setting | Value |
   |---|---|
   | Budget service address | pre-filled: `https://budget.tailc54221.ts.net` (the upload page's address) |
   | Budget sheet ID | pre-filled with the Budget-2026-TimeLine sheet |
   | Budget list tab | `SnapShot` |
   | Exceptions tab | `Exceptions` |
   | Google service account key | Import the same `.json` as Daily Log |

   The three jobs share these settings. The phone needs Tailscale on to reach the budget service.
   Tap **Test settings** to see that the service, SnapShot and Exceptions all work.

## Finding the service

The upload page and these jobs use the same address; the jobs read `/api/transactions` there. The service only
answers through Tailscale: the Tailscale Service `https://budget.<tailnet>.ts.net`, or Tailscale Serve on the
workstation's own name. What can be fixed is fixed:

* **Save** in settings adds a missing `https://` and drops a pasted path (`/upload`, `/api/...`).
* **Test settings** checks what's entered, without saving: the budget service (✓ with its accounts and how far
  the bank data goes) and the budget sheet (✓ with the SnapShot tab's expenses), or what's wrong and why. An
  address it can correct is filled in.
* **Save** runs the same test first. Settings that pass are saved; ones that don't are listed with the reason
  and only saved after **Save anyway**, so working settings aren't replaced by broken ones by accident.
* When the job opens and the saved address doesn't answer, it finds the one that does and **saves it** in
  place of the old one (the job says so). For the workstation's Tailscale IP, short name or the service's own
  port (`http://100.83.88.59:8092`), the phone asks Tailscale's DNS for the workstation's `*.ts.net` name, then
  tries `budget.<tailnet>.ts.net` and the workstation's name, on 443 and 8443. An old address on the
  workstation's own name moves to the `budget` service the same way.

What it can't fix, it says:

* the phone isn't on Tailscale: open the Tailscale app;
* the workstation runs an older budget service without the feed: run there
  `cd ~/develai/ScratchPatch && git checkout main && git pull && cd budget && docker compose up -d --build`;
* nothing answers: check `tailscale serve status` on the AI Workstation.
