# Receipts

Six jobs in the **Receipts** category are thin screens over the receipts service on the AI Workstation
([receipts/API.md](../../../receipts/API.md)), reached over Tailscale. The service does the parsing, matching
and storage; the phone sends photos and shows lists. The design is in
[receipts/PRD.md](../../../receipts/PRD.md).

## Setup

1. On the workstation, start the service and expose it with Tailscale Serve, as in the Tailscale Serve step of
   [receipts/README.md](../../../receipts/README.md). Note the HTTPS address it prints.
2. On the phone, connect Tailscale.
3. Open **Receipt capture**, tap the **tune** icon and set **Receipts service URL** to that address, e.g.
   `https://ai-workstation.tail1234.ts.net:8443`. All six jobs use this one setting.

When a job opens, it checks `GET /api/health`. If the service doesn't answer, the job says so, and says when
the reason is that the phone isn't on Tailscale. Errors from the service are shown as the job's error.

## Jobs

| Job | What it does |
|---|---|
| **Receipt capture** | Pick one or more photos (.jpg, .jpeg, .png, .webp), in order from the top of the receipt to the bottom, and an optional note. **Send receipt** uploads them as one receipt. The result shows the receipt number, the photo count and its review page. Parsing follows on the workstation. |
| **Receipts to review** | Counts (to review, retake, waiting, approved) and every parsed receipt that needs a person: store, date, total, bucket and the fields to check. Receipts in the retake bucket say a new photo is needed. Review them on the `/review` page. |
| **Unmatched items** | Receipt lines not yet matched to a product, as "text (store, date, price) → suggestion". **Accept suggestions** matches every item whose suggestion scores 90% or more; the preview lists exactly those first. The service then matches the same text automatically, on other receipts too. Match the rest on the `/items` page. |
| **Product photo** | Pick one or more photos of one product. **Send photos** adds it to the catalog; the name, brand and size are read in the background. |
| **Shopping list** | Products that are due, with how many days overdue, the usual store, the last price and the best price and store. |
| **Reconciliation** | Optionally imports a bank CSV (Bank of America, or any CSV with Date, Description and Amount) and copies new transactions from the budget store, then matches receipts to bank charges and lists receipts with no bank charge and charges with no receipt. Match those by hand on the `/reconcile` page. |

Amounts come from the service in cents and are shown in dollars.
