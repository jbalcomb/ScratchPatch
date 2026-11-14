// Jobs written in C++ where speed matters (decision 1 in docs/PLAN.md): Extract archive, on 7-Zip. They speak
// the job host's protocol (python/taskdeck_host.py: ops in, events out), so the shell runs them like the Python
// jobs, and they replace the Python job of the same id. Each run checks its inputs, then extracts in a
// background task ("task" events), like the phone's Extract archive screen.
#pragma once
#include <nlohmann/json.hpp>

#include <windows.h>

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

class NativeJobs {
public:
    ~NativeJobs();
    // Loads 7z.dll from next to the exe. Messages arriving later wake `notify` (WM_APP), as the job host's do.
    void start(HWND notify);
    // The jobs, as the job host lists them in "ready".
    nlohmann::json jobs() const;
    bool has_job(const std::string& id) const;
    // Handles the message if it's for one of these jobs, runs or tasks. False: it's for the job host.
    bool handle(const nlohmann::json& msg);
    // Events since the last call.
    std::vector<nlohmann::json> take();
    // Cancels whatever runs and waits for it.
    void stop();
    // Why 7z.dll didn't load ("" if it did).
    const std::string& engine_error() const { return engine_error_; }

private:
    struct Task;
    void run(int run_id, const nlohmann::json& inputs);
    void start_task(int run_id, const std::string& archive, const std::string& dest, const std::string& password,
                    const nlohmann::json& inputs);
    void push(nlohmann::json msg);
    void emit(Task& t, bool force);
    void spawn(std::function<void()> fn);

    HWND notify_ = nullptr;
    std::string engine_error_;
    std::mutex mutex_;
    std::vector<nlohmann::json> inbox_;
    std::set<int> runs_;                                // run ids handled here
    std::map<int, std::shared_ptr<std::atomic<bool>>> run_cancel_;
    std::map<int, std::shared_ptr<Task>> tasks_;
    int next_task_ = 1000000;                           // apart from the job host's task ids
    std::vector<std::thread> threads_;
    std::atomic<bool> stopping_{false};
};
