// The TaskDeck window: the same four places as the phone app (Home, All jobs, History, Settings), the command
// palette (Ctrl+K), and job screens built from each job's fields. The jobs are the phone app's Python jobs,
// run by the job host (job_host.h).
#pragma once
#include "job_host.h"
#include "perf.h"

#include <nlohmann/json.hpp>

#include <map>
#include <string>
#include <vector>

struct FieldSpec {
    std::string name, label, kind, help, grouped;
    bool required = false, secret = false;
    int lines = 3;
    std::vector<std::string> choices, extensions;
    nlohmann::json def;
};

struct JobSpec {
    std::string id, title, description, category, run_label, commit_label, committed_label, search;
    std::vector<FieldSpec> fields, settings;
};

struct Problem {
    std::string message, fix;
    bool blocking = true;
};

struct RunState {
    int id = 0;
    std::string job_id, job_title;
    bool done = false, ok = false, cancelled = false, cancel_sent = false;
    int step = 0, total = 0;
    std::string step_name, error, started;  // started: local time, "14:05:09"
    double seconds = 0;
    nlohmann::json result;  // the host's result object, or null
    // The commit step (two-phase jobs): idle -> committing -> committed, or a message and a confirm button.
    enum class Commit { Idle, Committing, Committed, Failed } commit = Commit::Idle;
    std::string commit_message, confirm_label;
};

// What's entered on one job's screen; kept while the app runs (drafts on disk come later).
struct JobForm {
    std::map<std::string, std::string> text;
    std::map<std::string, bool> flags;
    std::vector<Problem> problems;
    bool filled = false;   // defaults put in
    bool checked = false;  // check_ready asked
    int run_id = 0;  // the run shown on the screen (0 = none yet)
};

class Shell {
public:
    // Starts the job host; messages from it wake `hwnd`'s message loop.
    void start(HWND hwnd);
    // Applies what the job host sent since the last frame. Call before the frame.
    void pump();
    // Draws one frame. Returns true while something animates, so the loop keeps drawing; otherwise the
    // window sleeps until the next input or job host message.
    bool draw(const PerfSnapshot& perf);
    // Work that can't happen inside a frame (the file dialog, which runs its own modal loop). Call after Present.
    void after_frame();
    void stop() { host_.stop(); }

private:
    void draw_home();
    void draw_jobs();
    void draw_history();
    void draw_settings(const PerfSnapshot& perf);
    void draw_palette();
    void draw_job(const JobSpec& job);
    void draw_field(const JobSpec& job, JobForm& form, const FieldSpec& f);
    void draw_run(const JobSpec& job, RunState& run);
    void draw_host_status();
    void open_job(const std::string& id);
    void run_job(const JobSpec& job, JobForm& form);
    JobForm& form_for(const JobSpec& job);
    const JobSpec* find_job(const std::string& id) const;
    void on_message(const nlohmann::json& msg);

    enum class Tab { Home, Jobs, History, Settings } tab_ = Tab::Home;
    HWND hwnd_ = nullptr;
    JobHost host_;
    enum class HostState { Starting, Ready, Failed } host_state_ = HostState::Starting;
    std::string host_error_, python_version_;
    std::map<std::string, std::string> load_errors_;
    std::vector<JobSpec> jobs_;
    std::map<std::string, JobForm> forms_;
    std::map<int, RunState> runs_;
    std::vector<int> history_;  // run ids, newest first
    std::map<int, std::string> checks_;  // request id -> job id
    int next_id_ = 1;
    std::string open_job_id_;
    bool palette_open_ = false, palette_focus_ = false, show_demo_ = false;
    char palette_query_[128] = {};
    struct Browse { std::string job_id, field; bool multiple = false; std::vector<std::string> extensions; };
    std::vector<Browse> browse_;
};
