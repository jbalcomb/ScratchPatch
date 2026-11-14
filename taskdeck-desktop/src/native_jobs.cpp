#include "native_jobs.h"

#include "extract_job.h"
#include "sevenzip.h"

#include <chrono>
#include <deque>
#include <filesystem>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {
const char* const kExtract = "extract_archive";

using Clock = std::chrono::steady_clock;

double seconds_since(Clock::time_point t) { return std::chrono::duration<double>(Clock::now() - t).count(); }

std::string str(const json& j, const char* key) {
    const auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

json field(const char* name, const char* label, const char* kind) {
    return {{"name", name}, {"label", label}, {"kind", kind}, {"required", false}, {"default", nullptr},
            {"help", ""}, {"choices", json::array()}, {"secret", false}, {"lines", 3}, {"extensions", json::array()},
            {"grouped", ""}, {"importable", false}};
}
}  // namespace

// One extraction in the background, reported like the job host's tasks (core/tasks.py).
struct NativeJobs::Task {
    struct Phase {
        std::string label;
        uint64_t done = 0, total = 0;
    };
    int id = 0, run = 0;
    std::string title, dest;
    json inputs;
    std::atomic<bool> cancel{false};
    std::mutex m;  // guards everything below
    std::vector<Phase> phases;
    std::string status = "running", message;
    Clock::time_point last_emit{};
    int speed_phase = -1;
    std::deque<std::pair<Clock::time_point, uint64_t>> samples;  // the last few seconds of the current phase
};

NativeJobs::~NativeJobs() { stop(); }

void NativeJobs::start(HWND notify) {
    notify_ = notify;
    stopping_ = false;
    wchar_t exe[MAX_PATH];
    const DWORD n = ::GetModuleFileNameW(nullptr, exe, MAX_PATH);
    const fs::path dll = fs::path(std::wstring(exe, n)).parent_path() / L"7z.dll";
    std::string why;
    if (!sevenzip::load(dll.wstring(), &why))
        engine_error_ = "The archive engine (" + extract::utf8(dll) + ") can't be loaded: " + why + ".";
}

json NativeJobs::jobs() const {
    json archive = field("archive", "Archive", "file");
    archive["required"] = true;
    archive["extensions"] = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tbz2", ".xz", ".txz",
                             ".zst", ".lzma", ".cab", ".iso", ".jar", ".apk", ".zipx", ".001"};
    json dest = field("destination", "Extract into (folder)", "folder");
    dest["help"] = "Leave empty for a new folder next to the archive.";
    json password = field("password", "Password (if encrypted)", "text");
    password["secret"] = true;
    json job = {
        {"id", kExtract}, {"title", "Extract archive"},
        {"description", "Unpack zip, 7z, RAR, tar and gz files, password-protected too, into a folder on this PC."},
        {"category", "Tools"},
        {"tags", {"unzip", "zip", "7z", "7-zip", "rar", "unrar", "tar", "gz", "extract", "archive", "decompress"}},
        {"fields", {archive, dest, password}}, {"settings", json::array()}, {"settings_id", kExtract},
        {"settings_actions", json::array()}, {"run_label", "Extract"}, {"accounts", json::array()},
        {"commit_label", ""}, {"committed_label", ""}, {"quick_actions", json::array()}};
    return json::array({job});
}

bool NativeJobs::has_job(const std::string& id) const { return id == kExtract; }

bool NativeJobs::handle(const json& msg) {
    const std::string op = str(msg, "op");
    const bool for_job = has_job(str(msg, "job"));
    if (op == "check" && for_job) {
        json problems = json::array();
        if (!engine_error_.empty())
            problems.push_back({{"message", engine_error_}, {"blocking", true}, {"fix", "recheck"}, {"ok", false}});
        push({{"reply", msg.value("req", 0)}, {"problems", problems}, {"fixes", json::object()}});
        return true;
    }
    if ((op == "test_settings" || op == "settings_action") && for_job) {  // no settings (yet)
        push({{"reply", msg.value("req", 0)}, {"settings", msg.value("values", json::object())}, {"passed", json::array()},
              {"problems", json::array()}, {"fixes", json::object()}, {"text", ""}, {"values", json::object()}});
        return true;
    }
    if (op == "run" && for_job) {
        const int id = msg.value("run", 0);
        {
            std::lock_guard lock(mutex_);
            runs_.insert(id);
            run_cancel_[id] = std::make_shared<std::atomic<bool>>(false);
        }
        const json inputs = msg.value("inputs", json::object());
        spawn([this, id, inputs] { run(id, inputs); });
        return true;
    }
    const int run_id = msg.value("run", 0);
    if (op == "cancel" || op == "commit" || op == "forget") {
        std::lock_guard lock(mutex_);
        if (!runs_.count(run_id)) return false;
        if (op == "cancel" && run_cancel_.count(run_id)) *run_cancel_[run_id] = true;
        if (op == "commit")
            inbox_.push_back({{"event", "committed"}, {"run", run_id}, {"ok", false},
                              {"message", "Nothing to commit for this run."}, {"confirm_label", nullptr}});
        return true;
    }
    if (op == "task_cancel" || op == "task_forget" || op == "task_retry") {
        const int task_id = msg.value("task", 0);
        std::lock_guard lock(mutex_);
        const auto it = tasks_.find(task_id);
        if (it == tasks_.end()) return task_id >= 1000000;  // one of ours, already gone
        if (op == "task_cancel") it->second->cancel = true;
        else if (op == "task_forget") {
            std::lock_guard tl(it->second->m);
            if (it->second->status != "running") tasks_.erase(it);
        }
        return true;  // no retry: Extract again from the job's screen
    }
    return false;
}

