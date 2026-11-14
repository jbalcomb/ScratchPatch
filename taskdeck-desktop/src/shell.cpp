#include "shell.h"

#include "credentials.h"
#include "file_dialog.h"
#include "imgui.h"
#include "imgui_stdlib.h"

#include <shellapi.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <sstream>

using nlohmann::json;

namespace {
std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string trim(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return {};
    return s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
}

std::string str(const json& j, const char* key) {
    if (!j.is_object()) return {};
    const auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

bool flag(const json& j, const char* key) {
    if (!j.is_object()) return false;
    const auto it = j.find(key);
    return it != j.end() && it->is_boolean() && it->get<bool>();
}

std::vector<std::string> strings(const json& j, const char* key) {
    std::vector<std::string> out;
    if (!j.is_object()) return out;
    const auto it = j.find(key);
    if (it != j.end() && it->is_array())
        for (const auto& v : *it)
            if (v.is_string()) out.push_back(v.get<std::string>());
    return out;
}

FieldSpec parse_field(const json& j) {
    FieldSpec f;
    f.name = str(j, "name");
    f.label = str(j, "label");
    f.kind = str(j, "kind");
    f.help = str(j, "help");
    f.grouped = str(j, "grouped");
    f.required = flag(j, "required");
    f.secret = flag(j, "secret");
    f.importable = flag(j, "importable");
    f.lines = j.value("lines", 3);
    f.choices = strings(j, "choices");
    f.extensions = strings(j, "extensions");
    f.def = j.contains("default") ? j["default"] : json();
    return f;
}

JobSpec parse_job(const json& j) {
    JobSpec job;
    job.id = str(j, "id");
    job.title = str(j, "title");
    job.description = str(j, "description");
    job.category = str(j, "category");
    job.run_label = str(j, "run_label");
    job.commit_label = str(j, "commit_label");
    job.committed_label = str(j, "committed_label");
    job.settings_id = str(j, "settings_id");
    if (job.settings_id.empty()) job.settings_id = job.id;
    job.tags = strings(j, "tags");
    job.settings_actions = strings(j, "settings_actions");
    for (const auto& f : j.value("fields", json::array())) job.fields.push_back(parse_field(f));
    for (const auto& f : j.value("settings", json::array())) job.settings.push_back(parse_field(f));
    for (const auto& a : j.value("accounts", json::array()))
        if (a.is_array() && a.size() == 2 && a[0].is_string() && a[1].is_string())
            job.accounts.emplace_back(a[0].get<std::string>(), a[1].get<std::string>());
    for (const auto& q : j.value("quick_actions", json::array()))
        job.quick_actions.push_back({str(q, "title"), q.value("inputs", json::object())});
    job.search = lower(job.title + " " + job.category + " " + job.description);
    for (const auto& t : job.tags) job.search += " " + lower(t);
    return job;
}

// A stored or default value as the text a form shows.
std::string value_text(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%g", v.get<double>());
        return buf;
    }
    if (v.is_boolean()) return v.get<bool>() ? "true" : "false";
    return {};
}

bool value_flag(const json& v) {
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number()) return v.get<double>() != 0;
    if (v.is_string()) return !v.get<std::string>().empty() && v.get<std::string>() != "false";
    return false;
}

// Puts `values` (field name -> value) into a form; fields missing from `values` are left alone.
void put_values(FormValues& form, const std::vector<FieldSpec>& fields, const json& values) {
    if (!values.is_object()) return;
    for (const auto& f : fields) {
        const auto it = values.find(f.name);
        if (it == values.end()) continue;
        if (f.kind == "toggle") form.flags[f.name] = value_flag(*it);
        else form.text[f.name] = value_text(*it);
    }
}

void put_defaults(FormValues& form, const std::vector<FieldSpec>& fields) {
    for (const auto& f : fields) {
        if (f.kind == "toggle") form.flags[f.name] = value_flag(f.def);
        else form.text[f.name] = value_text(f.def);
    }
}

std::string now_hms() {
    SYSTEMTIME t;
    ::GetLocalTime(&t);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%02d:%02d:%02d", t.wHour, t.wMinute, t.wSecond);
    return buf;
}

// A history entry's time: "14:05:09" today, "Nov 12  14:05" before.
std::string when(double at) {
    const std::time_t t = static_cast<std::time_t>(at), now = std::time(nullptr);
    std::tm local{}, today{};
    localtime_s(&local, &t);
    localtime_s(&today, &now);
    char buf[32];
    const bool same_day = local.tm_year == today.tm_year && local.tm_yday == today.tm_yday;
    std::strftime(buf, sizeof(buf), same_day ? "%H:%M:%S" : "%b %d  %H:%M", &local);
    return buf;
}

// The line history shows for a finished run: the result's first line (the host sends it), or the error.
std::string run_summary(const RunState& run) {
    if (run.cancelled) return "Cancelled";
    if (!run.ok) return run.error.empty() ? "Failed" : run.error;
    const std::string summary = str(run.result, "summary");
    return summary.empty() ? "Done" : summary;
}

// "1.5 MB", "820 KB", "12 B".
std::string human_size(double bytes) {
    const char* units[] = {"B", "KB", "MB", "GB", "TB"};
    int u = 0;
    while (bytes >= 1024 && u < 4) { bytes /= 1024; ++u; }
    char buf[32];
    std::snprintf(buf, sizeof(buf), u == 0 ? "%.0f %s" : "%.1f %s", bytes, units[u]);
    return buf;
}

// "12 s left", "3 min left", "1 h 20 min left".
std::string time_left(double seconds) {
    char buf[48];
    if (seconds < 60) std::snprintf(buf, sizeof(buf), "%.0f s left", seconds);
    else if (seconds < 3600) std::snprintf(buf, sizeof(buf), "%.0f min left", seconds / 60);
    else std::snprintf(buf, sizeof(buf), "%d h %d min left", static_cast<int>(seconds / 3600), static_cast<int>(seconds) % 3600 / 60);
    return buf;
}

std::string amount(long long n, const std::string& unit) {
    if (unit == "files") return std::to_string(n) + (n == 1 ? " file" : " files");
    return human_size(static_cast<double>(n));
}

double number_or(const json& j, const char* key, double fallback) {
    const auto it = j.find(key);
    return (it != j.end() && it->is_number()) ? it->get<double>() : fallback;
}

void open_url(const std::string& url) {
    ::ShellExecuteW(nullptr, L"open", from_utf8(url).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

const char* const kGoogle = "account:google";
const char* const kGoogleSetupDoc = "https://github.com/jbalcomb/ScratchPatch/blob/main/taskdeck/docs/GOOGLE_DRIVE_SETUP.md";

const ImVec4 kWarn(0.95f, 0.75f, 0.30f, 1.0f), kError(0.95f, 0.45f, 0.40f, 1.0f), kOk(0.45f, 0.85f, 0.50f, 1.0f);

// Pinned on Home the first time TaskDeck starts on this PC: the first desktop jobs, then a few handy ones.
const char* const kDefaultPins[] = {"extract_archive", "exe_parse", "dll_parse", "pdf_parser", "json_format", "hash_text"};

float dpi() { return ImGui::GetStyle().FontScaleDpi; }

void wrapped(const ImVec4& color, const std::string& text) {
    ImGui::PushStyleColor(ImGuiCol_Text, color);
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
}

void hint(const std::string& text) { wrapped(ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled), text); }
}  // namespace

// ---- start, stop, messages from the job host -------------------------------------------------------------

void Shell::start(HWND hwnd) {
    hwnd_ = hwnd;
    store_.open(Store::default_path());
    native_.start(hwnd);  // the C++ jobs: there even when Python isn't
    add_native_jobs();
    if (store_.get("pinned").is_null()) {
        json pins = json::array();
        for (const char* id : kDefaultPins) pins.push_back(id);
        store_.set("pinned", pins);
    }
    start_host();
}

void Shell::start_host() {
    host_state_ = HostState::Starting;
    host_error_.clear();
    if (!host_.start(hwnd_, host_error_)) host_state_ = HostState::Failed;
}

void Shell::stop() {
    for (const auto& [id, form] : forms_)
        if (form.dirty) save_draft(id);
    native_.stop();
    host_.stop();
}

void Shell::pump() {
    for (const auto& msg : native_.take()) on_message(msg);
    for (const auto& msg : host_.take()) on_message(msg);
}

void Shell::send(const json& msg) {
    if (!native_.handle(msg)) host_.send(msg);
}

bool Shell::job_ready(const JobSpec& job) const {
    return native_.has_job(job.id) || host_state_ == HostState::Ready;
}

// The C++ jobs replace the Python jobs of the same id (in their place in the list), or come first.
void Shell::add_native_jobs() {
    size_t front = 0;
    for (const auto& j : native_.jobs()) {
        JobSpec job = parse_job(j);
        auto it = std::find_if(jobs_.begin(), jobs_.end(), [&](const JobSpec& o) { return o.id == job.id; });
        if (it != jobs_.end()) *it = std::move(job);
        else jobs_.insert(jobs_.begin() + static_cast<std::ptrdiff_t>(front++), std::move(job));
    }
}

int Shell::running_tasks(std::string* first_title) const {
    int n = 0;
    for (const auto& [id, task] : tasks_)
        if (task.running()) {
            if (n++ == 0 && first_title) *first_title = task.title;
        }
    return n;
}

static Activity task_activity(const TaskState& t);

