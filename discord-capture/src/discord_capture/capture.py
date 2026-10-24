"""Discovery, historic backfill and incremental sync.

A channel's progress lives in `sync_state`: `newest_id` is where forward sync
resumes, `oldest_id` is where backfill resumes. Each page is committed as it
arrives, so an interrupted run picks up where it stopped.
"""

import logging

from .client import Forbidden, NotFound

log = logging.getLogger(__name__)

PAGE = 100  # Discord's maximum for GET /channels/{id}/messages

# Channel types: 0 text, 2 voice (text-in-voice), 5 announcement, 13 stage,
# 10/11/12 threads. 4 is a category, 15 forum and 16 media only hold threads.
MESSAGE_TYPES = {0, 2, 5, 13, 10, 11, 12}
THREAD_PARENT_TYPES = {0, 5, 15, 16}


def discover(client, store, guild_ids=None):
    """Record the servers, channels and threads the bot can see; return the
    message-bearing channels as (guild_id, channel) pairs."""
    found = []
    for guild in client.get("/users/@me/guilds"):
        if guild_ids and guild["id"] not in guild_ids:
            continue
        store.upsert_guild(guild)
        gid = guild["id"]
        channels = client.get(f"/guilds/{gid}/channels")
        threads = client.get(f"/guilds/{gid}/threads/active").get("threads", [])
        for parent in channels:
            if parent["type"] in THREAD_PARENT_TYPES:
                threads += _archived_threads(client, parent)
        for channel in channels + threads:
            channel.setdefault("guild_id", gid)
            store.upsert_channel(channel)
            if channel["type"] in MESSAGE_TYPES:
                found.append((gid, channel))
        store.commit()
    return found


def _archived_threads(client, parent):
    threads, before = [], None
    while True:
        try:
            page = client.get(
                f"/channels/{parent['id']}/threads/archived/public", {"limit": PAGE, "before": before}
            )
        except (Forbidden, NotFound):
            return threads
        threads += page.get("threads", [])
        if not page.get("has_more") or not page.get("threads"):
            return threads
        before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]


def capture_channel(client, store, guild_id, channel_id, *, backfill=True, max_pages=None):
    """Fetch what's new in a channel and, if asked, continue its backfill.
    Returns the number of messages saved."""
    cid = int(channel_id)
    try:
        saved = _forward(client, store, guild_id, cid)
        if backfill:
            saved += _backward(client, store, guild_id, cid, max_pages)
    except (Forbidden, NotFound) as error:
        store.set_state(cid, last_error=str(error))
        store.commit()
        log.info("skipping channel %s: %s", cid, error)
        return 0
    store.set_state(cid, last_error=None)
    store.commit()
    return saved


def _forward(client, store, guild_id, cid):
    state = store.state(cid)
    if state["newest_id"] is None:
        # First visit: the newest page anchors both directions.
        page = client.get(f"/channels/{cid}/messages", {"limit": PAGE})
        _save(store, guild_id, cid, page)
        if page:
            ids = [int(m["id"]) for m in page]
            store.set_state(cid, newest_id=max(ids), oldest_id=min(ids), backfill_done=int(len(page) < PAGE))
        else:
            store.set_state(cid, backfill_done=1)
        store.commit()
        return len(page)

    saved, after = 0, state["newest_id"]
    while True:
        page = client.get(f"/channels/{cid}/messages", {"limit": PAGE, "after": after})
        if not page:
            return saved
        _save(store, guild_id, cid, page)
        after = max(int(m["id"]) for m in page)
        store.set_state(cid, newest_id=after)
        store.commit()
        saved += len(page)
        if len(page) < PAGE:
            return saved


def _backward(client, store, guild_id, cid, max_pages):
    saved, pages = 0, 0
    while True:
        state = store.state(cid)
        if state["backfill_done"] or state["oldest_id"] is None:
            return saved
        if max_pages is not None and pages >= max_pages:
            return saved
        page = client.get(f"/channels/{cid}/messages", {"limit": PAGE, "before": state["oldest_id"]})
        _save(store, guild_id, cid, page)
        oldest = min((int(m["id"]) for m in page), default=state["oldest_id"])
        store.set_state(cid, oldest_id=oldest, backfill_done=int(len(page) < PAGE))
        store.commit()
        saved += len(page)
        pages += 1


def _save(store, guild_id, cid, page):
    if page:
        store.save_messages(guild_id, page)
        log.info("channel %s: saved %d messages", cid, len(page))
