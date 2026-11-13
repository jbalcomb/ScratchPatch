// The Python job host as a child process: the phone app's jobs, run by python/taskdeck_host.py, spoken to in
// JSON lines over its stdin and stdout (the protocol is described at the top of that file).
#pragma once
#include <nlohmann/json.hpp>

#include <windows.h>

#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

class JobHost {
public:
    ~JobHost();
    // Starts the host. Messages arriving later are queued and `notify` gets a WM_APP message, which wakes the
    // app's idle loop. Returns false (with `error` set) if Python or the job sources can't be found or started.
    bool start(HWND notify, std::string& error);
    void send(const nlohmann::json& msg);
    // Messages received since the last call. When the host has exited, the last one is {"event": "exited"}.
    std::vector<nlohmann::json> take();
    void stop();
    bool running() const { return process_ != nullptr && !exited_; }
    const std::wstring& log_path() const { return log_path_; }
    const std::wstring& command_line() const { return command_line_; }

private:
    void read_loop();
    void push(nlohmann::json msg);

    HWND notify_ = nullptr;
    HANDLE process_ = nullptr, job_ = nullptr, stdin_w_ = nullptr, stdout_r_ = nullptr;
    std::thread reader_;
    std::mutex mutex_;
    std::vector<nlohmann::json> inbox_;
    std::atomic<bool> exited_{false};
    std::wstring log_path_, command_line_;
};

std::string to_utf8(const std::wstring& w);
std::wstring from_utf8(const std::string& s);