void Shell::on_task(const json& msg) {
    const int id = msg.value("id", 0);
    TaskState& t = tasks_[id];
    t.id = id;
    t.run = msg.value("run", 0);
    t.job_id = str(msg, "job");
    t.title = str(msg, "title");
    t.status = str(msg, "status");
    t.note = str(msg, "note");
    t.message = str(msg, "message");
    t.fix = str(msg, "fix");
    t.retry_label = str(msg, "retry_label");
    t.drive_url = str(msg, "drive_url");
    t.open_path = str(msg, "open_path");
    t.done = static_cast<long long>(number_or(msg, "done", 0));
    t.total = static_cast<long long>(number_or(msg, "total", 0));
    t.fraction = number_or(msg, "fraction", -1);
    t.eta = number_or(msg, "eta", -1);
    t.speed = number_or(msg, "speed", -1);
    t.inputs = msg.value("inputs", json::object());
    t.phases.clear();
    for (const auto& p : msg.value("phases", json::array()))
        t.phases.push_back({str(p, "label"), str(p, "unit"), static_cast<long long>(number_or(p, "done", 0)),
                            static_cast<long long>(number_or(p, "total", 0)), number_or(p, "fraction", -1)});
    if (!t.running() && !t.in_history) {  // background work goes into History when it ends, as on the phone
        t.in_history = true;
        History::add(store_, t.job_id, t.status == "done", t.message, plain_values(t.inputs));
        ended_.push_back(task_activity(t));
    }
}

// ---- what's running: the tray icon's menu and the close notice -----------------------------------------

static Activity task_activity(const TaskState& t) {
    Activity a;
    a.task = true;
    a.id = t.id;
    a.title = t.title;
    a.running = t.running();
    a.stopping = t.cancel_sent;
    char buf[64];
    if (a.running) {
        if (t.cancel_sent) a.status = "Stopping...";
        else if (t.fraction >= 0) { std::snprintf(buf, sizeof(buf), "%.0f%%", t.fraction * 100); a.status = buf; }
        else a.status = "Working...";
    } else if (t.status == "done") {
        a.status = "Done";
    } else if (t.status == "cancelled") {
        a.status = "Cancelled";
    } else {
        a.status = t.message.empty() ? "Failed" : "Failed: " + t.message;
        a.needs_you = true;
    }
    return a;
}

Activity Shell::run_activity(const RunState& run) const {
    Activity a;
    a.id = run.id;
    a.title = run.job_title;
    a.running = !run.done;
    a.stopping = run.cancel_sent;
    if (a.running) {
        if (run.cancel_sent) a.status = "Stopping...";
        else if (run.total == 0) a.status = "Starting...";
        else {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "Step %d of %d", std::min(run.step + 1, run.total), run.total);
            a.status = buf + (run.step_name.empty() ? std::string() : ": " + run.step_name);
        }
    } else if (run.cancelled) {
        a.status = "Cancelled";
    } else if (!run.ok) {
        a.status = run.error.empty() ? "Failed" : "Failed: " + run.error;
        a.needs_you = true;
    } else {
        const JobSpec* job = find_job(run.job_id);
        const bool commit = job && !job->commit_label.empty() && run.result.is_object() &&
                            run.result.value("has_payload", false) && run.commit == RunState::Commit::Idle;
        a.status = commit ? "Done, waiting for " + job->commit_label : "Done";
        a.needs_you = commit;
    }
    return a;
}

std::vector<Activity> Shell::activities() const {
    std::vector<Activity> out;
    for (const auto& [id, run] : runs_)
        if (!run.done) out.push_back(run_activity(run));
    for (const auto& [id, task] : tasks_)
        if (task.running()) out.push_back(task_activity(task));
    return out;
}

std::vector<Activity> Shell::take_ended() {
    std::vector<Activity> out;
    out.swap(ended_);
    return out;
}

void Shell::cancel(const Activity& a) {
    if (a.task) {
        auto it = tasks_.find(a.id);
        if (it == tasks_.end() || !it->second.running() || it->second.cancel_sent) return;
        send({{"op", "task_cancel"}, {"task", a.id}});
        it->second.cancel_sent = true;
        it->second.confirm_cancel = false;
    } else {
        auto it = runs_.find(a.id);
        if (it == runs_.end() || it->second.done || it->second.cancel_sent) return;
        send({{"op", "cancel"}, {"run", a.id}});
        it->second.cancel_sent = true;
    }
}

void Shell::cancel_all() {
    for (const auto& a : activities()) cancel(a);
}

Shell::CloseChoice Shell::close_choice() const {
    const json& v = store_.get("desktop_close_with_tasks");
    const std::string s = v.is_string() ? v.get<std::string>() : "";
    return s == "tray" ? CloseChoice::Tray : s == "quit" ? CloseChoice::Quit : CloseChoice::Ask;
}

void Shell::set_close_choice(CloseChoice c) {
    store_.set("desktop_close_with_tasks", c == CloseChoice::Tray ? "tray" : c == CloseChoice::Quit ? "quit" : "ask");
}

void Shell::ask_close() {
    close_notice_ = true;
    close_dont_ask_ = false;
}

Shell::CloseAction Shell::take_close_action() {
    const CloseAction a = close_action_;
    close_action_ = CloseAction::None;
    return a;
}

void Shell::draw_close_notice() {
    const char* title = "Close TaskDeck?";
    if (!ImGui::IsPopupOpen(title)) ImGui::OpenPopup(title);
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Always, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(580 * dpi(), 0));
    if (!ImGui::BeginPopupModal(title, nullptr, ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoSavedSettings)) return;
    const auto list = activities();
    if (list.empty()) {  // it all finished while the notice was open: nothing left to ask, so close as asked
        close_action_ = CloseAction::Quit;
        close_notice_ = false;
        ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
        return;
    }
    {
        ImGui::TextWrapped(list.size() == 1 ? "This is still running:" : "These are still running:");
        for (const auto& a : list) ImGui::BulletText("%s: %s", a.title.c_str(), a.status.c_str());
        ImGui::Spacing();
        ImGui::TextWrapped(list.size() == 1
            ? "TaskDeck can keep running in the notification area (by the clock) until it finishes, then close. "
              "Right-click its icon there to see it or cancel it."
            : "TaskDeck can keep running in the notification area (by the clock) until they finish, then close. "
              "Right-click its icon there to see them or cancel them.");
    }
    ImGui::Spacing();
    ImGui::Checkbox("Don't show this again", &close_dont_ask_);
    if (close_dont_ask_) hint("Closing will do what you pick now. You can change it in Settings, \"Closing the window\".");
    ImGui::Spacing();
    auto finish = [&](CloseAction action, CloseChoice choice) {
        if (close_dont_ask_) set_close_choice(choice);
        close_action_ = action;
        close_notice_ = false;
        ImGui::CloseCurrentPopup();
    };
    if (ImGui::Button("Keep running in the tray")) finish(CloseAction::Tray, CloseChoice::Tray);
    ImGui::SetItemDefaultFocus();
    ImGui::SameLine();
    if (ImGui::Button("Stop them and quit")) finish(CloseAction::Quit, CloseChoice::Quit);
    ImGui::SameLine();
    if (ImGui::Button("Cancel") || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
        close_notice_ = false;
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
}

// ---- the Google account ----------------------------------------------------------------------------------

json Shell::google_values() {
    const json& stored = store_.get(std::string("settings:") + kGoogle);
    json values = stored.is_object() ? stored : json::object();
    auto it = secrets_.find(kGoogle);
    if (it == secrets_.end()) {
        auto& cache = secrets_[kGoogle];
        for (const char* name : {"client_secret", "refresh_token"}) {
            std::string error;
            if (auto v = credentials::read(credentials::target(kGoogle, name), &error)) cache[name] = *v;
            if (!error.empty()) credential_error_ = "Couldn't read the Google sign-in from Credential Manager: " + error;
        }
        it = secrets_.find(kGoogle);
    }
    for (const auto& [name, value] : it->second) values[name] = value;
    return values;
}

void Shell::google_save(const json& changes) {
    json values = google_values();
    values.update(changes);
    auto& cache = secrets_[kGoogle];
    json plain = json::object();
    for (const auto& [name, value] : values.items()) {
        const std::string text = value.is_string() ? value.get<std::string>() : std::string();
        if (name != "client_secret" && name != "refresh_token") {
            plain[name] = value;
            continue;
        }
        const std::wstring target = credentials::target(kGoogle, name);
        if (text.empty()) {
            credentials::remove(target);
            cache.erase(name);
        } else if (cache[name] != text) {
            std::string error;
            if (credentials::write(target, text, &error)) cache[name] = text;
            else credential_error_ = "Couldn't save the Google sign-in in Credential Manager: " + error;
        }
    }
    store_.set(std::string("settings:") + kGoogle, plain);
    send_accounts();
}

void Shell::send_accounts() {
    if (host_state_ != HostState::Ready) return;
    const json v = google_values();
    send({{"op", "accounts"}, {"google", {{"client_id", str(v, "client_id")}, {"client_secret", str(v, "client_secret")},
                                               {"refresh_token", str(v, "refresh_token")}}}});
}

void Shell::google_connect(int retry_task) {
    const json v = google_values();
    google_.retry_task = retry_task;
    if (str(v, "client_id").empty() || str(v, "client_secret").empty()) {  // not set up yet: that first
        google_.setup_open = true;
        google_.client_id = str(v, "client_id");
        google_.client_secret = str(v, "client_secret");
        google_.setup_error.clear();
        return;
    }
    google_.req = next_id_++;
    google_.connecting = true;
    google_.user_code.clear();
    google_.url.clear();
    google_.error.clear();
    google_.status = "Getting a code from Google...";
    send({{"op", "google_connect"}, {"req", google_.req}, {"client_id", str(v, "client_id")},
                {"client_secret", str(v, "client_secret")}});
}

