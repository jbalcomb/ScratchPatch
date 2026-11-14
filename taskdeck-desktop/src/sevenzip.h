// 7-Zip's archive library (7z.dll, built from 7-Zip's source next to TaskDeck.exe) as a small C++ API: open
// an archive (with a password), list it, extract it with progress and cancel. The same engine, error codes and
// safety rules as the phone app's (taskdeck/native/sevenzip/sz_shim.cpp, taskdeck/src/taskdeck/shared/archive.py).
#pragma once
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace sevenzip {

enum Code {
    OK = 0,
    E_LOAD,              // 7z.dll not found or not loadable
    E_OPEN_FILE,         // can't read the archive file
    E_UNSUPPORTED,       // not an archive 7-Zip can open
    E_NEED_PASSWORD,
    E_WRONG_PASSWORD,
    E_DATA,              // damaged archive, CRC error
    E_CANCELLED,
    E_WRITE,             // can't create a folder or file (permission, disk full)
    E_UNSAFE_PATH,       // an entry would land outside the target folder
    E_UNSUPPORTED_METHOD,
    E_OTHER,
};

// What to tell the user, as the phone says it (with the entry's name where it helps).
std::string message(int code, const std::string& entry = {});

// Loads 7z.dll once. False (and why, in `error`) if it can't.
bool load(const std::wstring& dll_path, std::string* error);
bool loaded();
std::string version();  // of the 7-Zip source this was built against

struct Entry {
    std::string name;  // UTF-8, as stored ("docs/readme.txt")
    uint64_t size = 0;
    bool dir = false, encrypted = false;
};

// (done bytes, total bytes) -> true to cancel.
using Progress = std::function<bool(uint64_t, uint64_t)>;

// Where an entry lands under the target folder, as a relative path with '/' separators: leading slashes, drive
// letters and "." dropped ("" for "." itself, the target folder). False if it would escape ("..").
bool safe_relative(const std::string& name, std::string* out);

class Archive {
public:
    Archive();
    ~Archive();
    Archive(const Archive&) = delete;
    Archive& operator=(const Archive&) = delete;

    // Opens `path` (the format from its extension, else its signature). A password is needed for archives with
    // encrypted names (7z, RAR); for encrypted data it can also be given to extract().
    int open(const std::wstring& path, const std::optional<std::string>& password);
    const std::string& format() const;
    std::vector<Entry> entries() const;
    // Entries whose path would escape the target folder (checked before extracting).
    std::vector<std::string> unsafe_entries() const;
    // Extracts everything into `dir` (created if needed), replacing existing files. Nothing is written if an
    // entry's path is unsafe. On failure, `failed_entry` is the entry it happened on.
    int extract(const std::wstring& dir, const std::optional<std::string>& password, const Progress& progress,
                std::string* failed_entry);
    // Extracts the first entry into the file `target` (a .tar inside a .gz, say).
    int extract_first(const std::wstring& target, const Progress& progress, std::string* failed_entry);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace sevenzip
