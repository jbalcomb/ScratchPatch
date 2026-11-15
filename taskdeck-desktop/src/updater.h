// Releases and updates (docs/PLAN.md, decision 5: a plain exe with an update check).
//
// A release is a zip from GitHub Actions (.github/workflows/taskdeck-desktop-release.yml): TaskDeck.exe, 7z.dll,
// the job host (python/), the phone app's jobs (jobs/), Python itself (python-runtime/) and the licences,
// unzipped into any folder the user can write to. The workflow publishes it with a manifest
// (taskdeck-desktop-manifest.json: build number, SHA-256, size) to the `taskdeck-desktop-latest` release.
//
// Updating: the app reads the manifest; when its build number is higher, it offers the update. The zip is
// downloaded into `.update\` next to the exe, checked against the manifest's SHA-256 and unpacked there. On
// Restart, the app stops its jobs, moves the files it replaces into `.old\`, moves the new ones in (renaming a
// running exe is allowed on Windows; the job host has stopped), starts the new TaskDeck.exe and quits. If a move
// fails, everything moved is put back. The next start removes `.old\`.
#pragma once
#include <windows.h>

#include <atomic>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>

namespace install {
int build();                  // this exe's build number (the release workflow's run number); 0: built from source
std::string version();        // "0.1.<build>", or "built from source"
std::filesystem::path dir();  // the folder TaskDeck.exe is in
// The release layout: the job host and the jobs next to the exe (else the source tree's, see job_host.cpp).
bool packaged();
std::string release_page();   // where releases are, for a manual download
}  // namespace install

class Updater {
public:
    enum class State { Idle, Checking, UpToDate, Available, Downloading, Ready, Failed };
    struct Status {
        State state = State::Idle;
        int latest_build = 0;
        std::string latest_version, error, checked_at;  // checked_at: local time, "14:05"
        long long done = 0, size = 0;                    // while downloading
        bool failed_download = false;                    // Failed while downloading (Try again downloads)
    };
    ~Updater();
    // Reads the manifest in the background, after `delay_ms`; WM_APP to `notify` when the status changes.
    // An update unpacked earlier but not installed yet is found first (Ready).
    void check(HWND notify, int delay_ms = 0);
    // Downloads, checks and unpacks the release the last check found.
    void download(HWND notify);
    Status status() const;

    // After the app stopped its jobs: moves the unpacked release into place. True when it did (start
    // TaskDeck.exe again); false with `error` when nothing changed.
    static bool install(std::string* error);
    // Removes what an earlier update left (.old, a stale .update), in the background. Call at start.
    static void clean_up();

private:
    void set(const Status& s);
    void join();

    HWND notify_ = nullptr;
    mutable std::mutex mutex_;
    Status status_;
    std::string manifest_url_, zip_url_, sha256_;
    std::thread worker_;
    std::atomic<bool> stopping_{false};
};