void Shell::on_message(const json& msg) {
    const std::string event = str(msg, "event");
    if (event == "ready") {
        host_state_ = HostState::Ready;
        python_version_ = str(msg, "python");
        jobs_.clear();
        for (const auto& j : msg.value("jobs", json::array())) jobs_.push_back(parse_job(j));
        add_native_jobs();
        load_errors_.clear();
        for (const auto& [k, v] : msg.value("errors", json::object()).items())
            load_errors_[k] = v.is_string() ? v.get<std::string>() : v.dump();
        for (auto& [id, form] : forms_) form.checked = form.checking = false;  // check again against the new host
        requests_.clear();
        settings_form_.busy = false;
        send_accounts();
    } else if (event == "fatal") {
        host_state_ = HostState::Failed;
        host_error_ = str(msg, "error");
    } else if (event == "exited") {
        if (host_state_ != HostState::Failed) {
            host_state_ = HostState::Failed;
            host_error_ = "The job host stopped (exit code " + std::to_string(msg.value("code", 0)) + ").";
        }
        for (auto& [id, run] : runs_)
            if (!run.done && !native_.has_job(run.job_id)) {
                run.done = true;
                run.error = "The job host stopped while this ran.";
                ended_.push_back(run_activity(run));
            }
        for (auto& [id, task] : tasks_)
            if (task.running() && !native_.has_job(task.job_id)) {
                task.status = "failed";
                task.message = "The job host stopped while this ran.";
                ended_.push_back(task_activity(task));
            }
        if (google_.connecting) google_.error = "The job host stopped.";
    } else if (event == "garbled") {
        load_errors_["(job host output)"] = str(msg, "line");
    } else if (msg.contains("reply")) {
        on_reply(msg);
    } else if (event == "task") {
        on_task(msg);
    } else if (event == "google_code" && msg.value("req", 0) == google_.req) {
        google_.user_code = str(msg, "user_code");
        google_.url = str(msg, "url");
        google_.status = "Waiting for you to approve...";
    } else if (event == "google_done" && msg.value("req", 0) == google_.req) {
        if (!flag(msg, "ok")) {
            google_.error = str(msg, "error");
            return;
        }
        google_save({{"refresh_token", str(msg, "refresh_token")}, {"email", str(msg, "email")}});
        google_.connecting = false;
        notice_ = "Google Drive connected" + (str(msg, "email").empty() ? std::string(".") : " as " + str(msg, "email") + ".");
        notice_color_ = kOk;
        if (google_.retry_task) {  // "Connect again" on a failed upload: now run it again
            send({{"op", "task_retry"}, {"task", google_.retry_task}});
            tasks_.erase(google_.retry_task);
            google_.retry_task = 0;
        }
    } else if (event == "progress" || event == "done" || event == "committed") {
        auto it = runs_.find(msg.value("run", 0));
        if (it == runs_.end()) return;
        RunState& run = it->second;
        const JobSpec* job = find_job(run.job_id);
        if (event == "progress") {
            run.step = msg.value("step", 0);
            run.total = msg.value("total", 0);
            run.step_name = str(msg, "name");
        } else if (event == "done") {
            run.done = true;
            run.ok = flag(msg, "ok");
            run.cancelled = flag(msg, "cancelled");
            run.error = str(msg, "error");
            run.seconds = msg.value("seconds", 0.0);
            run.result = msg.contains("result") ? msg["result"] : json();
            run.step = run.total;
            ended_.push_back(run_activity(run));
            // A run that only starts background work (Extract archive) leaves History to its task.
            const bool background = run.result.is_object() && run.result.value("background", false);
            if (!background) session_runs_[History::add(store_, run.job_id, run.ok, run_summary(run), run.inputs)] = run.id;
            // The draft is kept until the job is done (for jobs with a commit step: until that's done).
            if (run.ok && job && job->commit_label.empty()) Drafts::clear(store_, run.job_id);
        } else {
            run.commit_message = str(msg, "message");
            run.confirm_label = str(msg, "confirm_label");
            run.commit = flag(msg, "ok") ? RunState::Commit::Committed : RunState::Commit::Failed;
            if (run.commit == RunState::Commit::Committed) {
                const std::string label = job ? job->committed_label : "Done";
                session_runs_[History::add(store_, run.job_id, true, label + ": " + run.commit_message, run.inputs)] = run.id;
                Drafts::clear(store_, run.job_id);
            }
        }
    }
}

void Shell::on_reply(const json& msg) {
    const auto it = requests_.find(msg.value("reply", 0));
    if (it == requests_.end()) return;
    const Request req = it->second;
    requests_.erase(it);
    const JobSpec* job = find_job(req.job_id);
    if (!job) return;

    std::vector<Problem> problems;
    for (const auto& p : msg.value("problems", json::array()))
        if (!flag(p, "ok")) problems.push_back({str(p, "message"), str(p, "fix"), flag(p, "blocking")});
    const json fixes = msg.value("fixes", json::object());

    if (req.kind == Request::Check) {
        JobForm& form = forms_[req.job_id];
        form.problems = std::move(problems);
        form.checking = false;
        if (fixes.is_object() && !fixes.empty()) {
            // Settings the check found that work (a corrected address, say): keep them, so the next run needs no
            // lookup. Saving asks every job using these settings to check again; this one just did.
            json values = job_settings(*job);
            values.update(fixes);
            save_settings(*job, values);
            form.checked = true;
        }
        return;
    }

    SettingsForm& sf = settings_form_;
    if (settings_job_id_ != req.job_id) return;  // the settings screen was closed meanwhile
    if (req.kind == Request::Action) {
        if (req.index >= 0 && req.index < static_cast<int>(sf.action_text.size())) {
            sf.action_text[req.index] = str(msg, "text");
            sf.action_busy[req.index] = false;
        }
        put_values(sf, job->settings, msg.value("values", json::object()));
        return;
    }
    // Test settings, or Save (which tests first).
    sf.busy = false;
    sf.passed = strings(msg, "passed");
    sf.problems = std::move(problems);
    sf.ok = std::none_of(sf.problems.begin(), sf.problems.end(), [](const Problem& p) { return p.blocking; });
    sf.fixed = fixes.is_object() && !fixes.empty();
    put_values(sf, job->settings, fixes);  // fixes the checks found: show them in the form
    if (req.save && sf.ok) {
        json values = json::object();
        const json tested = msg.value("settings", json::object());
        for (const auto& f : job->settings)
            if (tested.contains(f.name)) values[f.name] = tested[f.name];
        save_settings(*job, values);
        settings_job_id_.clear();
        notice_ = "Settings saved and checked.";
        notice_color_ = kOk;
        if (!credential_error_.empty()) { notice_ = "Settings saved, but not the keys: " + credential_error_; notice_color_ = kError; }
        return;
    }
    sf.tested = true;
    sf.after_save = req.save;
}

// ---- jobs, forms, drafts ---------------------------------------------------------------------------------

const JobSpec* Shell::find_job(const std::string& id) const {
    for (const auto& j : jobs_)
        if (j.id == id) return &j;
    return nullptr;
}

json Shell::form_inputs(const JobSpec& job, const JobForm& form, bool for_draft) const {
    json inputs = json::object();
    for (const auto& f : job.fields) {
        if (for_draft && f.secret) continue;  // passwords stay out of drafts and history
        if (f.kind == "toggle") {
            const auto it = form.flags.find(f.name);
            inputs[f.name] = it != form.flags.end() && it->second;
        } else {
            const auto it = form.text.find(f.name);
            inputs[f.name] = it != form.text.end() ? it->second : std::string();
        }
    }
    return inputs;
}

JobForm& Shell::form_for(const JobSpec& job) {
    JobForm& form = forms_[job.id];
    if (!form.filled) {
        form.filled = true;
        put_defaults(form, job.fields);
        put_values(form, job.fields, Drafts::get(store_, job.id));
    }
    if (!form.checked && job_ready(job)) {
        const int req = next_id_++;
        requests_[req] = {Request::Check, job.id};
        send({{"op", "check"}, {"req", req}, {"job", job.id}, {"settings", job_settings(job)}, {"online", true}});
        form.checked = form.checking = true;
    }
    return form;
}

void Shell::save_draft(const std::string& job_id) {
    const JobSpec* job = find_job(job_id);
    auto it = forms_.find(job_id);
    if (!job || it == forms_.end() || !it->second.dirty) return;
    Drafts::save(store_, job_id, form_inputs(*job, it->second, true));
    it->second.dirty = false;
}

void Shell::go(int tab) {
    close_job();
    settings_job_id_.clear();
    notice_.clear();
    tab_ = static_cast<Tab>(tab);
    google_.setup_open = false;
}

void Shell::open_job(const std::string& id, const json* inputs) {
    if (!open_job_id_.empty() && open_job_id_ != id) save_draft(open_job_id_);
    open_job_id_ = id;
    settings_job_id_.clear();
    notice_.clear();
    palette_open_ = false;
    const JobSpec* job = find_job(id);
    if (!job) return;
    forms_[id].checked = false;  // check again each time it opens
    if (inputs) {  // a quick action or a history entry: its inputs over the form
        JobForm& form = form_for(*job);
        put_values(form, job->fields, *inputs);
        form.dirty = true;
    }
}

void Shell::close_job() {
    if (!open_job_id_.empty()) save_draft(open_job_id_);
    open_job_id_.clear();
}

void Shell::toggle_pin(const std::string& id) {
    const bool pinned = Pins::has(store_, id);
    Pins::toggle(store_, id);
    const JobSpec* job = find_job(id);
    notice_ = (job ? job->title : id) + (pinned ? " is no longer pinned to Home." : " is pinned to Home.");
    notice_color_ = kOk;
}

void Shell::run_job(const JobSpec& job, JobForm& form) {
    save_draft(job.id);
    if (auto old = runs_.find(form.run_id); old != runs_.end() && old->second.done &&
        old->second.commit != RunState::Commit::Committed && flag(old->second.result, "has_payload"))
        send({{"op", "forget"}, {"run", form.run_id}});  // the previous preview won't be committed now
    RunState run;
    run.id = next_id_++;
    run.job_id = job.id;
    run.job_title = job.title;
    run.inputs = form_inputs(job, form, true);
    run.started = now_hms();
    runs_[run.id] = run;
    form.run_id = run.id;
    send({{"op", "run"}, {"run", run.id}, {"job", job.id}, {"inputs", form_inputs(job, form, false)},
                {"settings", job_settings(job)}});
}

// ---- settings --------------------------------------------------------------------------------------------

const JobSpec& Shell::settings_owner(const JobSpec& job) const {
    if (job.settings_id != job.id)
        if (const JobSpec* owner = find_job(job.settings_id)) return *owner;
    return job;
}

const std::map<std::string, std::string>& Shell::secrets(const JobSpec& owner) {
    auto it = secrets_.find(owner.id);
    if (it != secrets_.end()) return it->second;
    auto& values = secrets_[owner.id];
    for (const auto& f : owner.settings) {
        if (!f.secret) continue;
        std::string error;
        if (auto v = credentials::read(credentials::target(owner.id, f.name), &error)) values[f.name] = *v;
        if (!error.empty()) credential_error_ = "Couldn't read " + f.label + " from Credential Manager: " + error;
    }
    return values;
}

