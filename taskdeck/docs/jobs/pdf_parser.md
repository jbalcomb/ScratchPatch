# PDF Parser

The **PDF Parser** job (category **Tools**) sends a PDF from the phone to the PDF parser service on the AI
Workstation ([pdf-parser/README.md](../../../pdf-parser/README.md)), reached over Tailscale. The service does
the work: text with fonts and positions, tables as CSV, images with a quality check (heuristics, then the local
vision model, with retries), OCR of images and scanned pages, and a `manifest.json` describing all of it. The
phone only sends the file and shows the result.

## Setup

1. On the workstation, start the service and reach it over Tailscale at `https://pdf-parser.tailc54221.ts.net`, as in
   [pdf-parser/README.md](../../../pdf-parser/README.md) (*On the workstation*).
2. On the phone, connect Tailscale.
3. The job's setting **PDF parser service URL** defaults to `https://pdf-parser.tailc54221.ts.net`; change it
   with the **tune** icon if the service lives elsewhere.

When the job opens it checks `GET /api/health`. If the service doesn't answer, the job says so (and says when
the reason is that the phone isn't on Tailscale). If the vision model is unavailable, or Tesseract is missing,
it says so too, without blocking: PDFs are still parsed.

## Using it

**Browse** to a PDF and tap **Parse PDF**. The steps show the upload, then **wait for parsing**: the job asks
the service every 3 seconds, for up to 20 minutes (Cancel stops the wait; the service carries on). The result:

| Row | What |
|---|---|
| Pages | how many, and how many were scans (OCR'd) |
| Text blocks, Tables | what was found |
| Images kept / Needs review | images that passed the checks, and those that didn't after 3 tries |
| Also | duplicates, merged fragments and icons (not counted above) |
| Vision checks | how many images the vision model was asked about |
| Output folder | the bundle's page on the service: every file, `manifest.json`, and a link to the .zip |
| Download (.zip) | the whole bundle |

A warning says how many images need a look (`needs_review` in the manifest; the tries are in
`images/attempts/`) and when the vision model wasn't available for this PDF. If parsing takes longer than the
wait, the result says it's still parsing and links the output folder, which fills in when it's done.
