#include "updater.h"

#include "job_host.h"  // to_utf8, from_utf8
#include "sevenzip.h"

#include <bcrypt.h>
#include <winhttp.h>
#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <functional>
#include <vector>

#ifndef TASKDECK_BUILD
#define TASKDECK_BUILD 0
#endif

namespace fs = std::filesystem;
using nlohmann::json;

namespace {
constexpr const char* kReleaseTag = "taskdeck-desktop-latest";
constexpr const char* kRepo = "https://github.com/jbalcomb/ScratchPatch";
constexpr const char* kManifest = "taskdeck-desktop-manifest.json";

std::wstring env(const wchar_t* name) {
    wchar_t buf[2048];
    const DWORD n = ::GetEnvironmentVariableW(name, buf, 2048);
    return (n > 0 && n < 2048) ? std::wstring(buf, n) : std::wstring();
}

// Where the manifest and zip are: the release's download folder, or TASKDECK_UPDATE_URL (for testing, a local
// web server: "http://127.0.0.1:8765/").
std::string base_url() {
    std::string url = to_utf8(env(L"TASKDECK_UPDATE_URL"));
    if (url.empty()) url = std::string(kRepo) + "/releases/download/" + kReleaseTag + "/";
    if (url.back() != '/') url += '/';
    return url;
}

std::string now_hm() {
    const std::time_t t = std::time(nullptr);
    std::tm tm{};
    localtime_s(&tm, &t);
    char buf[16];
    std::strftime(buf, sizeof(buf), "%H:%M", &tm);
    return buf;
}

std::string win_error(DWORD code) {
    wchar_t* text = nullptr;
    const DWORD flags = FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS;
    HMODULE winhttp = ::GetModuleHandleW(L"winhttp.dll");
    DWORD n = ::FormatMessageW(flags, nullptr, code, 0, reinterpret_cast<wchar_t*>(&text), 0, nullptr);
    if (!n && winhttp)
        n = ::FormatMessageW(flags | FORMAT_MESSAGE_FROM_HMODULE, winhttp, code, 0, reinterpret_cast<wchar_t*>(&text), 0, nullptr);
    std::string s = n ? to_utf8(std::wstring(text, n)) : "error " + std::to_string(code);
    if (text) ::LocalFree(text);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ' || s.back() == '.')) s.pop_back();
    return s;
}

struct Handle {
    HINTERNET h = nullptr;
    ~Handle() { if (h) ::WinHttpCloseHandle(h); }
};

// GET over HTTPS (or HTTP, for a test server), following redirects (GitHub sends release files from another
// host). `sink` gets the body in pieces and returns false to stop. `total`: Content-Length, if sent.
bool http_get(const std::string& url, const std::function<bool(const char*, size_t, long long total)>& sink,
              std::string* error) {
    const std::wstring wurl = from_utf8(url);
    URL_COMPONENTS uc{sizeof(uc)};
    wchar_t host[256], path[2048];
    uc.lpszHostName = host;
    uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path;
    uc.dwUrlPathLength = 2048;
    if (!::WinHttpCrackUrl(wurl.c_str(), 0, 0, &uc)) {
        *error = "Bad address: " + url;
        return false;
    }
    const std::wstring agent = L"TaskDeck-Desktop/" + from_utf8(install::version());
    Handle session{::WinHttpOpen(agent.c_str(), WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME,
                                 WINHTTP_NO_PROXY_BYPASS, 0)};
    if (!session.h)  // before Windows 8.1: the default proxy settings
        session.h = ::WinHttpOpen(agent.c_str(), WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, WINHTTP_NO_PROXY_NAME,
                                  WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session.h) {
        *error = win_error(::GetLastError());
        return false;
    }
    ::WinHttpSetTimeouts(session.h, 10000, 10000, 15000, 30000);
    Handle connect{::WinHttpConnect(session.h, host, uc.nPort, 0)};
    const bool https = uc.nScheme == INTERNET_SCHEME_HTTPS;
    Handle request{connect.h ? ::WinHttpOpenRequest(connect.h, L"GET", path, nullptr, WINHTTP_NO_REFERER,
                                                    WINHTTP_DEFAULT_ACCEPT_TYPES, https ? WINHTTP_FLAG_SECURE : 0)
                             : nullptr};
    if (!request.h || !::WinHttpSendRequest(request.h, L"Cache-Control: no-cache\r\n", static_cast<DWORD>(-1), nullptr, 0, 0, 0) ||
        !::WinHttpReceiveResponse(request.h, nullptr)) {
        const DWORD code = ::GetLastError();
        *error = code == ERROR_WINHTTP_NAME_NOT_RESOLVED || code == ERROR_WINHTTP_CANNOT_CONNECT
                     ? "No connection to " + to_utf8(host) + " (offline?)"
                     : win_error(code);
        return false;
    }
    DWORD status = 0, size = sizeof(status);
    ::WinHttpQueryHeaders(request.h, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX,
                          &status, &size, WINHTTP_NO_HEADER_INDEX);
    if (status != 200) {
        *error = status == 404 ? "No release published yet (" + url + " not found)" : "The server answered " + std::to_string(status);
        return false;
    }
    wchar_t length[32];
    size = sizeof(length);
    long long total = -1;
    if (::WinHttpQueryHeaders(request.h, WINHTTP_QUERY_CONTENT_LENGTH, WINHTTP_HEADER_NAME_BY_INDEX, length, &size,
                              WINHTTP_NO_HEADER_INDEX))
        total = _wtoi64(length);
    std::vector<char> buf(256 * 1024);
    for (;;) {
        DWORD n = 0;
        if (!::WinHttpReadData(request.h, buf.data(), static_cast<DWORD>(buf.size()), &n)) {
            *error = "The download stopped: " + win_error(::GetLastError());
            return false;
        }
        if (n == 0) return true;
        if (!sink(buf.data(), n, total)) {
            if (error->empty()) *error = "Cancelled";
            return false;
        }
    }
}

class Sha256 {
public:
    Sha256() {
        ::BCryptOpenAlgorithmProvider(&alg_, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
        if (alg_) ::BCryptCreateHash(alg_, &hash_, nullptr, 0, nullptr, 0, 0);
    }
    ~Sha256() {
        if (hash_) ::BCryptDestroyHash(hash_);
        if (alg_) ::BCryptCloseAlgorithmProvider(alg_, 0);
    }
    void add(const char* p, size_t n) {
        if (hash_) ::BCryptHashData(hash_, reinterpret_cast<PUCHAR>(const_cast<char*>(p)), static_cast<ULONG>(n), 0);
    }
    std::string hex() {
        unsigned char d[32];
        if (!hash_ || ::BCryptFinishHash(hash_, d, sizeof(d), 0) != 0) return {};
        std::string s;
        char b[3];
        for (unsigned char c : d) {
            std::snprintf(b, sizeof(b), "%02x", c);
            s += b;
        }
        return s;
    }

private:
    BCRYPT_ALG_HANDLE alg_ = nullptr;
    BCRYPT_HASH_HANDLE hash_ = nullptr;
};

fs::path update_dir() { return install::dir() / L".update"; }

// The update unpacked earlier and not installed yet: its build number, or 0.
int ready_build() {
    std::ifstream in(update_dir() / L"ready.json");
    if (!in) return 0;
    const json j = json::parse(in, nullptr, false);
    if (!j.is_object() || !fs::exists(update_dir() / L"new" / L"TaskDeck.exe")) return 0;
    return j.value("build", 0);
}

// Renames, retrying for a few seconds: a job host that was just stopped may still be closing its files.
bool move(const fs::path& from, const fs::path& to, std::string* error) {
    std::error_code ec;
    for (int i = 0; i < 25; ++i) {
        fs::rename(from, to, ec);
        if (!ec) return true;
        ::Sleep(200);
    }
    *error = "Couldn't move " + to_utf8(from.wstring()) + " to " + to_utf8(to.wstring()) + ": " + ec.message();
    return false;
}
}  // namespace

namespace install {
int build() { return TASKDECK_BUILD; }

std::string version() { return build() > 0 ? "0.1." + std::to_string(build()) : "built from source"; }

fs::path dir() {
    wchar_t exe[MAX_PATH];
    const DWORD n = ::GetModuleFileNameW(nullptr, exe, MAX_PATH);
    return fs::path(std::wstring(exe, n)).parent_path();
}

bool packaged() {
    std::error_code ec;
    return fs::exists(dir() / L"python" / L"taskdeck_host.py", ec) && fs::exists(dir() / L"jobs" / L"taskdeck" / L"core", ec);
}

std::string release_page() { return std::string(kRepo) + "/releases/tag/" + kReleaseTag; }
}  // namespace install

Updater::~Updater() {
    stopping_ = true;
    join();
}

void Updater::join() {
    if (worker_.joinable()) worker_.join();
}

void Updater::set(const Status& s) {
    {
        std::lock_guard lock(mutex_);
        status_ = s;
    }
    if (notify_) ::PostMessageW(notify_, WM_APP, 0, 0);
}

Updater::Status Updater::status() const {
    std::lock_guard lock(mutex_);
    return status_;
}

void Updater::check(HWND notify, int delay_ms) {
    if (status().state == State::Downloading || status().state == State::Checking) return;
    join();
    notify_ = notify;
    Status s = status();
    s.state = State::Checking;
    s.error.clear();
    {
        std::lock_guard lock(mutex_);
        status_ = s;
    }
    worker_ = std::thread([this, delay_ms] {
        for (int waited = 0; waited < delay_ms && !stopping_; waited += 100) ::Sleep(100);
        if (stopping_) return;
        Status s = status();
        s.checked_at = now_hm();
        const int ready = ready_build();
        if (ready > install::build()) {
            s.state = State::Ready;
            s.latest_build = ready;
            s.latest_version = "0.1." + std::to_string(ready);
            set(s);
            return;
        }
        std::string body, error;
        const bool ok = http_get(base_url() + kManifest, [&](const char* p, size_t n, long long) {
            body.append(p, n);
            return body.size() < (1 << 20) && !stopping_;
        }, &error);
        const json m = ok ? json::parse(body, nullptr, false) : json();
        if (!ok || !m.is_object() || !m.contains("build") || !m.contains("sha256") || !m.contains("file")) {
            s.state = State::Failed;
            s.failed_download = false;
            s.error = ok ? "The release's manifest can't be read." : error;
            set(s);
            return;
        }
        s.latest_build = m.value("build", 0);
        s.latest_version = m.value("version", std::string());
        s.size = m.value("size", 0LL);
        s.state = s.latest_build > install::build() ? State::Available : State::UpToDate;
        {
            std::lock_guard lock(mutex_);
            zip_url_ = base_url() + m.value("file", std::string());
            sha256_ = m.value("sha256", std::string());
        }
        set(s);
    });
}

void Updater::download(HWND notify) {
    const State st = status().state;
    if (st != State::Available && !(st == State::Failed && status().failed_download)) return;
    join();
    notify_ = notify;
    Status s = status();
    s.state = State::Downloading;
    s.error.clear();
    s.done = 0;
    set(s);
    worker_ = std::thread([this] {
        Status s = status();
        std::string url, expected;
        {
            std::lock_guard lock(mutex_);
            url = zip_url_;
            expected = sha256_;
        }
        auto fail = [&](const std::string& why) {
            s.state = State::Failed;
            s.failed_download = true;
            s.error = why;
            set(s);
        };
        const fs::path up = update_dir();
        std::error_code ec;
        fs::remove_all(up, ec);
        fs::create_directories(up, ec);
        const fs::path zip = up / L"TaskDeck-update.zip";
        std::ofstream out(zip, std::ios::binary);
        if (ec || !out) {
            fail("TaskDeck can't write to its folder (" + to_utf8(install::dir().wstring()) +
                 "), so it can't update itself. Download the new version from the release page and unzip it over this one.");
            return;
        }
        Sha256 sha;
        auto last = std::chrono::steady_clock::now();
        std::string error;
        const bool ok = http_get(url, [&](const char* p, size_t n, long long total) {
            out.write(p, static_cast<std::streamsize>(n));
            sha.add(p, n);
            s.done += static_cast<long long>(n);
            if (total > 0) s.size = total;
            const auto now = std::chrono::steady_clock::now();
            if (now - last > std::chrono::milliseconds(200)) {
                last = now;
                set(s);
            }
            if (!out) error = "Couldn't write the download (disk full?)";
            return static_cast<bool>(out) && !stopping_;
        }, &error);
        out.close();
        if (!ok) return fail(error);
        if (sha.hex() != expected) return fail("The download doesn't match the release's checksum. Try again later.");

        // Unpack with 7-Zip's library, which Extract archive loads too.
        std::string why;
        if (!sevenzip::loaded() && !sevenzip::load((install::dir() / L"7z.dll").wstring(), &why))
            return fail("Can't unpack the update: 7z.dll can't be loaded (" + why + ").");
        sevenzip::Archive archive;
        int code = archive.open(zip.wstring(), std::nullopt);
        std::string entry;
        if (code == sevenzip::OK)
            code = archive.extract((up / L"new").wstring(), std::nullopt, [this](uint64_t, uint64_t) { return stopping_.load(); }, &entry);
        if (code != sevenzip::OK) return fail("Can't unpack the update: " + sevenzip::message(code, entry));
        if (!fs::exists(up / L"new" / L"TaskDeck.exe", ec)) return fail("The update has no TaskDeck.exe.");
        fs::remove(zip, ec);
        std::ofstream(up / L"ready.json") << json{{"build", s.latest_build}, {"version", s.latest_version}}.dump();
        s.state = State::Ready;
        set(s);
    });
}

bool Updater::install(std::string* error) {
    const fs::path dir = install::dir(), up = update_dir(), fresh = up / L"new";
    std::error_code ec;
    if (ready_build() <= 0) {
        *error = "There's no update unpacked to install.";
        return false;
    }
    fs::path old = dir / L".old";
    fs::remove_all(old, ec);
    if (fs::exists(old, ec)) old = dir / (L".old-" + std::to_wstring(::GetTickCount64()));
    fs::create_directories(old, ec);
    if (ec) {
        *error = "Can't create " + to_utf8(old.wstring()) + ": " + ec.message();
        return false;
    }
    std::vector<fs::path> names;
    for (const auto& e : fs::directory_iterator(fresh, ec)) names.push_back(e.path().filename());
    std::vector<fs::path> moved_out, moved_in;
    auto roll_back = [&] {
        std::string ignored;
        for (auto it = moved_in.rbegin(); it != moved_in.rend(); ++it) move(dir / *it, fresh / *it, &ignored);
        for (auto it = moved_out.rbegin(); it != moved_out.rend(); ++it) move(old / *it, dir / *it, &ignored);
    };
    for (const auto& name : names) {
        if (fs::exists(dir / name, ec)) {
            if (!move(dir / name, old / name, error)) return roll_back(), false;
            moved_out.push_back(name);
        }
        if (!move(fresh / name, dir / name, error)) return roll_back(), false;
        moved_in.push_back(name);
    }
    fs::remove_all(up, ec);
    return true;
}

void Updater::clean_up() {
    if (!install::packaged()) return;
    std::thread([] {
        const fs::path dir = install::dir();
        std::error_code ec;
        if (fs::exists(update_dir(), ec) && ready_build() <= install::build()) fs::remove_all(update_dir(), ec);
        // The TaskDeck that started this one may still be exiting: its exe is in .old.
        for (int i = 0; i < 40; ++i) {
            bool left = false;
            for (const auto& e : fs::directory_iterator(dir, ec))
                if (e.path().filename().wstring().rfind(L".old", 0) == 0) {
                    fs::remove_all(e.path(), ec);
                    left |= fs::exists(e.path());
                }
            if (!left) return;
            ::Sleep(250);
        }
    }).detach();
}
