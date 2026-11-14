#include "extract_job.h"

#include "sevenzip.h"

#include <windows.h>

#include <algorithm>
#include <cctype>
#include <cstdio>

namespace fs = std::filesystem;

namespace extract {

namespace {
std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

bool ends_with(const std::string& s, const std::string& end) {
    return s.size() >= end.size() && s.compare(s.size() - end.size(), end.size(), end) == 0;
}

// The phone's files.ARCHIVE_EXTENSIONS, two-part extensions first.
const char* const kArchiveExtensions[] = {
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz", ".tbz2", ".txz",
    ".gz", ".bz2", ".xz", ".zst", ".lzma", ".cab", ".iso", ".jar", ".apk", ".zipx", ".001"};

// Compressors 7-Zip opens as a "one-file archive": with a .tar inside, the tar is extracted in a second pass.
bool is_compressor(const std::string& format) {
    const std::string f = lower(format);
    return f == "gzip" || f == "bzip2" || f == "xz" || f == "zstd" || f == "lzma" || f == "z";
}

bool tar_inside(const std::string& archive_name, const std::vector<sevenzip::Entry>& entries) {
    if (entries.size() != 1) return false;
    if (ends_with(lower(entries[0].name), ".tar")) return true;
    const std::string n = lower(archive_name);
    for (const char* ext : {".tgz", ".tbz", ".tbz2", ".txz", ".tzst", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"})
        if (ends_with(n, ext)) return true;
    return false;
}

std::optional<std::string> pw(const std::string& password) {
    return password.empty() ? std::nullopt : std::optional<std::string>(password);
}

// Can TaskDeck create files in `folder`? (Tried, as Windows permissions can't be read off simply.)
bool writable(const fs::path& folder) {
    const fs::path probe = folder / L".taskdeck-write-test";
    HANDLE h = ::CreateFileW(probe.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                             FILE_ATTRIBUTE_TEMPORARY | FILE_ATTRIBUTE_HIDDEN | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
    if (h == INVALID_HANDLE_VALUE) return ::GetLastError() == ERROR_FILE_EXISTS;
    ::CloseHandle(h);
    return true;
}

std::string files_text(uint64_t n) { return thousands(n) + (n == 1 ? " file" : " files"); }
}  // namespace

std::string utf8(const fs::path& p) {
    const std::wstring w = p.wstring();
    if (w.empty()) return {};
    const int n = ::WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), s.data(), n, nullptr, nullptr);
    return s;
}

fs::path path_from_utf8(const std::string& s) {
    if (s.empty()) return {};
    const int n = ::MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(n, L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
    return fs::path(w);
}

std::string human_size(double n) {
    const char* units[] = {"bytes", "KB", "MB", "GB", "TB"};
    int u = 0;
    while (n >= 1024 && u < 4) { n /= 1024; ++u; }
    if (u == 0) return thousands(static_cast<uint64_t>(n)) + " bytes";
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.1f %s", n, units[u]);
    return buf;
}

std::string thousands(uint64_t n) {
    std::string s = std::to_string(n);
    for (int i = static_cast<int>(s.size()) - 3; i > 0; i -= 3) s.insert(static_cast<size_t>(i), ",");
    return s;
}

std::string archive_stem(const std::string& name) {
    const std::string low = lower(name);
    for (const char* ext : kArchiveExtensions) {
        const std::string e = ext;
        if (ends_with(low, e) && name.size() > e.size()) {
            std::string stem = name.substr(0, name.size() - e.size());
            if (ends_with(lower(stem), ".tar")) stem.resize(stem.size() - 4);  // "x.tar" left by ".gz"
            return stem;
        }
    }
    const std::string stem = utf8(path_from_utf8(name).stem());
    return stem.empty() ? name : stem;
}

fs::path destination(const fs::path& archive, const std::string& dest) {
    if (!dest.empty()) return path_from_utf8(dest);
    return archive.parent_path() / path_from_utf8(archive_stem(utf8(archive.filename())));
}

Scan scan(const fs::path& path, const std::optional<std::string>& password) {
    Scan result;
    sevenzip::Archive a;
    const int code = a.open(path.wstring(), password);
    if (code == sevenzip::E_NEED_PASSWORD) {
        result.names_hidden = result.encrypted = true;
        return result;
    }
    if (code == sevenzip::E_WRONG_PASSWORD) {
        result.names_hidden = result.encrypted = true;
        result.error = sevenzip::message(code);
        return result;
    }
    if (code != sevenzip::OK) {
        result.error = sevenzip::message(code);
        return result;
    }
    result.format = a.format();
    const auto entries = a.entries();
    if (is_compressor(result.format) && tar_inside(utf8(path.filename()), entries)) {
        result.compressed_tar = true;
        result.format = "tar." + lower(result.format);
        result.unpacked = entries[0].size;  // the tar: its files are counted when it's opened
        return result;
    }
    for (const auto& e : entries) {
        if (e.dir) {
            ++result.folders;
            continue;
        }
        ++result.files;
        result.unpacked += e.size;
        result.encrypted |= e.encrypted;
    }
    const auto unsafe = a.unsafe_entries();
    if (!unsafe.empty())
        result.error = "This archive contains a path that points outside the folder it's extracted to (“" +
                       unsafe[0] + "”). TaskDeck won't extract it.";
    return result;
}

std::vector<std::string> problems(const Scan& s, const fs::path& dest, const std::string& password) {
    std::vector<std::string> out;
    if (!s.error.empty()) out.push_back(s.error);
    if (s.encrypted && password.empty()) out.push_back("Enter the archive's password.");
    std::error_code ec;
    fs::path existing;  // the destination if it exists, else its parent (the destination is created)
    if (fs::exists(dest, ec)) {
        if (!fs::is_directory(dest, ec)) {
            out.push_back(utf8(dest) + " is a file, not a folder. Choose another.");
            return out;
        }
        existing = dest;
    } else {
        existing = dest.parent_path();
        if (existing.empty() || !fs::is_directory(existing, ec)) {
            out.push_back("The folder " + utf8(existing.empty() ? dest : existing) + " doesn't exist. Choose another.");
            return out;
        }
    }
    if (!writable(existing)) {
        out.push_back("TaskDeck can't write to " + utf8(existing) + ". Choose another folder.");
        return out;
    }
    // A .tar inside a .gz is decompressed next to its files first: room for both, for a moment.
    const uint64_t needed = s.compressed_tar ? s.unpacked * 2 : s.unpacked;
    ULARGE_INTEGER free{};
    if (needed && ::GetDiskFreeSpaceExW(existing.c_str(), &free, nullptr, nullptr) && needed > free.QuadPart)
        out.push_back("Not enough space: needs " + human_size(static_cast<double>(needed)) + ", " +
                      human_size(static_cast<double>(free.QuadPart)) + " free.");
    return out;
}

Outcome run(const fs::path& archive, const fs::path& dest, const std::optional<std::string>& password,
            const Reporter& r) {
    Outcome out;
    auto progress_for = [&](int phase) {
        return sevenzip::Progress([&r, phase](uint64_t done, uint64_t total) {
            r.advance(phase, done, total);
            return r.cancelled();
        });
    };
    auto fail = [&](int code, const std::string& entry) {
        if (code == sevenzip::E_CANCELLED) {
            out.status = Outcome::Cancelled;
            out.message = "Stopped. Files already extracted stay in " + utf8(dest) + ".";
        } else {
            out.status = Outcome::Failed;
            out.message = sevenzip::message(code, entry);
        }
        return out;
    };

    sevenzip::Archive a;
    int code = a.open(archive.wstring(), password);
    if (code != sevenzip::OK) return fail(code, "");
    auto entries = a.entries();
    std::string failed;

    if (is_compressor(a.format()) && tar_inside(utf8(archive.filename()), entries)) {
        // Two passes: the .tar out of the .gz (next to where its files go), then the files out of the .tar.
        const uint64_t tar_size = entries[0].size;
        r.set_phases({{"Decompressing", tar_size}, {"Extracting", tar_size}});
        std::error_code ec;
        fs::create_directories(dest, ec);
        const fs::path tar = dest / L".taskdeck-extracting.tar";
        code = a.extract_first(tar.wstring(), progress_for(0), &failed);
        if (code == sevenzip::OK) {
            sevenzip::Archive t;
            code = t.open(tar.wstring(), std::nullopt);
            if (code == sevenzip::OK) {
                entries = t.entries();
                code = t.extract(dest.wstring(), std::nullopt, progress_for(1), &failed);
            }
        }
        fs::remove(tar, ec);
    } else {
        uint64_t total = 0;
        for (const auto& e : entries)
            if (!e.dir) total += e.size;
        r.set_phases({{"Extracting", total}});
        code = a.extract(dest.wstring(), password, progress_for(0), &failed);
    }
    if (code != sevenzip::OK) return fail(code, failed);

    uint64_t count = 0, bytes = 0;
    for (const auto& e : entries)
        if (!e.dir) { ++count; bytes += e.size; }
    out.status = Outcome::Done;
    out.message = "Extracted " + files_text(count) + " (" + human_size(static_cast<double>(bytes)) + ") to " + utf8(dest);
    return out;
}

}  // namespace extract
