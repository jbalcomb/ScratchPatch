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
