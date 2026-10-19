from taskdeck.core import Favorites, History, JsonStore


def test_pin_move_unpin_and_persist(tmp_path):
    store = JsonStore(tmp_path / "s.json")
    fav = Favorites(store)
    for i in "abc":
        fav.pin(i)
    fav.pin("a")  # idempotent
    assert fav.ids == ["a", "b", "c"]
    fav.move("c", 0)
    assert fav.ids == ["c", "a", "b"]
    fav.shift("c", 1)
    assert fav.ids == ["a", "c", "b"]
    fav.shift("b", 5)  # clamps
    assert fav.ids == ["a", "c", "b"]
    assert fav.toggle("a") is False
    fav.prune({"b"})
    assert Favorites(JsonStore(tmp_path / "s.json")).ids == ["b"]


def test_history_recent_and_limit(tmp_path):
    h = History(JsonStore(tmp_path / "s.json"))
    h.LIMIT = 3
    for job in ["x", "y", "x", "z"]:
        h.add(job, True, "ok", {"v": 1, "obj": object()})
    assert [e.job_id for e in h.entries()] == ["z", "x", "y"]
    assert h.recent_job_ids() == ["z", "x", "y"]
    assert h.entries()[0].inputs == {"v": 1}  # non-JSON values dropped
    assert h.usage_counts() == {"z": 1, "x": 1, "y": 1}


def test_corrupt_store_starts_empty(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert JsonStore(p).get("pinned", []) == []


def test_drafts_survive_reload_and_clear(tmp_path):
    from taskdeck.core import Drafts

    path = tmp_path / "s.json"
    Drafts(JsonStore(path)).save("daily_log", {"log": "Friday October 16th\nStart time 10:38", "obj": object()})
    drafts = Drafts(JsonStore(path))  # e.g. app restarted
    assert drafts.get("daily_log") == {"log": "Friday October 16th\nStart time 10:38"}
    assert drafts.get("other_job") == {}
    drafts.clear("daily_log")
    assert Drafts(JsonStore(path)).get("daily_log") == {}
