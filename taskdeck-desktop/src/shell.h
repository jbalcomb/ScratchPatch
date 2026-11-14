// The TaskDeck window: the same four places as the phone app (Home, All jobs, History, Settings), the command
// palette (Ctrl+K), job screens built from each job's fields, and each job's settings screen. The jobs are the
// phone app's Python jobs, run by the job host (job_host.h). Pins, history, drafts and settings are kept in
// state.json (store.h); secret settings in Credential Manager (credentials.h).
#pragma once
#include "commands.h"
#include "imgui.h"
#include "job_host.h"
#include "perf.h"
#include "store.h"

#include <nlohmann/json.hpp>

#include <map>
#include <set>
#include <string>
#include <vector>

struct FieldSpec {
    std::string name, label, kind, help, grouped;
    bool required = false, secret = false, importable = false;
    int lines = 3;
    std::vector<std::string> choices, extensions;
    nlohmann::json def;
};

struct QuickSpec {
    std::string title;
    nlohmann::json inputs;
};

struct JobSpec {
    std::string id, title, description, category, run_label, commit_label, committed_label, search;
    std::string settings_id;  // whose stored settings it uses (another job's, for jobs that share them)
    std::vector<std::string> tags, settings_actions;
    std::vector<FieldSpec> fields, settings;
    std::vector<QuickSpec> quick_actions;
};

struct Problem {
    std::string message, fix;  // fix: "settings" offers Open settings, "recheck" only Check again
    bool blocking = true;
};

struct RunState {
    int id = 0;
    std::string job_id, job_title;
    nlohmann::json inputs;  // as run, for history
    bool done = false, ok = false, cancelled = false, cancel_sent = false;
    int step = 0, total = 0;
    std::string step_name, error, started;  // started: local time, "14:05:09"
    double seconds = 0;
    nlohmann::json result;  // the host's result object, or null
    // The commit step (two-phase jobs): idle -> committing -> committed, or a message and a confirm button.
    enum class Commit { Idle, Committing, Committed, Failed } commit = Commit::Idle;
    std::string commit_message, confirm_label;
};

// Values entered on a form: text for every kind but toggles.
struct FormValues {
    std::map<std::string, std::string> text;
    std::map<std::string, bool> flags;
};

// What's entered on one job's screen. Kept as a draft in state.json until the job is done.
struct JobForm : FormValues {
    std::vector<Problem> problems;
    bool filled = false;    // defaults and the draft put in
    bool checked = false;   // check_ready asked
    bool checking = false;  // waiting for the answer
    bool dirty = false;     // edited since the draft was saved
    int run_id = 0;  // the run shown on the screen (0 = none yet)
};

// One job's settings screen while it's open.
struct SettingsForm : FormValues {
    std::string job_id;
    std::set<std::string> shown;  // secret fields shown in clear
    bool busy = false;            // Test or Save waiting for the host
    bool tested = false, ok = false, after_save = false, fixed = false;
    std::vector<std::string> passed;
    std::vector<Problem> problems;
    std::vector<std::string> action_text;  // what each settings button found
    std::vector<bool> action_busy;
};

class Shell {
public:
    // Opens the state file and starts the job host; messages from it wake `hwnd`'s message loop.
    void start(HWND hwnd);
    // Applies what the job host sent since the last frame. Call before the frame.
    void pump();
    // Draws one frame. Returns true while something animates, so the loop keeps drawing; otherwise the
    // window sleeps until the next input or job host message.
    bool draw(const PerfSnapshot& perf);
    // Work that can't happen inside a frame (the file dialog, which runs its own modal loop). Call after Present.
    void after_frame();
    // Saves the open job's draft and stops the job host.
    void stop();

private:
    void start_host();
    void draw_home();
    void draw_jobs();
    void draw_history();
    void draw_settings(const PerfSnapshot& perf);
    void draw_palette();
    void draw_job(const JobSpec& job);
    void draw_job_settings(const JobSpec& job);
    // Returns true when the value was edited.
    bool draw_field(const std::string& owner, FormValues& form, const FieldSpec& f, std::set<std::string>* shown);
    void draw_run(const JobSpec& job, RunState& run);
    void draw_host_status();
    void draw_notice();
    void tile_menu(const JobSpec& job, int index, int count);

    void go(int tab);
    void open_job(const std::string& id, const nlohmann::json* inputs = nullptr);
    void close_job();
    void open_settings(const std::string& job_id);
    void run_job(const JobSpec& job, JobForm& form);
    void toggle_pin(const std::string& id);
    JobForm& form_for(const JobSpec& job);
    const JobSpec* find_job(const std::string& id) const;
    void on_message(const nlohmann::json& msg);
    void on_reply(const nlohmann::json& msg);
    void save_draft(const std::string& job_id);
    nlohmann::json form_inputs(const JobSpec& job, const JobForm& form, bool for_draft) const;

    // Settings: what's stored (plain values in state.json, secrets from Credential Manager), and saving.
    const JobSpec& settings_owner(const JobSpec& job) const;  // the job whose settings fields describe them
    nlohmann::json job_settings(const JobSpec& job);
    void save_settings(const JobSpec& job, const nlohmann::json& values);
    const std::map<std::string, std::string>& secrets(const JobSpec& owner);
    void fill_settings_form(const JobSpec& owner, const nlohmann::json& values);
    nlohmann::json settings_form_values() const;

    std::vector<Command> build_commands() const;
    void run_command(const std::string& id);
    std::map<std::string, int> command_usage() const;

    enum class Tab { Home, Jobs, History, Settings } tab_ = Tab::Home;
    HWND hwnd_ = nullptr;
    Store store_;
    JobHost host_;
    enum class HostState { Starting, Ready, Failed } host_state_ = HostState::Starting;
    std::string host_error_, python_version_, notice_, credential_error_;
    ImVec4 notice_color_;
    std::map<std::string, std::string> load_errors_;
    std::vector<JobSpec> jobs_;
    std::map<std::string, JobForm> forms_;
    std::map<int, RunState> runs_;
    std::map<double, int> session_runs_;  // history entry time -> run id, for runs of this session
    std::map<std::string, std::map<std::string, std::string>> secrets_;  // settings id -> field -> value
    SettingsForm settings_form_;

    // Requests waiting for a reply from the job host.
    struct Request { enum Kind { Check, Test, Action } kind; std::string job_id; int index = 0; bool save = false; };
    std::map<int, Request> requests_;
    int next_id_ = 1;

    std::string open_job_id_, settings_job_id_;
    bool palette_open_ = false, palette_focus_ = false, show_demo_ = false;
    std::string palette_query_;
    int palette_selected_ = 0;

    // File dialogs to show after the frame: Browse/Add for a job field, Import file for a setting.
    struct Browse { std::string job_id, field; bool multiple = false, settings = false, import = false; std::vector<std::string> extensions; };
    std::vector<Browse> browse_;
};