std::vector<json> NativeJobs::take() {
    std::lock_guard lock(mutex_);
    std::vector<json> out;
    out.swap(inbox_);
    return out;
}

void NativeJobs::push(json msg) {
    {
        std::lock_guard lock(mutex_);
        inbox_.push_back(std::move(msg));
    }
    if (notify_) ::PostMessageW(notify_, WM_APP, 0, 0);
}

void NativeJobs::spawn(std::function<void()> fn) {
    std::lock_guard lock(mutex_);
    threads_.emplace_back(std::move(fn));
}

void NativeJobs::stop() {
    stopping_ = true;
    std::vector<std::thread> threads;
    {
        std::lock_guard lock(mutex_);
        for (auto& [id, c] : run_cancel_) *c = true;
        for (auto& [id, t] : tasks_) t->cancel = true;
        threads.swap(threads_);
    }
    for (auto& t : threads)
        if (t.joinable()) t.join();
    notify_ = nullptr;
}

// The run: check the archive and the destination (the phone's "check archive" step), then start the task.
void NativeJobs::run(int run_id, const json& inputs) {
    const auto started = Clock::now();
    auto done = [&](bool ok, const std::string& error, json result) {
        push({{"event", "done"}, {"run", run_id}, {"ok", ok}, {"cancelled", false}, {"error", error.empty() ? json() : json(error)},
              {"seconds", seconds_since(started)}, {"result", result}, {"steps", json::array()}});
        std::lock_guard lock(mutex_);
        run_cancel_.erase(run_id);
    };
    push({{"event", "progress"}, {"run", run_id}, {"step", 0}, {"total", 2}, {"name", "check archive"}});
    const std::string archive = str(inputs, "archive"), password = str(inputs, "password");
    if (!engine_error_.empty()) return done(false, engine_error_, nullptr);
    if (archive.empty()) return done(false, "Choose an archive.", nullptr);
    const fs::path path = extract::path_from_utf8(archive);
    std::error_code ec;
    if (!fs::is_regular_file(path, ec)) return done(false, "File not found: " + archive, nullptr);
    const auto scan = extract::scan(path, password.empty() ? std::nullopt : std::optional<std::string>(password));
    const fs::path dest = extract::destination(path, str(inputs, "destination"));
    const auto problems = extract::problems(scan, dest, password);
    if (!problems.empty()) {
        std::string error;
        for (const auto& p : problems) error += (error.empty() ? "" : " ") + p;
        return done(false, error, nullptr);
    }
    {
        std::lock_guard lock(mutex_);
        const auto it = run_cancel_.find(run_id);
        if (it != run_cancel_.end() && *it->second) {
            run_cancel_.erase(it);
            inbox_.push_back({{"event", "done"}, {"run", run_id}, {"ok", false}, {"cancelled", true}, {"error", nullptr},
                              {"seconds", seconds_since(started)}, {"result", nullptr}, {"steps", json::array()}});
            return;
        }
    }
    push({{"event", "progress"}, {"run", run_id}, {"step", 1}, {"total", 2}, {"name", "extract"}});
    json plain = inputs;
    plain.erase("password");  // what the task shows and History keeps
    start_task(run_id, archive, extract::utf8(dest), password, plain);
    std::string what = scan.compressed_tar ? scan.format
                       : extract::thousands(static_cast<uint64_t>(scan.files)) + (scan.files == 1 ? " file" : " files");
    if (scan.unpacked) what += ", " + extract::human_size(static_cast<double>(scan.unpacked));
    const std::string text = "Extracting " + extract::utf8(path.filename()) + " (" + what + ") into " +
                             extract::utf8(dest) + ". It goes on if you leave this screen.";
    done(true, "", {{"title", "Extracting"}, {"text", text}, {"rows", json::array()}, {"warning", nullptr},
                    {"table_title", ""}, {"table", json::array()}, {"images", json::array()}, {"has_payload", false},
                    {"summary", "Extracting " + extract::utf8(path.filename())}, {"background", true}});
}

