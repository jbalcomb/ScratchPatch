#include "store.h"

#include <windows.h>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <sstream>

using nlohmann::json;
namespace fs = std::filesystem;

namespace {
std::wstring env(const wchar_t* name) {
    wchar_t buf[MAX_PATH * 2];
    const DWORD n = ::GetEnvironmentVariableW(name, buf, static_cast<DWORD>(std::size(buf)));
    return (n > 0 && n < std::size(buf)) ? std::wstring(buf, n) : std::wstring();
}

// The first `n` characters (code points, as Python counts them) of UTF-8 text.
std::string first_chars(const std::string& s, size_t n) {
    size_t i = 0, count = 0;
    while (i < s.size() && count < n) {
        const unsigned char c = static_cast<unsigned char>(s[i]);
        i += c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
        ++count;
    }
    return s.substr(0, std::min(i, s.size()));
}

const json kNull;
}  // namespace

fs::path Store::default_path() {
    const std::wstring dir = env(L"TASKDECK_DATA_DIR");
    if (!dir.empty()) return fs::path(dir) / L"state.json";
    return fs::path(env(L"LOCALAPPDATA")) / L"TaskDeck" / L"state.json";
}

void Store::open(const fs::path& path) {
    path_ = path;
    data_ = json::object();
    std::ifstream in(path, std::ios::binary);
    if (!in) return;
    std::stringstream text;
    text << in.rdbuf();
    json parsed = json::parse(text.str(), nullptr, /*allow_exceptions=*/false);
    if (parsed.is_object()) data_ = std::move(parsed);
}

const json& Store::get(const std::string& key) const {
    const auto it = data_.find(key);
    return it == data_.end() ? kNull : *it;
}

void Store::set(const std::string& key, json value) {
    if (auto it = data_.find(key); it != data_.end() && *it == value) return;
    data_[key] = std::move(value);
    flush();
}

void Store::flush() {
    error_.clear();
    if (path_.empty()) return;
    std::error_code ec;
    fs::create_directories(path_.parent_path(), ec);
    const fs::path tmp = path_.parent_path() / (L".tmp-" + path_.filename().wstring());
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        out << data_.dump(2, ' ', false, json::error_handler_t::replace);
        out.close();
        if (!out) {
            error_ = "Couldn't write " + tmp.string();
            return;
        }
    }
    if (!::MoveFileExW(tmp.c_str(), path_.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        error_ = "Couldn't replace " + path_.string() + " (error " + std::to_string(::GetLastError()) + ")";
}

json plain_values(const json& values) {
    json out = json::object();
    if (!values.is_object()) return out;
    for (const auto& [k, v] : values.items())
        if (v.is_string() || v.is_number() || v.is_boolean() || v.is_null()) out[k] = v;
    return out;
}

// ---- pins ------------------------------------------------------------------------------------------------

std::vector<std::string> Pins::ids(const Store& s) {
    std::vector<std::string> out;
    const json& v = s.get("pinned");
    if (v.is_array())
        for (const auto& id : v)
            if (id.is_string()) out.push_back(id.get<std::string>());
    return out;
}

bool Pins::has(const Store& s, const std::string& id) {
    const auto ids = Pins::ids(s);
    return std::find(ids.begin(), ids.end(), id) != ids.end();
}

void Pins::toggle(Store& s, const std::string& id) {
    auto ids = Pins::ids(s);
    if (auto it = std::find(ids.begin(), ids.end(), id); it != ids.end()) ids.erase(it);
    else ids.push_back(id);
    s.set("pinned", ids);
}

void Pins::move(Store& s, const std::string& id, int new_index) {
    auto ids = Pins::ids(s);
    auto it = std::find(ids.begin(), ids.end(), id);
    if (it == ids.end()) return;
    ids.erase(it);
    const int at = std::clamp(new_index, 0, static_cast<int>(ids.size()));
    ids.insert(ids.begin() + at, id);
    s.set("pinned", ids);
}

// ---- history ---------------------------------------------------------------------------------------------

double History::add(Store& s, const std::string& job_id, bool ok, const std::string& summary, const json& inputs) {
    const double now = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    json entries = json::array();
    entries.push_back({{"job_id", job_id}, {"at", now}, {"ok", ok}, {"summary", first_chars(summary, 200)},
                       {"inputs", plain_values(inputs)}});
    const json& old = s.get("history");
    if (old.is_array())
        for (const auto& e : old) {
            if (entries.size() >= kLimit) break;
            if (e.is_object()) entries.push_back(e);
        }
    s.set("history", std::move(entries));
    return now;
}

void History::clear(Store& s) { s.set("history", json::array()); }

std::vector<std::string> History::recent_job_ids(const Store& s, size_t limit) {
    std::vector<std::string> seen;
    const json& entries = s.get("history");
    if (!entries.is_array()) return seen;
    for (const auto& e : entries) {
        if (seen.size() >= limit) break;
        const std::string id = e.value("job_id", "");
        if (!id.empty() && std::find(seen.begin(), seen.end(), id) == seen.end()) seen.push_back(id);
    }
    return seen;
}

// ---- drafts ----------------------------------------------------------------------------------------------

json Drafts::get(const Store& s, const std::string& job_id) {
    const json& all = s.get("drafts");
    if (all.is_object())
        if (auto it = all.find(job_id); it != all.end() && it->is_object()) return *it;
    return json::object();
}

void Drafts::save(Store& s, const std::string& job_id, const json& values) {
    json all = s.get("drafts").is_object() ? s.get("drafts") : json::object();
    all[job_id] = plain_values(values);
    s.set("drafts", std::move(all));  // unchanged: no write
}

void Drafts::clear(Store& s, const std::string& job_id) {
    if (!s.get("drafts").is_object() || !s.get("drafts").contains(job_id)) return;
    json all = s.get("drafts");
    all.erase(job_id);
    s.set("drafts", std::move(all));
}