json Shell::job_settings(const JobSpec& job) {
    const JobSpec& owner = settings_owner(job);
    const json& stored = store_.get("settings:" + owner.id);
    json values = stored.is_object() ? stored : json::object();
    for (const auto& f : owner.settings)
        if (f.secret) values.erase(f.name);  // never in state.json; only Credential Manager's copy counts
    for (const auto& [name, value] : secrets(owner)) values[name] = value;
    return values;
}

void Shell::save_settings(const JobSpec& job, const json& values) {
    const JobSpec& owner = settings_owner(job);
    json plain = json::object();
    auto& cache = secrets_[owner.id];
    secrets(owner);  // loaded, to compare against
    credential_error_.clear();
    for (const auto& [name, value] : values.items()) {
        const auto f = std::find_if(owner.settings.begin(), owner.settings.end(), [&](const FieldSpec& s) { return s.name == name; });
        if (f == owner.settings.end() || !f->secret) {
            plain[name] = value;
            continue;
        }
        const std::string text = value.is_string() ? value.get<std::string>() : value.is_null() ? "" : value.dump();
        const std::wstring target = credentials::target(owner.id, name);
        if (trim(text).empty()) {
            credentials::remove(target);
            cache.erase(name);
        } else if (cache[name] != text) {
            std::string error;
            if (credentials::write(target, text, &error)) cache[name] = text;
            else credential_error_ = "Couldn't save " + f->label + " in Credential Manager: " + error;
        }
    }
    store_.set("settings:" + owner.id, plain);
    for (const auto& j : jobs_)  // every job using these settings checks again
        if (&settings_owner(j) == &owner) forms_[j.id].checked = false;
}

void Shell::fill_settings_form(const JobSpec& owner, const json& values) {
    SettingsForm& sf = settings_form_;
    put_defaults(sf, owner.settings);
    for (const auto& f : owner.settings) {  // a stored null means "the default"
        const auto it = values.find(f.name);
        if (it == values.end() || it->is_null()) continue;
        if (f.kind == "toggle") sf.flags[f.name] = value_flag(*it);
        else sf.text[f.name] = value_text(*it);
    }
}

void Shell::open_settings(const std::string& job_id) {
    const JobSpec* job = find_job(job_id);
    if (!job) return;
    const JobSpec& owner = settings_owner(*job);
    if (!open_job_id_.empty()) save_draft(open_job_id_);
    settings_form_ = SettingsForm{};
    settings_form_.job_id = owner.id;
    settings_form_.action_text.assign(owner.settings_actions.size(), "");
    settings_form_.action_busy.assign(owner.settings_actions.size(), false);
    fill_settings_form(owner, job_settings(owner));
    settings_job_id_ = owner.id;
    palette_open_ = false;
    notice_.clear();
}

json Shell::settings_form_values() const {
    json values = json::object();
    const JobSpec* owner = find_job(settings_job_id_);
    if (!owner) return values;
    for (const auto& f : owner->settings) {
        if (f.kind == "toggle") {
            const auto it = settings_form_.flags.find(f.name);
            values[f.name] = it != settings_form_.flags.end() && it->second;
        } else {
            const auto it = settings_form_.text.find(f.name);
            values[f.name] = trim(it != settings_form_.text.end() ? it->second : std::string());
        }
    }
    return values;
}

// ---- the command palette ---------------------------------------------------------------------------------

std::vector<Command> Shell::build_commands() const {
    std::vector<Command> out;
    for (const auto& job : jobs_) {
        out.push_back({"job:" + job.id, job.title, job.category + " · " + job.description, "", CommandKind::Job, job.tags});
        if (!job.settings.empty() && job.settings_id == job.id) {
            std::vector<std::string> kw = job.tags;
            kw.insert(kw.begin(), "configure");
            out.push_back({"settings:" + job.id, "Settings: " + job.title, "Job settings", "", CommandKind::Action, kw});
        }
        for (const auto& q : job.quick_actions)
            out.push_back({"quick:" + job.id + ":" + q.title, q.title, job.title, "", CommandKind::Quick, job.tags});
    }
    const struct { const char *id, *title, *key; } nav[] = {{"nav:home", "Go to Home", "Ctrl+1"},
        {"nav:jobs", "Go to All jobs", "Ctrl+2"}, {"nav:history", "Go to History", "Ctrl+3"},
        {"nav:settings", "Go to Settings", "Ctrl+4"}};
    for (const auto& n : nav) out.push_back({n.id, n.title, "", n.key, CommandKind::Action, {"navigate", "open"}});
    const JobSpec* job = open_job_id_.empty() ? nullptr : find_job(open_job_id_);
    const bool pinned = job && Pins::has(store_, job->id);
    Command pin{"job:pin", "Pin current job to Home", "", "Ctrl+D", CommandKind::Action, {"favorite", "star"}};
    pin.applicable = job && !pinned;
    Command unpin{"job:unpin", "Unpin current job", "", "Ctrl+D", CommandKind::Action, {"favorite", "remove"}};
    unpin.applicable = pinned;
    Command run{"job:run", "Run current job", "", "", CommandKind::Action, {"execute", "start"}};
    run.applicable = job != nullptr;
    Command settings{"job:settings", "Settings for current job", "", "", CommandKind::Action, {"configure"}};
    settings.applicable = job && !job->settings.empty();
    out.insert(out.end(), {pin, unpin, run, settings});
    return out;
}

std::map<std::string, int> Shell::command_usage() const {
    std::map<std::string, int> usage;
    const json& stored = store_.get("command_usage");
    if (stored.is_object())
        for (const auto& [id, n] : stored.items())
            if (n.is_number_integer()) usage[id] = n.get<int>();
    const json& history = store_.get("history");
    if (history.is_array())
        for (const auto& e : history) usage["job:" + str(e, "job_id")] += 1;
    return usage;
}

void Shell::run_command(const std::string& id) {
    json usage = store_.get("command_usage").is_object() ? store_.get("command_usage") : json::object();
    usage[id] = usage.value(id, 0) + 1;
    store_.set("command_usage", usage);
    palette_open_ = false;

    const JobSpec* current = open_job_id_.empty() ? nullptr : find_job(open_job_id_);
    if (id.rfind("job:", 0) == 0 && find_job(id.substr(4))) open_job(id.substr(4));
    else if (id == "job:pin" || id == "job:unpin") { if (current) toggle_pin(current->id); }
    else if (id == "job:run") {
        if (current) {
            JobForm& form = form_for(*current);
            const bool blocked = std::any_of(form.problems.begin(), form.problems.end(), [](const Problem& p) { return p.blocking; });
            auto it = runs_.find(form.run_id);
            if (!blocked && job_ready(*current) && (it == runs_.end() || it->second.done)) run_job(*current, form);
        }
    } else if (id == "job:settings") { if (current) open_settings(current->id); }
    else if (id.rfind("settings:", 0) == 0) open_settings(id.substr(9));
    else if (id == "nav:home") go(0);
    else if (id == "nav:jobs") go(1);
    else if (id == "nav:history") go(2);
    else if (id == "nav:settings") go(3);
    else if (id.rfind("quick:", 0) == 0) {
        for (const auto& job : jobs_)
            for (const auto& q : job.quick_actions)
                if (id == "quick:" + job.id + ":" + q.title) { open_job(job.id, &q.inputs); return; }
    }
}

void Shell::draw_palette() {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f, vp->WorkPos.y + vp->WorkSize.y * 0.15f),
                            ImGuiCond_Always, ImVec2(0.5f, 0));
    ImGui::SetNextWindowSize(ImVec2(vp->WorkSize.x * 0.55f, 0));
    // No keyboard navigation in here: Up and Down move the selection while the cursor stays in the search box.
    if (ImGui::Begin("Command palette", &palette_open_,
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoNav)) {
        if (palette_focus_) { ImGui::SetKeyboardFocusHere(); palette_focus_ = false; }
        ImGui::SetNextItemWidth(-FLT_MIN);
        const std::string before = palette_query_;
        const bool enter = ImGui::InputTextWithHint("##q", "Type a job or command (> actions only, @ jobs only)",
                                                    &palette_query_, ImGuiInputTextFlags_EnterReturnsTrue);
        if (palette_query_ != before) palette_selected_ = 0;
        const auto commands = build_commands();
        const auto matches = search_commands(commands, palette_query_, command_usage(), 12);
        if (!matches.empty()) {
            if (ImGui::IsKeyPressed(ImGuiKey_DownArrow)) palette_selected_ = (palette_selected_ + 1) % static_cast<int>(matches.size());
            if (ImGui::IsKeyPressed(ImGuiKey_UpArrow))
                palette_selected_ = (palette_selected_ + static_cast<int>(matches.size()) - 1) % static_cast<int>(matches.size());
            palette_selected_ = std::clamp(palette_selected_, 0, static_cast<int>(matches.size()) - 1);
        }
        std::string chosen;
        for (int i = 0; i < static_cast<int>(matches.size()); ++i) {
            const Command& c = *matches[i].command;
            ImGui::PushID(c.id.c_str());
            if (ImGui::Selectable(c.title.c_str(), i == palette_selected_)) chosen = c.id;
            ImGui::PopID();
            if (!c.subtitle.empty()) {
                ImGui::SameLine();
                std::string sub = c.subtitle.size() > 90 ? c.subtitle.substr(0, 87) + "..." : c.subtitle;
                ImGui::TextDisabled("%s", sub.c_str());
            }
            if (!c.shortcut.empty()) {
                ImGui::SameLine(ImGui::GetContentRegionMax().x - ImGui::CalcTextSize(c.shortcut.c_str()).x);
                ImGui::TextDisabled("%s", c.shortcut.c_str());
            }
        }
        if (matches.empty()) ImGui::TextDisabled("Nothing matches.");
        if (enter && !matches.empty()) chosen = matches[palette_selected_].command->id;
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) palette_open_ = false;
        if (!chosen.empty()) run_command(chosen);
        else if (enter) palette_focus_ = true;  // Enter ended editing with nothing to open: back to typing
    }
    ImGui::End();
}