void NativeJobs::start_task(int run_id, const std::string& archive, const std::string& dest, const std::string& password,
                            const json& inputs) {
    auto t = std::make_shared<Task>();
    t->run = run_id;
    t->title = "Extracting " + extract::utf8(extract::path_from_utf8(archive).filename());
    t->dest = dest;
    t->inputs = inputs;
    {
        std::lock_guard lock(mutex_);
        t->id = next_task_++;
        tasks_[t->id] = t;
    }
    emit(*t, true);
    spawn([this, t, archive, dest, password] {
        extract::Reporter r;
        r.set_phases = [&](const std::vector<std::pair<std::string, uint64_t>>& phases) {
            {
                std::lock_guard lock(t->m);
                t->phases.clear();
                for (const auto& [label, total] : phases) t->phases.push_back({label, 0, total});
            }
            emit(*t, true);
        };
        r.advance = [&](int phase, uint64_t done, uint64_t total) {
            {
                std::lock_guard lock(t->m);
                if (phase < 0 || phase >= static_cast<int>(t->phases.size())) return;
                for (int i = 0; i < phase; ++i) t->phases[i].done = t->phases[i].total;  // earlier phases are done
                auto& p = t->phases[phase];
                p.done = done;
                if (total) p.total = total;
                if (t->speed_phase != phase) {
                    t->speed_phase = phase;
                    t->samples.clear();
                }
                const auto now = Clock::now();
                t->samples.emplace_back(now, done);
                while (t->samples.size() > 2 && now - t->samples.front().first > std::chrono::seconds(5))
                    t->samples.pop_front();
            }
            emit(*t, false);
        };
        r.cancelled = [&] { return t->cancel.load() || stopping_.load(); };
        const auto outcome = extract::run(extract::path_from_utf8(archive), extract::path_from_utf8(dest),
                                          password.empty() ? std::nullopt : std::optional<std::string>(password), r);
        {
            std::lock_guard lock(t->m);
            t->status = outcome.status == extract::Outcome::Done ? "done"
                        : outcome.status == extract::Outcome::Cancelled ? "cancelled" : "failed";
            t->message = outcome.message;
        }
        emit(*t, true);
    });
}

// A "task" event, as task_json in the job host writes it. While it moves, at most 4 a second.
void NativeJobs::emit(Task& t, bool force) {
    json msg;
    {
        std::lock_guard lock(t.m);
        const auto now = Clock::now();
        if (!force && now - t.last_emit < std::chrono::milliseconds(250)) return;
        t.last_emit = now;
        json phases = json::array();
        double sum = 0;
        uint64_t done = 0, total = 0;
        int current = -1;
        for (size_t i = 0; i < t.phases.size(); ++i) {
            const auto& p = t.phases[i];
            const double f = p.total ? std::min(1.0, static_cast<double>(p.done) / static_cast<double>(p.total)) : 0.0;
            sum += f;
            if (current < 0 && !(p.total && p.done >= p.total)) {
                current = static_cast<int>(i);
                done = p.done;
                total = p.total;
            }
            phases.push_back({{"label", p.label}, {"done", p.done}, {"total", p.total}, {"unit", "bytes"},
                              {"fraction", p.total ? json(f) : json()}});
        }
        json speed, eta;
        if (current >= 0 && current == t.speed_phase && t.samples.size() >= 2) {
            const double span = std::chrono::duration<double>(t.samples.back().first - t.samples.front().first).count();
            if (span >= 1) {
                const double s = static_cast<double>(t.samples.back().second - t.samples.front().second) / span;
                speed = s;
                if (s > 0 && total > done) eta = static_cast<double>(total - done) / s;
            }
        }
        msg = {{"event", "task"}, {"id", t.id}, {"run", t.run}, {"job", kExtract}, {"title", t.title}, {"status", t.status},
               {"done", done}, {"total", total}, {"fraction", t.phases.empty() ? json() : json(sum / t.phases.size())},
               {"eta", eta}, {"speed", speed}, {"note", ""}, {"message", t.message}, {"fix", ""}, {"phases", phases},
               {"retry_label", ""}, {"drive_url", nullptr}, {"inputs", t.inputs},
               {"open_path", t.status == "done" ? json(t.dest) : json()}};
    }
    push(std::move(msg));
}
