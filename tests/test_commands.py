from taskdeck.core import Command, CommandKind, CommandRegistry, fuzzy_score


def noop():
    pass


def reg():
    r = CommandRegistry()
    r.add(Command("job:unit", "Unit Converter", noop, CommandKind.JOB, keywords=("miles", "km")))
    r.add(Command("job:case", "Case Converter", noop, CommandKind.JOB))
    r.add(Command("nav:settings", "Go to Settings", noop))
    r.add(Command("theme:dark", "Theme: Dark", noop, keywords=("appearance",)))
    r.add(Command("hidden", "Unpin current job", noop, when=lambda: False))
    return r


def titles(matches):
    return [m.command.title for m in matches]


def test_fuzzy_requires_in_order_subsequence():
    assert fuzzy_score("uc", "Unit Converter") is not None
    assert fuzzy_score("ct", "Unit Converter") is not None
    assert fuzzy_score("zz", "Unit Converter") is None


def test_word_start_beats_mid_word():
    starts, _ = fuzzy_score("uc", "Unit Converter")
    middle, _ = fuzzy_score("uc", "Abduct")
    assert starts > middle


def test_positions_point_at_matched_characters():
    _, pos = fuzzy_score("set", "Go to Settings")
    assert [("Go to Settings")[i] for i in pos] == list("Set")


def test_search_ranks_and_filters_by_prefix():
    r = reg()
    assert titles(r.search("uconv"))[0] == "Unit Converter"
    assert set(titles(r.search(">"))) == {"Go to Settings", "Theme: Dark"}
    assert set(titles(r.search("@conv"))) == {"Unit Converter", "Case Converter"}


def test_keywords_match_without_fuzzy_noise():
    r = reg()
    assert titles(r.search("miles")) == ["Unit Converter"]
    assert titles(r.search("appear")) == ["Theme: Dark"]


def test_usage_boost_reorders_ties():
    r = reg()
    assert titles(r.search("verter"))[0] == "Case Converter"  # alphabetical tie-break
    assert titles(r.search("verter", usage={"job:unit": 5}))[0] == "Unit Converter"


def test_inapplicable_commands_hidden():
    assert "Unpin current job" not in titles(reg().search("unpin"))
