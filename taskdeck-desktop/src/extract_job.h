// Extract archive, without the UI: the phone app's job (taskdeck/src/taskdeck/jobs/extract_archive/logic.py) in
// C++ on 7-Zip (sevenzip.h). Scan an archive, decide and check the destination, extract with progress.
#pragma once
#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace extract {

struct Scan {
    std::string format;
    int files = 0, folders = 0;
    uint64_t unpacked = 0;       // bytes, 0 if not known
    bool encrypted = false;      // some files need a password
    bool names_hidden = false;   // encrypted names: nothing listed until the password is right
    bool compressed_tar = false; // a .tar inside a .gz/.bz2/.xz/.zst: decompressed first, then extracted
    std::string error;           // why it can't be extracted ("" if it can)
};

// Reads the archive's index (fast): format, counts, unpacked size, encryption, unsafe paths.
Scan scan(const std::filesystem::path& archive, const std::optional<std::string>& password);

// The folder name for an archive: its name without the archive extension ("site.tar.gz" -> "site").
std::string archive_stem(const std::string& name);

// Where it goes: `destination` itself if given, else a new folder next to the archive named after it.
std::filesystem::path destination(const std::filesystem::path& archive, const std::string& destination);

// What would stop the extraction, checked before starting (empty = go).
std::vector<std::string> problems(const Scan& s, const std::filesystem::path& dest, const std::string& password);

// Where the work reports to: phases (label, total, unit "bytes"), progress, and whether to stop.
struct Reporter {
    std::function<void(const std::vector<std::pair<std::string, uint64_t>>&)> set_phases;
    std::function<void(int phase, uint64_t done, uint64_t total)> advance;
    std::function<bool()> cancelled;
};

struct Outcome {
    enum Status { Done, Failed, Cancelled } status = Failed;
    std::string message;
};

// Extracts everything into dest (created if needed), replacing existing files.
Outcome run(const std::filesystem::path& archive, const std::filesystem::path& dest,
            const std::optional<std::string>& password, const Reporter& reporter);

// "1.5 MB", "820 bytes" (the phone's files.human_size); "1,234".
std::string human_size(double bytes);
std::string thousands(uint64_t n);
std::string utf8(const std::filesystem::path& p);
std::filesystem::path path_from_utf8(const std::string& s);

}  // namespace extract