// ---- the frame -------------------------------------------------------------------------------------------

bool Shell::draw(const PerfSnapshot& perf) {
    ImGuiIO& io = ImGui::GetIO();
    if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_K)) {
        palette_open_ = !palette_open_;
        palette_focus_ = palette_open_;
        palette_query_.clear();
        palette_selected_ = 0;
    }
    const ImGuiKey digits[] = {ImGuiKey_1, ImGuiKey_2, ImGuiKey_3, ImGuiKey_4};
    for (int i = 0; i < 4; ++i)
        if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | digits[i])) go(i);
    if (!open_job_id_.empty() && settings_job_id_.empty() &&
        ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_D))
        toggle_pin(open_job_id_);

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->WorkPos);
    ImGui::SetNextWindowSize(vp->WorkSize);
    ImGui::Begin("TaskDeck", nullptr, ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                                          ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoSavedSettings);

    // Navigation on the left, like a desktop app (the phone has it at the bottom).
    ImGui::BeginChild("nav", ImVec2(180 * dpi(), 0), ImGuiChildFlags_Borders);
    const char* labels[] = {"Home", "All jobs", "History", "Settings"};
    const bool on_page = open_job_id_.empty() && settings_job_id_.empty();
    for (int i = 0; i < 4; ++i)
        if (ImGui::Selectable(labels[i], on_page && static_cast<int>(tab_) == i)) go(i);
    ImGui::Separator();
    if (ImGui::Button("Search  (Ctrl+K)")) { palette_open_ = true; palette_focus_ = true; palette_query_.clear(); }
    ImGui::EndChild();
    ImGui::SameLine();

    ImGui::BeginChild("page");
    const JobSpec* settings_job = settings_job_id_.empty() ? nullptr : find_job(settings_job_id_);
    const JobSpec* job = open_job_id_.empty() ? nullptr : find_job(open_job_id_);
    if (google_.setup_open) draw_google_setup();
    else if (settings_job) draw_job_settings(*settings_job);
    else if (job) draw_job(*job);
    else switch (tab_) {
        case Tab::Home: draw_home(); break;
        case Tab::Jobs: draw_jobs(); break;
        case Tab::History: draw_history(); break;
        case Tab::Settings: draw_settings(perf); break;
    }
    ImGui::EndChild();
    ImGui::End();

    if (palette_open_) draw_palette();
    if (google_.connecting) draw_google_connect();
    if (close_notice_) draw_close_notice();
    if (show_demo_) ImGui::ShowDemoWindow(&show_demo_);
    if (!palette_open_) palette_focus_ = false;  // a focus request for a closed palette would keep drawing
    return palette_focus_ || io.MouseDown[0];
}

void Shell::after_frame() {
    auto pending = std::move(browse_);
    browse_.clear();
    for (const auto& b : pending) {
        if (b.folder) {  // a folder field: starts where it points, if anywhere
            JobForm& form = forms_[b.job_id];
            const std::wstring folder = pick_folder(hwnd_, from_utf8(form.text[b.field]));
            if (!folder.empty()) {
                form.text[b.field] = to_utf8(folder);
                form.dirty = true;
            }
            continue;
        }
        const auto paths = pick_files(hwnd_, b.multiple, b.extensions);
        if (paths.empty()) continue;
        if (b.import) {  // Import file: the file's contents become the setting (a key file, say)
            if (settings_job_id_ != b.job_id) continue;
            std::ifstream in(paths[0], std::ios::binary);
            std::stringstream text;
            text << in.rdbuf();
            if (!in) {
                notice_ = "Couldn't read " + to_utf8(paths[0]);
                notice_color_ = kError;
                continue;
            }
            settings_form_.text[b.field] = trim(text.str());
            continue;
        }
        JobForm& form = forms_[b.job_id];
        form.dirty = true;
        std::string& value = form.text[b.field];
        if (!b.multiple) {
            value = to_utf8(paths[0]);
            continue;
        }
        if (!value.empty() && value.back() != '\n') value += '\n';
        for (const auto& p : paths) value += to_utf8(p) + "\n";
    }
}

void Shell::draw_host_status() {
    if (host_state_ == HostState::Starting) {
        ImGui::TextDisabled("Loading the jobs...");
    } else if (host_state_ == HostState::Failed) {
        wrapped(kError, "The jobs can't run: " + host_error_);
        if (ImGui::Button("Start the job host again")) start_host();
    }
}

void Shell::draw_notice() {
    if (notice_.empty()) return;
    wrapped(notice_color_, notice_);
    ImGui::Spacing();
}

// ---- Home, All jobs, History, Settings -------------------------------------------------------------------

void Shell::tile_menu(const JobSpec& job, int index, int count) {
    if (!ImGui::BeginPopupContextItem("tile")) return;
    if (ImGui::MenuItem("Open")) open_job(job.id);
    if (ImGui::MenuItem("Move left", nullptr, false, index > 0)) Pins::move(store_, job.id, index - 1);
    if (ImGui::MenuItem("Move right", nullptr, false, index + 1 < count)) Pins::move(store_, job.id, index + 1);
    if (!job.settings.empty() && ImGui::MenuItem("Settings...")) open_settings(job.id);
    ImGui::Separator();
    if (ImGui::MenuItem("Unpin from Home")) toggle_pin(job.id);
    ImGui::EndPopup();
}

void Shell::draw_home() {
    draw_host_status();
    draw_notice();
    ImGui::TextUnformatted("Pinned");
    ImGui::Separator();
    std::vector<const JobSpec*> pinned;  // jobs no longer installed are skipped, but stay pinned
    for (const auto& id : Pins::ids(store_))
        if (const JobSpec* job = find_job(id)) pinned.push_back(job);
    const float tile = 150 * dpi();
    const float avail = ImGui::GetContentRegionAvail().x;
    const int per_row = std::max(1, static_cast<int>(avail / (tile + ImGui::GetStyle().ItemSpacing.x)));
    for (int i = 0; i < static_cast<int>(pinned.size()); ++i) {
        const JobSpec& job = *pinned[i];
        if (i % per_row) ImGui::SameLine();
        ImGui::PushID(job.id.c_str());
        ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.5f, 0.5f));
        if (ImGui::Button(job.title.c_str(), ImVec2(tile, tile * 0.6f))) open_job(job.id);
        ImGui::PopStyleVar();
        ImGui::SetItemTooltip("%s\n\nRight-click to move or unpin.", job.description.c_str());
        tile_menu(job, i, static_cast<int>(pinned.size()));
        ImGui::PopID();
    }
    if (pinned.empty() && host_state_ == HostState::Ready)
        hint("Nothing pinned. Pin a job from its screen (Ctrl+D), or right-click it in All jobs.");

    if (running_tasks() > 0) {
        ImGui::Spacing();
        ImGui::TextUnformatted("Running");
        ImGui::Separator();
        for (auto& [id, task] : tasks_)
            if (task.running()) draw_task(task, false);
    }

    const auto recent = History::recent_job_ids(store_);
    if (!recent.empty()) {
        ImGui::Spacing();
        ImGui::TextUnformatted("Recent");
        ImGui::Separator();
        bool first = true;
        for (const auto& id : recent) {
            const JobSpec* job = find_job(id);
            if (!job) continue;
            if (!first) ImGui::SameLine();
            first = false;
            ImGui::PushID(id.c_str());
            if (ImGui::Button(job->title.c_str())) open_job(id);
            ImGui::PopID();
        }
    }
}

void Shell::draw_jobs() {
    draw_host_status();
    draw_notice();
    if (ImGui::BeginTable("jobs", 3, ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_Resizable |
                                         ImGuiTableFlags_ScrollY)) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Job", ImGuiTableColumnFlags_WidthFixed, 200 * dpi());
        ImGui::TableSetupColumn("Category", ImGuiTableColumnFlags_WidthFixed, 110 * dpi());
        ImGui::TableSetupColumn("What it does", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableHeadersRow();
        for (const auto& job : jobs_) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::PushID(job.id.c_str());
            const bool pinned = Pins::has(store_, job.id);
            const std::string label = pinned ? job.title + "  (pinned)" : job.title;
            if (ImGui::Selectable(label.c_str(), false, ImGuiSelectableFlags_SpanAllColumns)) open_job(job.id);
            if (ImGui::BeginPopupContextItem("row")) {
                if (ImGui::MenuItem("Open")) open_job(job.id);
                if (ImGui::MenuItem(pinned ? "Unpin from Home" : "Pin to Home")) toggle_pin(job.id);
                if (!job.settings.empty() && ImGui::MenuItem("Settings...")) open_settings(job.id);
                ImGui::EndPopup();
            }
            ImGui::PopID();
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(job.category.c_str());
            ImGui::TableSetColumnIndex(2);
            ImGui::TextWrapped("%s", job.description.c_str());
        }
        ImGui::EndTable();
    }
}

