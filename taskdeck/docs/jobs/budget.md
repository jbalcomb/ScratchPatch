# Budget jobs

Two jobs, in the **Money** category, that bring together:

* the budget sheet's **SnapShot** tab: the list of every expense (you edit it in Google Sheets), and
* the **bank transactions** of the three Bank of America accounts, which the budget service on the AI Workstation
  collects from uploaded exports ([budget/README.md](../../../budget/README.md)).

| Job | Run | Write to sheet |
|---|---|---|
| **Reconcile budget month** | A month (blank = the latest with bank data) against SnapShot | the **Reconcile (auto)** tab |
| **Update budget timeline** | This month's transactions, then what's coming through the end of next month | the **Timeline (auto)** tab |

Running only reads. **Write to sheet** replaces the job's own tab (created the first time); no other tab is
touched, so the hand-made TimeLine and Projected tabs stay as they are.

## How a transaction is tied to a budget line

By name. A SnapShot **Expense** is also a **category** in the budget service's `budget/rules.txt`:
`EXETERFINA = Car Payment` makes the Exeter Finance loan payment the "Car Payment" line. To add a line:
add the row to SnapShot, then a rule with exactly the same name, then `categorize` on the workstation
(or upload anything). A rule can be limited to one account, e.g. `[bills] Monthly Maintenance Fee = BofA Fee - Bills`.

SnapShot columns used: **Expense**, **Periodicity**, **Day of Period**, **Monthly** (found by their header,
in the row whose first cell is "Expense").

| Row looks like | Treated as |
|---|---|
| Periodicity *Monthly* with a Monthly amount | a **bill**: expected once a month, about that amount, around that day |
| Periodicity *Annually* | **yearly**: only reported when it's charged |
| no Periodicity (Groceries, Motor Fuel) | **spending**: the Monthly column is its target (optional) |

## Reconcile budget month

* **Bills**: each one *paid* (date, amount), *paid +$X vs the budget* (amount changed by more than 2% or $1),
  *due 10/25* (not past its day + 3 days in the bank data yet), or **missing**. A line never seen in the bank
  at all (rent) shows as *paid in cash?*. When a category has two charges (Claude's $106.75 and $21.35), the one
  closest to the budget amount is the bill and the other is listed below as an extra charge.
* **Spending**: spent against the Monthly target.
* **Not on SnapShot**: every other expense, grouped by category (or by merchant when no rule matches). *Repeats*
  means it was also charged in 2 of the 3 months before: a subscription that belongs on SnapShot, or should be
  cancelled. Transfers between the three accounts and income are left out.
* **Income**: totals per income category.

## Update budget timeline

* **Actual** rows: this month's transactions, with each account's balance as the bank reported it.
* **Projected** rows, from the day after the bank data ends to the end of next month:
  * bills on their day, from the account that paid them last, unless already paid that month;
  * spending as a weekly share (Mondays) of its Monthly target, or of the last three months' average;
  * income that arrived in at least 3 of the last 4 weeks, weekly on the same weekday at its average;
  * lines paid in cash (rent) are listed but move no bank balance.
* The result says each account's lowest point, and for one that goes negative, the day and how much to move
  in before then.

## One-time setup

1. **Share the budget sheet** with the service account's `client_email` (inside its `.json` key: the same one
   Daily Log uses) as **Editor**.
2. On the phone, open either job → settings (tune icon):

   | Setting | Value |
   |---|---|
   | Budget service address | the Tailscale Serve address of the budget service, e.g. `https://ai-workstation.tail1234.ts.net` |
   | Budget sheet ID | pre-filled with the Budget-2026-TimeLine sheet |
   | Budget list tab | `SnapShot` |
   | Google service account key | Import the same `.json` as Daily Log |

   The two jobs share these settings. The phone needs Tailscale on to reach the budget service.

## Finding the service

The upload page and these jobs use the same address; the jobs read `/api/transactions` there. The service only
answers through Tailscale Serve (`https://<machine>.<tailnet>.ts.net`), so when the address in settings doesn't
answer, the job works out the right one by itself and says which it used:

* no `https://`, or a pasted path (`/upload`, `/api/...`): fixed;
* the workstation's Tailscale IP or short name, or the service's own port (`http://100.83.88.59:8092`): the
  phone asks Tailscale's DNS for the workstation's `*.ts.net` name and tries it on 443 and 8443.

What it can't fix, it says:

* the phone isn't on Tailscale: open the Tailscale app;
* the workstation runs an older budget service without the feed: run there
  `cd ~/ScratchPatch && git checkout main && git pull && cd budget && docker compose up -d --build`;
* nothing answers: check `tailscale serve status` on the AI Workstation.
