// Tests for the C++ core that has no window: the palette ranking (the same cases as the phone app's
// taskdeck/tests/test_commands.py), the state file (pins, history, drafts) and Credential Manager.
// Run: build\Release\core_tests.exe (exit code 0 = all passed). CI runs it too.
#include "../src/commands.h"
#include "../src/credentials.h"
#include "../src/store.h"

#include <windows.h>

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <set>
#include <string>

namespace fs = std::filesystem;
using nlohmann::json;

static int g_failed = 0, g_checks = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        ++g_checks;                                                              \
        if (!(cond)) {                                                           \
            ++g_failed;                                                          \
            std::printf("FAILED %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
        }                                                                        \
    } while (0)

static std::vector<Command> registry() {
    std::vector<Command> r;
    r.push_back({"job:unit", "Unit Converter", "", "", CommandKind::Job, {"miles", "km"}});
    r.push_back({"job:case", "Case Converter", "", "", CommandKind::Job, {}});
    r.push_back({"nav:settings", "Go to Settings", "", "", CommandKind::Action, {}});
    r.push_back({"theme:dark", "Theme: Dark", "", "", CommandKind::Action, {"appearance"}});
    Command hidden{"hidden", "Unpin current job", "", "", CommandKind::Action, {}};
    hidden.applicable = false;
    r.push_back(hidden);
    return r;
}

static std::vector<std::string> titles(const std::vector<Match>& ms) {
    std::vector<std::string> out;
    for (const auto& m : ms) out.push_back(m.command->title);
    return out;
}

static void test_commands() {
    CHECK(fuzzy_score("uc", "Unit Converter").has_value());
    CHECK(fuzzy_score("ct", "Unit Converter").has_value());
    CHECK(!fuzzy_score("zz", "Unit Converter").has_value());

    CHECK(fuzzy_score("uc", "Unit Converter")->score > fuzzy_score("uc", "Abduct")->score);  // word starts win

    const std::string title = "Go to Settings";
    std::string picked;
    const auto hit = fuzzy_score("set", title);
    for (int i : hit->positions) picked += title[i];
    CHECK(picked == "Set");

    const auto r = registry();
    CHECK(titles(search_commands(r, "uconv")).at(0) == "Unit Converter");
    auto t = titles(search_commands(r, ">"));
    CHECK((std::set<std::string>(t.begin(), t.end()) == std::set<std::string>{"Go to Settings", "Theme: Dark"}));
    t = titles(search_commands(r, "@conv"));
    CHECK((std::set<std::string>(t.begin(), t.end()) == std::set<std::string>{"Unit Converter", "Case Converter"}));

    CHECK(titles(search_commands(r, "miles")) == std::vector<std::string>{"Unit Converter"});
    CHECK(titles(search_commands(r, "appear")) == std::vector<std::string>{"Theme: Dark"});

    CHECK(titles(search_commands(r, "verter")).at(0) == "Case Converter");  // alphabetical tie-break
    CHECK(titles(search_commands(r, "verter", {{"job:unit", 5}})).at(0) == "Unit Converter");

    t = titles(search_commands(r, "unpin"));
    CHECK(std::find(t.begin(), t.end(), "Unpin current job") == t.end());
}

static void test_store() {
    wchar_t tmp[MAX_PATH];
    ::GetTempPathW(MAX_PATH, tmp);
    const fs::path dir = fs::path(tmp) / (L"taskdeck-core-tests-" + std::to_wstring(::GetCurrentProcessId()));
    fs::create_directories(dir);
    const fs::path file = dir / L"state.json";

    {
        Store s;
        s.open(file);  // missing: empty
        CHECK(s.get("pinned").is_null());
        s.set("pinned", json::array({"a", "b"}));
        Pins::toggle(s, "c");
        Pins::toggle(s, "a");
        CHECK((Pins::ids(s) == std::vector<std::string>{"b", "c"}));
        Pins::move(s, "c", 0);
        CHECK((Pins::ids(s) == std::vector<std::string>{"c", "b"}));
        Pins::move(s, "b", 99);
        CHECK((Pins::ids(s) == std::vector<std::string>{"c", "b"}));

        for (int i = 0; i < 105; ++i)
            History::add(s, i % 2 ? "odd" : "even", true, std::string(300, 'x'),
                         {{"text", "héllo"}, {"n", i}, {"list", json::array({1})}});
        CHECK(s.get("history").size() == History::kLimit);
        CHECK(s.get("history")[0]["summary"].get<std::string>().size() == 200);
        CHECK(s.get("history")[0]["inputs"].contains("text"));
        CHECK(!s.get("history")[0]["inputs"].contains("list"));  // only plain values
        CHECK((History::recent_job_ids(s) == std::vector<std::string>{"even", "odd"}));

        Drafts::save(s, "job", {{"text", "draft"}, {"flag", true}});
        CHECK(Drafts::get(s, "job")["text"] == "draft");
        CHECK(Drafts::get(s, "other").empty());
        CHECK(s.error().empty());
    }
    {
        Store s;  // read back what was written
        s.open(file);
        CHECK((Pins::ids(s) == std::vector<std::string>{"c", "b"}));
        CHECK(s.get("history")[0]["inputs"]["text"] == "héllo");
        CHECK(Drafts::get(s, "job")["flag"] == true);
        Drafts::clear(s, "job");
        CHECK(Drafts::get(s, "job").empty());
    }
    {
        std::FILE* f = nullptr;
        _wfopen_s(&f, file.c_str(), L"wb");  // a broken file reads as empty, not a crash
        std::fputs("{not json", f);
        std::fclose(f);
        Store s;
        s.open(file);
        CHECK(s.get("pinned").is_null());
    }
    std::error_code ec;
    fs::remove_all(dir, ec);
}

static void test_credentials() {
    const std::wstring target = L"TaskDeck-tests/" + std::to_wstring(::GetCurrentProcessId());
    credentials::remove(target);
    CHECK(!credentials::read(target).has_value());

    std::string error;
    CHECK(credentials::write(target, "s3cret é", &error));
    CHECK(error.empty());
    CHECK(credentials::read(target).value_or("") == "s3cret é");

    std::string big;  // a Google service-account key can be over the 2,560-byte limit: stored in parts
    for (int i = 0; big.size() < 6000; ++i) big += std::to_string(i) + ",";
    CHECK(credentials::write(target, big, &error));
    CHECK(credentials::read(target).value_or("") == big);

    CHECK(credentials::write(target, "short", &error));  // the parts left over from the long one go
    CHECK(credentials::read(target).value_or("") == "short");

    credentials::remove(target);
    CHECK(!credentials::read(target).has_value());
    CHECK(credentials::target("daily_log", "llm_api_key") == L"TaskDeck/settings:daily_log:llm_api_key");
}

int main() {
    test_commands();
    test_store();
    test_credentials();
    std::printf("%d checks, %d failed\n", g_checks, g_failed);
    return g_failed ? 1 : 0;
}