void Shell::draw_history() {
    const json& entries = store_.get("history");
    std::vector<const RunState*> running;
    for (const auto& [id, run] : runs_)
        if (!run.done) running.push_back(&run);
    if ((!entries.is_array() || entries.empty()) && running.empty()) {
        hint("No runs yet. Every job you run is listed here; open one to go back to it.");
        return;
    }
    if (ImGui::Button("Clear history")) ImGui::OpenPopup("clear");
    if (ImGui::BeginPopup("clear")) {
        ImGui::TextUnformatted("Remove every entry? Results of this session stay on their job screens.");
        if (ImGui::Button("Clear")) {
            History::clear(store_);
            session_runs_.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Keep")) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
    if (!ImGui::BeginTable("history", 3, ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_ScrollY))
        return;
    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("When", ImGuiTableColumnFlags_WidthFixed, 110 * dpi());
    ImGui::TableSetupColumn("Job", ImGuiTableColumnFlags_WidthFixed, 200 * dpi());
    ImGui::TableSetupColumn("Outcome", ImGuiTableColumnFlags_WidthStretch);
    ImGui::TableHeadersRow();
    for (const RunState* run : running) {
        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        ImGui::PushID(run->id);
        if (ImGui::Selectable(run->started.c_str(), false, ImGuiSelectableFlags_SpanAllColumns)) {
            forms_[run->job_id].run_id = run->id;
            open_job(run->job_id);
        }
        ImGui::PopID();
        ImGui::TableSetColumnIndex(1);
        ImGui::TextUnformatted(run->job_title.c_str());
        ImGui::TableSetColumnIndex(2);
        ImGui::TextDisabled("Running...");
    }
    if (entries.is_array()) {
        std::string open_id;
        const json* open_inputs = nullptr;
        int open_run = 0;
        for (size_t i = 0; i < entries.size(); ++i) {
            const json& e = entries[i];
            const std::string job_id = str(e, "job_id");
            const JobSpec* job = find_job(job_id);
            const double at = e.value("at", 0.0);
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::PushID(static_cast<int>(i));
            if (ImGui::Selectable(when(at).c_str(), false, ImGuiSelectableFlags_SpanAllColumns) && job) {
                open_id = job_id;
                const auto run = session_runs_.find(at);
                if (run != session_runs_.end()) open_run = run->second;  // this session's: its result is still here
                else open_inputs = &e["inputs"];                         // older: open the job with its inputs
            }
            ImGui::PopID();
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(job ? job->title.c_str() : job_id.c_str());
            ImGui::TableSetColumnIndex(2);
            const std::string summary = str(e, "summary");
            if (flag(e, "ok")) ImGui::TextColored(kOk, "%s", summary.c_str());
            else ImGui::TextColored(summary == "Cancelled" ? kWarn : kError, "%s", summary.c_str());
        }
        if (!open_id.empty()) {  // after the loop: opening may change the entries
            const json inputs = open_inputs ? *open_inputs : json();
            if (open_run) forms_[open_id].run_id = open_run;
            open_job(open_id, open_inputs ? &inputs : nullptr);
        }
    }
    ImGui::EndTable();
}

void Shell::draw_settings(const PerfSnapshot& perf) {
    draw_notice();
    ImGui::SeparatorText("Job settings");
    hint("Kept on this PC. Keys and passwords are in Windows Credential Manager, the rest in the file below.");
    for (const auto& job : jobs_) {
        if (job.settings.empty() || job.settings_id != job.id) continue;
        ImGui::PushID(job.id.c_str());
        if (ImGui::Selectable(job.title.c_str())) open_settings(job.id);
        ImGui::PopID();
    }
    if (!credential_error_.empty()) wrapped(kError, credential_error_);

    ImGui::SeparatorText("Accounts");
    draw_google_rows();

    ImGui::SeparatorText("Closing the window");
    hint("When jobs or background tasks (uploads) are still running:");
    int choice = static_cast<int>(close_choice());
    bool changed = ImGui::RadioButton("Ask", &choice, 0);
    changed |= ImGui::RadioButton("Keep running in the notification area (tray) until they finish", &choice, 1);
    changed |= ImGui::RadioButton("Stop them and quit", &choice, 2);
    if (changed) set_close_choice(static_cast<CloseChoice>(choice));

    ImGui::SeparatorText("Jobs");
    const char* state = host_state_ == HostState::Ready ? "running" : host_state_ == HostState::Starting ? "starting" : "stopped";
    ImGui::Text("Job host:      %s, Python %s, %d jobs", state, python_version_.c_str(), static_cast<int>(jobs_.size()));
    if (!host_error_.empty()) ImGui::TextColored(kError, "%s", host_error_.c_str());
    for (const auto& [name, error] : load_errors_) ImGui::TextColored(kWarn, "Not loaded: %s: %s", name.c_str(), error.c_str());
    ImGui::TextDisabled("Log: %s", to_utf8(host_.log_path()).c_str());
    if (ImGui::Button("Restart the job host")) start_host();

    ImGui::SeparatorText("Your data");
    hint("Pins, history, drafts and settings: " + to_utf8(store_.path().wstring()));
    if (!store_.error().empty()) ImGui::TextColored(kError, "%s", store_.error().c_str());

    ImGui::SeparatorText("Performance (for comparing with the WinUI 3 build)");
    ImGui::Text("First frame:   %.0f ms after start", perf.first_frame_ms);
    ImGui::Text("Frame time:    %.2f ms average, %.2f ms worst (last 120 frames)", perf.frame_ms_avg, perf.frame_ms_max);
    ImGui::Text("CPU:           %.1f %% of one core", perf.cpu_percent);
    ImGui::Text("Memory:        %.1f MB working set, %.1f MB private", perf.working_set_mb, perf.private_mb);
    ImGui::Text("Frames drawn:  %llu", static_cast<unsigned long long>(perf.frames));
    ImGui::Spacing();
    ImGui::Checkbox("Show the Dear ImGui demo window", &show_demo_);
}

// ---- a job's screen --------------------------------------------------------------------------------------

void Shell::draw_job(const JobSpec& job) {
    JobForm& form = form_for(job);
    if (ImGui::Button("< Back")) { close_job(); return; }
    ImGui::SameLine();
    ImGui::PushFont(nullptr, ImGui::GetStyle().FontSizeBase * 1.4f);
    ImGui::TextUnformatted(job.title.c_str());
    ImGui::PopFont();
    // Pin and Settings at the right of the title row.
    const bool pinned = Pins::has(store_, job.id);
    const char* pin_label = pinned ? "Unpin" : "Pin to Home";
    const ImGuiStyle& style = ImGui::GetStyle();
    float right = ImGui::CalcTextSize(pin_label).x + style.FramePadding.x * 2;
    if (!job.settings.empty()) right += ImGui::CalcTextSize("Settings").x + style.FramePadding.x * 2 + style.ItemSpacing.x;
    ImGui::SameLine(ImGui::GetContentRegionMax().x - right);
    if (ImGui::Button(pin_label)) toggle_pin(job.id);
    ImGui::SetItemTooltip("Ctrl+D");
    if (!job.settings.empty()) {
        ImGui::SameLine();
        if (ImGui::Button("Settings")) { open_settings(job.id); return; }
    }
    hint(job.description);
    draw_notice();

    bool blocked = false, settings_fix = false;
    for (const Problem& p : form.problems) {
        blocked |= p.blocking;
        settings_fix |= p.fix == "settings" && !job.settings.empty();
        wrapped(p.blocking ? kError : kWarn, p.message);
    }
    if (form.checking) {
        ImGui::TextDisabled("Checking...");
    } else if (!form.problems.empty()) {
        if (settings_fix) {
            if (ImGui::SmallButton("Open settings")) { open_settings(job.id); return; }
            ImGui::SameLine();
        }
        if (ImGui::SmallButton("Check again")) form.checked = false;
    }
    ImGui::Spacing();

    ImGui::PushItemWidth(-FLT_MIN);
    for (const auto& f : job.fields) form.dirty |= draw_field(job.id, form, f, nullptr);
    ImGui::PopItemWidth();
    ImGui::Spacing();

    auto run_it = runs_.find(form.run_id);
    const bool running = run_it != runs_.end() && !run_it->second.done;
    // As on the phone: no new run while the checks run or while the job's background task does.
    const bool task_running = std::any_of(tasks_.begin(), tasks_.end(),
                                          [&](const auto& t) { return t.second.job_id == job.id && t.second.running(); });
    ImGui::BeginDisabled(blocked || running || task_running || form.checking || !job_ready(job));
    if (ImGui::Button(job.run_label.empty() ? "Run" : job.run_label.c_str())) run_job(job, form);
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
        if (blocked) ImGui::SetItemTooltip("Fix the problems above first.");
        else if (task_running) ImGui::SetItemTooltip("Wait for the task below to finish, or cancel it.");
    }
    ImGui::SameLine();
    if (ImGui::Button("Clear")) {
        put_defaults(form, job.fields);
        Drafts::clear(store_, job.id);
        form.dirty = false;
    }
    ImGui::SetItemTooltip("Back to the defaults; forgets the draft.");

    // The job's background task (an upload after the run): the running one, else the latest.
    TaskState* task = nullptr;
    for (auto& [id, t] : tasks_)
        if (t.job_id == job.id && (!task || !task->running())) task = &t;
    if (task) {
        ImGui::Spacing();
        draw_task(*task, true);
    }

    run_it = runs_.find(form.run_id);  // run_job may have started a new one
    if (run_it != runs_.end()) {
        ImGui::Spacing();
        draw_run(job, run_it->second);
    }
}

void Shell::draw_task(TaskState& t, bool on_job_screen) {
    ImGui::PushID(t.id);
    const ImGuiStyle& style = ImGui::GetStyle();
    ImGui::BeginChild("task", ImVec2(0, 0), ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY);
    if (!on_job_screen) {
        if (ImGui::Selectable(t.title.c_str())) open_job(t.job_id);
    } else {
        ImGui::TextUnformatted(t.title.c_str());
    }
    if (t.running()) {
        // One bar per phase (unpacking, then uploading...), or one for the whole task.
        auto bar = [&](const std::string& label, long long done, long long total, const std::string& unit, double fraction,
                       bool current) {
            std::string text = label;
            if (total > 0) text += (text.empty() ? "" : ": ") + amount(done, unit) + " of " + amount(total, unit);
            if (current && t.speed > 0 && unit != "files") text += "  " + human_size(t.speed) + "/s";
            if (current && t.eta >= 0) text += "  " + time_left(t.eta);
            ImGui::ProgressBar(fraction >= 0 ? static_cast<float>(fraction) : -1.0f * static_cast<float>(ImGui::GetTime()),
                               ImVec2(-FLT_MIN, 0), text.c_str());
        };
        if (t.phases.empty()) {
            bar("", t.done, t.total, "bytes", t.fraction, true);
        } else {
            bool current_found = false;
            for (const auto& p : t.phases) {
                const bool complete = p.total > 0 && p.done >= p.total;
                const bool current = !complete && !current_found;
                current_found |= current;
                bar(p.label, p.done, p.total, p.unit, complete ? 1.0 : p.fraction, current);
            }
        }
        if (!t.note.empty()) hint(t.note);
        if (on_job_screen) {
            if (t.confirm_cancel) {
                ImGui::TextUnformatted("Stop it? What's already done stays.");
                ImGui::SameLine();
                if (ImGui::Button("Stop")) {
                    send({{"op", "task_cancel"}, {"task", t.id}});
                    t.cancel_sent = true;
                    t.confirm_cancel = false;
                }
                ImGui::SameLine();
                if (ImGui::Button("Keep going")) t.confirm_cancel = false;
            } else {
                ImGui::BeginDisabled(t.cancel_sent);
                if (ImGui::Button(t.cancel_sent ? "Stopping..." : "Cancel")) t.confirm_cancel = true;
                ImGui::EndDisabled();
            }
        }
    } else {
        const ImVec4 color = t.status == "done" ? kOk : t.status == "cancelled" ? kWarn : kError;
        wrapped(color, t.message.empty() ? t.status : t.message);
        bool row = false;
        auto button = [&](const char* label) {
            if (row) ImGui::SameLine();
            row = true;
            return ImGui::Button(label);
        };
        if (!t.drive_url.empty() && button("Open in Drive")) open_url(t.drive_url);
        if (!t.open_path.empty() && button("Open folder"))
            ::ShellExecuteW(nullptr, L"open", from_utf8(t.open_path).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        const bool retryable = !t.retry_label.empty() && (t.status == "failed" || t.status == "cancelled");
        if (t.status == "failed" && t.fix == kGoogle && button("Connect again")) google_connect(retryable ? t.id : 0);
        if (retryable && button(t.retry_label.c_str())) {
            send({{"op", "task_retry"}, {"task", t.id}});
            const int id = t.id;
            ImGui::EndChild();
            ImGui::PopID();
            tasks_.erase(id);  // the host starts it again under a new id
            return;
        }
        if (button("Close")) {
            send({{"op", "task_forget"}, {"task", t.id}});
            const int id = t.id;
            ImGui::EndChild();
            ImGui::PopID();
            tasks_.erase(id);
            return;
        }
    }
    (void)style;
    ImGui::EndChild();
    ImGui::PopID();
}

// ---- the Google account: Settings rows, the Set up screen, the sign-in window ----------------------------

void Shell::draw_google_rows() {
    const json v = google_values();
    const bool configured = !str(v, "client_id").empty() && !str(v, "client_secret").empty();
    const bool connected = !str(v, "refresh_token").empty();
    ImGui::TextUnformatted("Google Drive");
    ImGui::SameLine(160 * dpi());
    if (connected) ImGui::TextColored(kOk, "Connected%s", str(v, "email").empty() ? "" : (" as " + str(v, "email")).c_str());
    else ImGui::TextDisabled("%s", configured ? "Not connected" : "Not set up");
    ImGui::SameLine();
    ImGui::BeginDisabled(host_state_ != HostState::Ready);
    if (connected) {
        if (ImGui::SmallButton("Disconnect")) {
            const std::string token = str(v, "refresh_token");
            google_save({{"refresh_token", ""}, {"email", ""}});
            send({{"op", "google_revoke"}, {"token", token}});
            notice_ = "Google Drive disconnected.";
            notice_color_ = kOk;
        }
    } else if (ImGui::SmallButton(configured ? "Connect" : "Set up")) {
        google_connect();
    }
    ImGui::EndDisabled();
    if (configured) {
        ImGui::SameLine();
        if (ImGui::SmallButton("Change client ID / secret")) {
            google_.setup_open = true;
            google_.client_id = str(v, "client_id");
            google_.client_secret = str(v, "client_secret");
            google_.setup_error.clear();
        }
    }
    hint("Jobs that upload to Google Drive (Parse executable with \"Push to Google Drive\") sign in with this. "
         "The client secret and the sign-in are kept in Credential Manager.");
}

void Shell::draw_google_setup() {
    GoogleState& g = google_;
    if (ImGui::Button("< Back")) { g.setup_open = false; return; }
    ImGui::SameLine();
    ImGui::PushFont(nullptr, ImGui::GetStyle().FontSizeBase * 1.4f);
    ImGui::TextUnformatted("Google Drive");
    ImGui::PopFont();
    ImGui::TextWrapped("TaskDeck needs its own Google sign-in client. Create one in Google Cloud (type \"TVs and "
                       "Limited Input devices\") and paste its Client ID and secret here. The phone app's client "
                       "works too.");
    if (ImGui::Button("Open the setup guide")) open_url(kGoogleSetupDoc);
    ImGui::Spacing();
    ImGui::PushItemWidth(-FLT_MIN);
    ImGui::TextUnformatted("Client ID");
    ImGui::InputText("##id", &g.client_id);
    hint("Ends in .apps.googleusercontent.com");
    ImGui::TextUnformatted("Client secret");
    ImGui::InputText("##secret", &g.client_secret, ImGuiInputTextFlags_Password);
    ImGui::PopItemWidth();
    if (!g.setup_error.empty()) wrapped(kError, g.setup_error);
    ImGui::Spacing();
    ImGui::BeginDisabled(host_state_ != HostState::Ready);
    if (ImGui::Button("Save and connect")) {
        const std::string id = trim(g.client_id), secret = trim(g.client_secret);
        const std::string suffix = ".apps.googleusercontent.com";
        if (id.size() <= suffix.size() || id.compare(id.size() - suffix.size(), suffix.size(), suffix) != 0) {
            g.setup_error = "The Client ID should end in .apps.googleusercontent.com.";
        } else if (secret.empty()) {
            g.setup_error = "Paste the client secret too.";
        } else {
            json changes = {{"client_id", id}, {"client_secret", secret}};
            if (id != str(google_values(), "client_id")) {  // another client: the old sign-in doesn't apply
                changes["refresh_token"] = "";
                changes["email"] = "";
            }
            google_save(changes);
            g.setup_open = false;
            g.client_secret.clear();
            google_connect(g.retry_task);
        }
    }
    ImGui::EndDisabled();
}

void Shell::draw_google_connect() {
    GoogleState& g = google_;
    if (!ImGui::IsPopupOpen("Connect Google Drive")) ImGui::OpenPopup("Connect Google Drive");
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Always, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(520 * dpi(), 0));
    if (!ImGui::BeginPopupModal("Connect Google Drive", nullptr, ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoSavedSettings))
        return;
    if (!g.user_code.empty()) {
        ImGui::TextWrapped("On any phone or computer, open google.com/device and enter:");
        ImGui::PushFont(nullptr, ImGui::GetStyle().FontSizeBase * 2.2f);
        const float w = ImGui::CalcTextSize(g.user_code.c_str()).x;
        ImGui::SetCursorPosX((ImGui::GetWindowWidth() - w) * 0.5f);
        ImGui::TextUnformatted(g.user_code.c_str());
        ImGui::PopFont();
        if (ImGui::Button("Copy code")) ImGui::SetClipboardText(g.user_code.c_str());
        ImGui::SameLine();
        if (ImGui::Button("Open google.com/device")) open_url(g.url.empty() ? "https://www.google.com/device" : g.url);
    }
    if (!g.error.empty()) wrapped(kError, g.error);
    else hint(g.status);
    ImGui::Spacing();
    if (ImGui::Button(g.error.empty() ? "Cancel" : "Close")) {
        if (g.error.empty()) send({{"op", "google_cancel"}, {"req", g.req}});
        g.connecting = false;
        g.retry_task = 0;
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
}

bool Shell::draw_field(const std::string& owner, FormValues& form, const FieldSpec& f, std::set<std::string>* shown) {
    bool changed = false;
    ImGui::PushID(f.name.c_str());
    const ImGuiStyle& style = ImGui::GetStyle();
    if (f.kind == "toggle") {
        changed = ImGui::Checkbox(f.label.c_str(), &form.flags[f.name]);
    } else {
        ImGui::Text("%s%s", f.label.c_str(), f.required ? " *" : "");
        std::string& value = form.text[f.name];
        const float line = ImGui::GetTextLineHeight();
        auto button_w = [&](const char* label) { return ImGui::CalcTextSize(label).x + style.FramePadding.x * 2; };
        if (f.kind == "multiline") {
            changed = ImGui::InputTextMultiline("##v", &value, ImVec2(-FLT_MIN, line * std::clamp(f.lines, 2, 20) + 8));
        } else if (f.kind == "number") {
            changed = ImGui::InputText("##v", &value, ImGuiInputTextFlags_CharsDecimal);
        } else if (f.kind == "choice") {
            if (ImGui::BeginCombo("##v", value.c_str())) {
                for (const auto& c : f.choices)
                    if (ImGui::Selectable(c.c_str(), c == value)) { value = c; changed = true; }
                ImGui::EndCombo();
            }
        } else if (f.kind == "folder") {
            const float w = std::max(button_w("Add files..."), button_w("Browse..."));  // as wide as a file field's
            ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - w - style.ItemSpacing.x);
            changed = ImGui::InputText("##v", &value);
            ImGui::SameLine();
            if (ImGui::Button("Browse...", ImVec2(w, 0))) {
                Browse b{owner, f.name, false, shown != nullptr, false, {}};
                b.folder = true;
                browse_.push_back(b);
            }
        } else if (f.kind == "file" || f.kind == "files") {
            const bool multiple = f.kind == "files";
            const char* button = multiple ? "Add files..." : "Browse...";
            const float w = std::max(button_w("Add files..."), button_w("Browse..."));  // the same width for both
            ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - w - style.ItemSpacing.x);
            if (multiple) changed = ImGui::InputTextMultiline("##v", &value, ImVec2(0, line * 4 + 8));
            else changed = ImGui::InputText("##v", &value);
            ImGui::SameLine();
            if (ImGui::Button(button, ImVec2(w, 0))) browse_.push_back({owner, f.name, multiple, shown != nullptr, false, f.extensions});
        } else {
            // Text. Secret settings are masked, with Show; importable ones (a key file) also offer Import file.
            const bool masked = f.secret && !(shown && shown->count(f.name));
            float extra = 0;
            if (f.secret && shown) extra += ImGui::GetFrameHeight() + style.ItemInnerSpacing.x + ImGui::CalcTextSize("Show").x + style.ItemSpacing.x;
            if (f.importable && shown) extra += button_w("Import file...") + style.ItemSpacing.x;
            ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - extra);
            changed = ImGui::InputText("##v", &value, masked ? ImGuiInputTextFlags_Password : 0);
            if (f.secret && shown) {
                ImGui::SameLine();
                bool show = shown->count(f.name) > 0;
                if (ImGui::Checkbox("Show", &show)) { if (show) shown->insert(f.name); else shown->erase(f.name); }
            }
            if (f.importable && shown) {
                ImGui::SameLine();
                if (ImGui::Button("Import file...")) browse_.push_back({owner, f.name, false, true, true, {}});
            }
        }
    }
    if (!f.help.empty()) hint(f.help);
    ImGui::Spacing();
    ImGui::PopID();
    return changed;
}

