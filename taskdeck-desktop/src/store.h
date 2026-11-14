// What TaskDeck keeps on this PC between runs, in one JSON file, the same layout as the phone app's
// state.json (taskdeck/src/taskdeck/core/storage.py, favorites.py): "pinned", "history", "drafts",
// "command_usage" and "settings:<job id>". Secret settings aren't in it: they're in Credential Manager
// (credentials.h).
#pragma once
#include <nlohmann/json.hpp>

#include <filesystem>
#include <string>
#include <vector>

class Store {
public:
    // Reads `path` (a missing or broken file reads as empty). Writes go to the same file.
    void open(const std::filesystem::path& path);
    const std::filesystem::path& path() const { return path_; }
    // Why the last write failed ("" if it worked).
    const std::string& error() const { return error_; }

    const nlohmann::json& get(const std::string& key) const;  // null if missing
    // Sets the key and writes the file (atomically: a temporary file, then a rename). Unchanged = no write.
    void set(const std::string& key, nlohmann::json value);

    // %LOCALAPPDATA%\TaskDeck\state.json, or TASKDECK_DATA_DIR\state.json when that is set (tests).
    static std::filesystem::path default_path();

private:
    void flush();

    std::filesystem::path path_;
    nlohmann::json data_ = nlohmann::json::object();
    std::string error_;
};

// Pinned tiles on Home: an ordered list of job ids (favorites.py, Favorites).
struct Pins {
    static std::vector<std::string> ids(const Store& s);
    static bool has(const Store& s, const std::string& id);
    static void toggle(Store& s, const std::string& id);
    static void move(Store& s, const std::string& id, int new_index);
};

// Recent runs, newest first, at most 100 (favorites.py, History). Entries keep the phone's fields:
// {"job_id", "at" (Unix seconds), "ok", "summary" (200 chars at most), "inputs" (plain values only)}.
struct History {
    static constexpr size_t kLimit = 100;
    // Returns the entry's time ("at").
    static double add(Store& s, const std::string& job_id, bool ok, const std::string& summary,
                    const nlohmann::json& inputs);
    static void clear(Store& s);
    static std::vector<std::string> recent_job_ids(const Store& s, size_t limit = 6);
};

// Unfinished inputs per job, so nothing typed is lost to leaving the screen or closing the app.
struct Drafts {
    static nlohmann::json get(const Store& s, const std::string& job_id);  // {} if none
    static void save(Store& s, const std::string& job_id, const nlohmann::json& values);
    static void clear(Store& s, const std::string& job_id);
};

// Keeps only strings, numbers, booleans and nulls (what history and drafts may hold).
nlohmann::json plain_values(const nlohmann::json& values);
