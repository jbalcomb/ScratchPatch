"""Photo OCR: thin screens over the photo OCR service on the AI Workstation (photo-ocr/API.md), reached over
Tailscale. Capture, review queue, unmatched items, product photos, shopping list, reconciliation, handwritten
notes and cork boards share one setting, the service URL (stored under Receipt capture).
See docs/jobs/photo_ocr.md."""

from . import items, lists, notes, photos, reconcile

PLUGINS = (photos.CAPTURE, lists.REVIEW, items.PLUGIN, photos.PRODUCT, lists.SHOPPING, reconcile.PLUGIN,
           notes.NOTE, notes.BOARD)