void Shell::draw_run(const JobSpec& job, RunState& run) {
    ImGui::PushID(run.id);
    if (!run.done) {
        const float fraction = run.total ? static_cast<float>(run.step) / run.total : 0.0f;
        char overlay[160];
        std::snprintf(overlay, sizeof(overlay), "%s (%d of %d)", run.step_name.c_str(), run.step + 1, std::max(run.total, 1));
        ImGui::ProgressBar(fraction, ImVec2(-FLT_MIN, 0), overlay);
        ImGui::BeginDisabled(run.cancel_sent);
        if (ImGui::Button("Cancel")) {
            send({{"op", "cancel"}, {"run", run.id}});
            run.cancel_sent = true;
        }
        ImGui::EndDisabled();
        ImGui::PopID();
        return;
    }
    if (run.cancelled) ImGui::TextColored(kWarn, "Cancelled.");
    else if (!run.ok) wrapped(kError, run.error);

    const json& r = run.result;
    if (run.ok && r.is_object() && r.value("background", false)) {  // it only started a task: the task box says it all
        ImGui::PopID();
        return;
    }
    if (r.is_object()) {
        ImGui::SeparatorText(str(r, "title").empty() ? "Result" : str(r, "title").c_str());
        auto two_columns = [](const char* id, const json& rows, bool right_align) {
            if (!rows.is_array() || rows.empty()) return;
            if (ImGui::BeginTable(id, 2, ImGuiTableFlags_RowBg)) {
                ImGui::TableSetupColumn("label", ImGuiTableColumnFlags_WidthFixed);  // as wide as the longest label
                ImGui::TableSetupColumn("value", ImGuiTableColumnFlags_WidthStretch);
                for (const auto& row : rows) {
                    if (!row.is_array() || row.size() < 2) continue;
                    ImGui::TableNextRow();
                    ImGui::TableSetColumnIndex(0);
                    ImGui::TextDisabled("%s", row[0].get<std::string>().c_str());
                    ImGui::TableSetColumnIndex(1);
                    const std::string v = row[1].get<std::string>();
                    if (right_align)
                        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x - ImGui::CalcTextSize(v.c_str()).x);
                    ImGui::TextWrapped("%s", v.c_str());
                }
                ImGui::EndTable();
            }
        };
        two_columns("rows", r.value("rows", json::array()), false);
        if (r.contains("table") && r["table"].is_array() && !r["table"].empty()) {
            if (!str(r, "table_title").empty()) ImGui::SeparatorText(str(r, "table_title").c_str());
            two_columns("table", r["table"], true);
        }
        std::string text = str(r, "text");
        if (!text.empty()) {
            const int lines = static_cast<int>(std::count(text.begin(), text.end(), '\n')) + 1;
            const float h = ImGui::GetTextLineHeight() * std::clamp(lines, 2, 24) + 10;
            ImGui::InputTextMultiline("##text", &text, ImVec2(-FLT_MIN, h), ImGuiInputTextFlags_ReadOnly);
            if (ImGui::SmallButton("Copy")) ImGui::SetClipboardText(text.c_str());
        }
        for (const auto& image : strings(r, "images")) ImGui::TextDisabled("Image: %s", image.c_str());
        if (!str(r, "warning").empty()) wrapped(kWarn, str(r, "warning"));

        // The commit step: the run was a side-effect-free preview; this does the job's real work.
        if (flag(r, "has_payload") && !job.commit_label.empty()) {
            ImGui::Spacing();
            auto commit = [&](bool confirm) {
                run.commit = RunState::Commit::Committing;
                send({{"op", "commit"}, {"run", run.id}, {"settings", job_settings(job)}, {"confirm", confirm}});
            };
            switch (run.commit) {
                case RunState::Commit::Committed:
                    ImGui::TextColored(kOk, "%s: %s", job.committed_label.c_str(), run.commit_message.c_str());
                    break;
                case RunState::Commit::Committing:
                    ImGui::TextDisabled("Working...");
                    break;
                case RunState::Commit::Failed:
                    wrapped(run.confirm_label.empty() ? kError : kWarn, run.commit_message);
                    if (!run.confirm_label.empty() && ImGui::Button(run.confirm_label.c_str())) commit(true);
                    else if (run.confirm_label.empty() && ImGui::Button("Try again")) commit(false);
                    break;
                case RunState::Commit::Idle:
                    if (ImGui::Button(job.commit_label.c_str())) commit(false);
                    break;
            }
        }
    }
    if (run.ok) {
        if (run.seconds < 1) ImGui::TextDisabled("Done in %.0f ms", run.seconds * 1000);
        else ImGui::TextDisabled("Done in %.2f s", run.seconds);
    }
    ImGui::PopID();
}

// ---- a job's settings screen -----------------------------------------------------------------------------

void Shell::draw_job_settings(const JobSpec& job) {
    SettingsForm& sf = settings_form_;
    if (ImGui::Button("< Back")) { settings_job_id_.clear(); return; }
    ImGui::SameLine();
    ImGui::PushFont(nullptr, ImGui::GetStyle().FontSizeBase * 1.4f);
    ImGui::Text("Settings: %s", job.title.c_str());
    ImGui::PopFont();
    hint("Stored only on this PC. Keys and passwords go to Windows Credential Manager; the rest to " +
         to_utf8(store_.path().wstring()) + ".");
    std::vector<std::string> sharing;
    for (const auto& j : jobs_)
        if (j.id != job.id && j.settings_id == job.id) sharing.push_back(j.title);
    if (!sharing.empty()) {
        std::string list;
        for (const auto& t : sharing) list += (list.empty() ? "" : ", ") + t;
        hint("Also used by: " + list + ".");
    }
    if (!credential_error_.empty()) wrapped(kError, credential_error_);
    ImGui::Spacing();

    ImGui::PushItemWidth(-FLT_MIN);
    for (const auto& f : job.settings) draw_field(job.id, sf, f, &sf.shown);
    ImGui::PopItemWidth();

    const bool ready = host_state_ == HostState::Ready;
    for (size_t i = 0; i < job.settings_actions.size(); ++i) {
        ImGui::PushID(static_cast<int>(i));
        ImGui::BeginDisabled(sf.action_busy[i] || !ready);
        if (ImGui::Button(job.settings_actions[i].c_str())) {
            const int req = next_id_++;
            requests_[req] = {Request::Action, job.id, static_cast<int>(i)};
            send({{"op", "settings_action"}, {"req", req}, {"job", job.id}, {"index", i}, {"values", settings_form_values()}});
            sf.action_busy[i] = true;
            sf.action_text[i] = "Working...";
        }
        ImGui::EndDisabled();
        if (!sf.action_text[i].empty()) {
            std::string text = sf.action_text[i];
            const int lines = static_cast<int>(std::count(text.begin(), text.end(), '\n')) + 1;
            ImGui::InputTextMultiline("##found", &text, ImVec2(-FLT_MIN, ImGui::GetTextLineHeight() * std::clamp(lines, 1, 12) + 10),
                                      ImGuiInputTextFlags_ReadOnly);
        }
        ImGui::PopID();
    }
    ImGui::Spacing();

    auto ask = [&](bool save) {
        const int req = next_id_++;
        requests_[req] = {Request::Test, job.id, 0, save};
        send({{"op", "test_settings"}, {"req", req}, {"job", job.id}, {"values", settings_form_values()}});
        sf.busy = true;
    };
    ImGui::BeginDisabled(sf.busy || !ready);
    if (ImGui::Button("Save")) ask(true);
    ImGui::SetItemTooltip("Checks the settings first; settings that don't work are only saved after Save anyway.");
    ImGui::SameLine();
    if (ImGui::Button("Test settings")) ask(false);
    ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::Button("Reset to defaults")) {
        put_defaults(sf, job.settings);
        sf.tested = false;
    }
    ImGui::SetItemTooltip("Puts the defaults in the form; nothing is saved until Save.");
    if (!ready) {
        ImGui::SameLine();
        ImGui::TextDisabled("(the job host isn't running)");
    }

    if (sf.busy) ImGui::TextDisabled("Checking the settings...");
    else if (sf.tested) {
        ImGui::Spacing();
        if (sf.ok) ImGui::TextColored(kOk, "%s", sf.fixed ? "Everything checked out. Save to keep these settings." : "Everything checked out.");
        else wrapped(kError, sf.after_save ? "These settings don't work yet: nothing was saved, your saved settings are unchanged."
                                           : "These settings don't work yet:");
        for (const auto& m : sf.passed) ImGui::TextColored(kOk, "  OK  %s", m.c_str());
        for (const auto& p : sf.problems) wrapped(p.blocking ? kError : ImGui::GetStyleColorVec4(ImGuiCol_Text), (p.blocking ? "  X   " : "  -   ") + p.message);
        if (!sf.ok && sf.after_save && ImGui::Button("Save anyway")) {
            save_settings(job, settings_form_values());
            settings_job_id_.clear();
            notice_ = credential_error_.empty() ? "Settings saved (not working yet)." : "Settings saved, but not the keys: " + credential_error_;
            notice_color_ = credential_error_.empty() ? kWarn : kError;
        }
    }
}
