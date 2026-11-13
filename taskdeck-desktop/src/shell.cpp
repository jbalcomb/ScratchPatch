#include "shell.h"

#include "file_dialog.h"
#include "imgui.h"
#include "imgui_stdlib.h"

#include <algorithm>
#include <cctype>
#include <cstdio>

using nlohmann::json;

namespace {
std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string str(const json& j, const char* key) {
    const auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

bool flag(const json& j, const char* key) {
    const auto it = j.find(key);
    return it != j.end() && it->is_boolean() && it->get<bool>();
}

std::vector<std::string> strings(const json& j, const char* key) {
    std::vector<std::string> out;
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
    for (const auto& f : j.value("fields", json::array())) job.fields.push_back(parse_field(f));
    for (const auto& f : j.value("settings", json::array())) job.settings.push_back(parse_field(f));
    job.search = lower(job.title + " " + job.category + " " + job.description);
    for (const auto& t : strings(j, "tags")) job.search += " " + lower(t);
    return job;
}

std::string default_text(const json& def) {
    if (def.is_string()) return def.get<std::string>();
    if (def.is_number_integer()) return std::to_string(def.get<long long>());
    if (def.is_number()) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%g", def.get<double>());
        return buf;
    }
    return {};
}

std::string now_hms() {
    SYSTEMTIME t;
    ::GetLocalTime(&t);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%02d:%02d:%02d", t.wHour, t.wMinute, t.wSecond);
    return buf;
}

const ImVec4 kWarn(0.95f, 0.75f, 0.30f, 1.0f), kError(0.95f, 0.45f, 0.40f, 1.0f), kOk(0.45f, 0.85f, 0.50f, 1.0f);

// Pinned on Home until pins can be chosen (and saved) in the app: the first desktop jobs, then a few handy ones.
const char* const kDefaultPins[] = {"extract_archive", "exe_parse", "dll_parse", "pdf_parser", "json_format", "hash_text"};
}  // namespace

void Shell::start(HWND hwnd) {
    hwnd_ = hwnd;
    host_state_ = HostState::Starting;
    host_error_.clear();
    if (!host_.start(hwnd, host_error_)) host_state_ = HostState::Failed;
}

void Shell::pump() {
    for (const auto& msg : host_.take()) on_message(msg);
}

void Shell::on_message(const json& msg) {
    const std::string event = str(msg, "event");
    if (event == "ready") {
        host_state_ = HostState::Ready;
        python_version_ = str(msg, "python");
        jobs_.clear();
        for (const auto& j : msg.value("jobs", json::array())) jobs_.push_back(parse_job(j));
        load_errors_.clear();
        for (const auto& [k, v] : msg.value("errors", json::object()).items())
            load_errors_[k] = v.is_string() ? v.get<std::string>() : v.dump();
        for (auto& [id, form] : forms_) form.checked = false;  // check again against the new host
    } else if (event == "fatal") {
        host_state_ = HostState::Failed;
        host_error_ = str(msg, "error");
    } else if (event == "exited") {
        if (host_state_ != HostState::Failed) {
            host_state_ = HostState::Failed;
            host_error_ = "The job host stopped (exit code " + std::to_string(msg.value("code", 0)) + ").";
        }
        for (auto& [id, run] : runs_)
            if (!run.done) { run.done = true; run.error = "The job host stopped while this ran."; }
    } else if (event == "garbled") {
        load_errors_["(job host output)"] = str(msg, "line");
    } else if (msg.contains("reply")) {
        const int req = msg.value("reply", 0);
        if (auto it = checks_.find(req); it != checks_.end()) {
            JobForm& form = forms_[it->second];
            form.problems.clear();
            for (const auto& p : msg.value("problems", json::array()))
                if (!flag(p, "ok")) form.problems.push_back({str(p, "message"), str(p, "fix"), flag(p, "blocking")});
            checks_.erase(it);
        }
    } else if (event == "progress" || event == "done" || event == "committed") {
        auto it = runs_.find(msg.value("run", 0));
        if (it == runs_.end()) return;
        RunState& run = it->second;
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
        } else {
            run.commit_message = str(msg, "message");
            run.confirm_label = str(msg, "confirm_label");
            run.commit = flag(msg, "ok") ? RunState::Commit::Committed : RunState::Commit::Failed;
        }
    }
}

const JobSpec* Shell::find_job(const std::string& id) const {
    for (const auto& j : jobs_)
        if (j.id == id) return &j;
    return nullptr;
}

JobForm& Shell::form_for(const JobSpec& job) {
    JobForm& form = forms_[job.id];
    if (!form.filled) {
        form.filled = true;
        for (const auto& f : job.fields) {
            if (f.kind == "toggle") form.flags[f.name] = f.def.is_boolean() && f.def.get<bool>();
            else form.text[f.name] = default_text(f.def);
        }
    }
    if (!form.checked && host_state_ == HostState::Ready) {
        // Job settings aren't edited or saved yet (next step), so the checks see the defaults.
        const int req = next_id_++;
        checks_[req] = job.id;
        host_.send({{"op", "check"}, {"req", req}, {"job", job.id}, {"settings", json::object()}});
        form.checked = true;
    }
    return form;
}

void Shell::open_job(const std::string& id) {
    open_job_id_ = id;
    palette_open_ = false;
    if (find_job(id)) forms_[id].checked = false;  // check again each time it opens
}

void Shell::run_job(const JobSpec& job, JobForm& form) {
    if (auto old = runs_.find(form.run_id); old != runs_.end() && old->second.done &&
        old->second.commit != RunState::Commit::Committed && flag(old->second.result, "has_payload"))
        host_.send({{"op", "forget"}, {"run", form.run_id}});  // the previous preview won't be committed now
    json inputs = json::object();
    for (const auto& f : job.fields) {
        if (f.kind == "toggle") inputs[f.name] = form.flags[f.name];
        else inputs[f.name] = form.text[f.name];
    }
    RunState run;
    run.id = next_id_++;
    run.job_id = job.id;
    run.job_title = job.title;
    run.started = now_hms();
    runs_[run.id] = run;
    history_.insert(history_.begin(), run.id);
    form.run_id = run.id;
    host_.send({{"op", "run"}, {"run", run.id}, {"job", job.id}, {"inputs", inputs}, {"settings", json::object()}});
}

bool Shell::draw(const PerfSnapshot& perf) {
    ImGuiIO& io = ImGui::GetIO();
    if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_K)) {
        palette_open_ = !palette_open_;
        palette_focus_ = palette_open_;
        palette_query_[0] = 0;
    }

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->WorkPos);
    ImGui::SetNextWindowSize(vp->WorkSize);
    ImGui::Begin("TaskDeck", nullptr, ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                                          ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoSavedSettings);

    // Navigation on the left, like a desktop app (the phone has it at the bottom).
    ImGui::BeginChild("nav", ImVec2(180 * ImGui::GetStyle().FontScaleDpi, 0), ImGuiChildFlags_Borders);
    const struct { Tab tab; const char* label; } items[] = {
        {Tab::Home, "Home"}, {Tab::Jobs, "All jobs"}, {Tab::History, "History"}, {Tab::Settings, "Settings"}};
    for (const auto& it : items)
        if (ImGui::Selectable(it.label, tab_ == it.tab && open_job_id_.empty())) { tab_ = it.tab; open_job_id_.clear(); }
    ImGui::Separator();
    if (ImGui::Button("Search  (Ctrl+K)")) { palette_open_ = true; palette_focus_ = true; }
    ImGui::EndChild();
    ImGui::SameLine();

    ImGui::BeginChild("page");
    const JobSpec* job = open_job_id_.empty() ? nullptr : find_job(open_job_id_);
    if (job) draw_job(*job);
    else switch (tab_) {
        case Tab::Home: draw_home(); break;
        case Tab::Jobs: draw_jobs(); break;
        case Tab::History: draw_history(); break;
        case Tab::Settings: draw_settings(perf); break;
    }
    ImGui::EndChild();
    ImGui::End();

    if (palette_open_) draw_palette();
    if (show_demo_) ImGui::ShowDemoWindow(&show_demo_);
    return palette_focus_ || io.MouseDown[0];
}

void Shell::after_frame() {
    auto pending = std::move(browse_);
    browse_.clear();
    for (const auto& b : pending) {
        const auto paths = pick_files(hwnd_, b.multiple, b.extensions);
        if (paths.empty()) continue;
        std::string& value = forms_[b.job_id].text[b.field];
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
        ImGui::PushStyleColor(ImGuiCol_Text, kError);
        ImGui::TextWrapped("The jobs can't run: %s", host_error_.c_str());
        ImGui::PopStyleColor();
        if (ImGui::Button("Start the job host again")) start(hwnd_);
    }
}

void Shell::draw_home() {
    draw_host_status();
    ImGui::TextUnformatted("Pinned");
    ImGui::Separator();
    const float tile = 150 * ImGui::GetStyle().FontScaleDpi;
    const float avail = ImGui::GetContentRegionAvail().x;
    const int per_row = std::max(1, static_cast<int>(avail / (tile + ImGui::GetStyle().ItemSpacing.x)));
    int shown = 0;
    for (const char* id : kDefaultPins) {
        const JobSpec* job = find_job(id);
        if (!job) continue;
        if (shown++ % per_row) ImGui::SameLine();
        ImGui::PushID(id);
        ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.5f, 0.5f));
        if (ImGui::Button(job->title.c_str(), ImVec2(tile, tile * 0.6f))) open_job(job->id);
        ImGui::PopStyleVar();
        if (ImGui::IsItemHovered()) ImGui::SetItemTooltip("%s", job->description.c_str());
        ImGui::PopID();
    }
}

void Shell::draw_jobs() {
    draw_host_status();
    if (ImGui::BeginTable("jobs", 3, ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_Resizable |
                                         ImGuiTableFlags_ScrollY)) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Job", ImGuiTableColumnFlags_WidthFixed, 200 * ImGui::GetStyle().FontScaleDpi);
        ImGui::TableSetupColumn("Category", ImGuiTableColumnFlags_WidthFixed, 110 * ImGui::GetStyle().FontScaleDpi);
        ImGui::TableSetupColumn("What it does", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableHeadersRow();
        for (const auto& job : jobs_) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::PushID(job.id.c_str());
            if (ImGui::Selectable(job.title.c_str(), false, ImGuiSelectableFlags_SpanAllColumns)) open_job(job.id);
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
    if (history_.empty()) {
        ImGui::TextDisabled("No runs yet. Every job you run is listed here so you can go back to its result.");
        return;
    }
    if (ImGui::BeginTable("history", 3, ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH)) {
        ImGui::TableSetupColumn("Time", ImGuiTableColumnFlags_WidthFixed, 80 * ImGui::GetStyle().FontScaleDpi);
        ImGui::TableSetupColumn("Job", ImGuiTableColumnFlags_WidthFixed, 200 * ImGui::GetStyle().FontScaleDpi);
        ImGui::TableSetupColumn("Outcome", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableHeadersRow();
        for (int id : history_) {
            const RunState& run = runs_[id];
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::PushID(id);
            if (ImGui::Selectable(run.started.c_str(), false, ImGuiSelectableFlags_SpanAllColumns) && find_job(run.job_id)) {
                forms_[run.job_id].run_id = id;
                open_job(run.job_id);
            }
            ImGui::PopID();
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(run.job_title.c_str());
            ImGui::TableSetColumnIndex(2);
            if (!run.done) ImGui::TextDisabled("Running...");
            else if (run.ok) ImGui::TextColored(kOk, "Done in %.2f s", run.seconds);
            else ImGui::TextColored(run.cancelled ? kWarn : kError, "%s", run.cancelled ? "Cancelled" : run.error.c_str());
        }
        ImGui::EndTable();
    }
}

void Shell::draw_settings(const PerfSnapshot& perf) {
    ImGui::SeparatorText("Jobs");
    const char* state = host_state_ == HostState::Ready ? "running" : host_state_ == HostState::Starting ? "starting" : "stopped";
    ImGui::Text("Job host:      %s, Python %s, %d jobs", state, python_version_.c_str(), static_cast<int>(jobs_.size()));
    if (!host_error_.empty()) ImGui::TextColored(kError, "%s", host_error_.c_str());
    for (const auto& [name, error] : load_errors_) ImGui::TextColored(kWarn, "Not loaded: %s: %s", name.c_str(), error.c_str());
    ImGui::TextDisabled("Log: %s", to_utf8(host_.log_path()).c_str());
    if (ImGui::Button("Restart the job host")) start(hwnd_);

    ImGui::SeparatorText("Performance (for comparing with the WinUI 3 build)");
    ImGui::Text("First frame:   %.0f ms after start", perf.first_frame_ms);
    ImGui::Text("Frame time:    %.2f ms average, %.2f ms worst (last 120 frames)", perf.frame_ms_avg, perf.frame_ms_max);
    ImGui::Text("CPU:           %.1f %% of one core", perf.cpu_percent);
    ImGui::Text("Memory:        %.1f MB working set, %.1f MB private", perf.working_set_mb, perf.private_mb);
    ImGui::Text("Frames drawn:  %llu", static_cast<unsigned long long>(perf.frames));
    ImGui::Spacing();
    ImGui::Checkbox("Show the Dear ImGui demo window", &show_demo_);
}

void Shell::draw_job(const JobSpec& job) {
    JobForm& form = form_for(job);
    if (ImGui::Button("< Back")) { open_job_id_.clear(); return; }
    ImGui::SameLine();
    ImGui::PushFont(nullptr, ImGui::GetStyle().FontSizeBase * 1.4f);
    ImGui::TextUnformatted(job.title.c_str());
    ImGui::PopFont();
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    ImGui::TextWrapped("%s", job.description.c_str());
    ImGui::PopStyleColor();

    bool blocked = false;
    for (const auto& p : form.problems) {
        blocked |= p.blocking;
        ImGui::PushStyleColor(ImGuiCol_Text, p.blocking ? kError : kWarn);
        ImGui::TextWrapped("%s", p.message.c_str());
        ImGui::PopStyleColor();
    }
    if (!form.problems.empty() && ImGui::SmallButton("Check again")) form.checked = false;
    ImGui::Spacing();

    ImGui::PushItemWidth(-FLT_MIN);
    for (const auto& f : job.fields) draw_field(job, form, f);
    ImGui::PopItemWidth();
    ImGui::Spacing();

    auto run_it = runs_.find(form.run_id);
    const bool running = run_it != runs_.end() && !run_it->second.done;
    ImGui::BeginDisabled(blocked || running || host_state_ != HostState::Ready);
    if (ImGui::Button(job.run_label.empty() ? "Run" : job.run_label.c_str())) run_job(job, form);
    ImGui::EndDisabled();
    if (blocked && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetItemTooltip("Fix the problems above first.");

    run_it = runs_.find(form.run_id);  // run_job may have started a new one
    if (run_it != runs_.end()) {
        ImGui::Spacing();
        draw_run(job, run_it->second);
    }
}

void Shell::draw_field(const JobSpec& job, JobForm& form, const FieldSpec& f) {
    ImGui::PushID(f.name.c_str());
    if (f.kind == "toggle") {
        ImGui::Checkbox(f.label.c_str(), &form.flags[f.name]);
    } else {
        ImGui::Text("%s%s", f.label.c_str(), f.required ? " *" : "");
        std::string& value = form.text[f.name];
        const float line = ImGui::GetTextLineHeight();
        if (f.kind == "multiline") {
            ImGui::InputTextMultiline("##v", &value, ImVec2(-FLT_MIN, line * std::clamp(f.lines, 2, 20) + 8));
        } else if (f.kind == "number") {
            ImGui::InputText("##v", &value, ImGuiInputTextFlags_CharsDecimal);
        } else if (f.kind == "choice") {
            if (ImGui::BeginCombo("##v", value.c_str())) {
                for (const auto& c : f.choices)
                    if (ImGui::Selectable(c.c_str(), c == value)) value = c;
                ImGui::EndCombo();
            }
        } else if (f.kind == "file" || f.kind == "files") {
            const bool multiple = f.kind == "files";
            const char* button = multiple ? "Add files..." : "Browse...";
            const float button_w = ImGui::CalcTextSize(button).x + ImGui::GetStyle().FramePadding.x * 2;
            ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - button_w - ImGui::GetStyle().ItemSpacing.x);
            if (multiple) ImGui::InputTextMultiline("##v", &value, ImVec2(0, line * 4 + 8));
            else ImGui::InputText("##v", &value);
            ImGui::SameLine();
            if (ImGui::Button(button)) browse_.push_back({job.id, f.name, multiple, f.extensions});
        } else {
            ImGui::InputText("##v", &value, f.secret ? ImGuiInputTextFlags_Password : 0);
        }
    }
    if (!f.help.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
        ImGui::TextWrapped("%s", f.help.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();
    ImGui::PopID();
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
            host_.send({{"op", "cancel"}, {"run", run.id}});
            run.cancel_sent = true;
        }
        ImGui::EndDisabled();
        ImGui::PopID();
        return;
    }
    if (run.cancelled) ImGui::TextColored(kWarn, "Cancelled.");
    else if (!run.ok) {
        ImGui::PushStyleColor(ImGuiCol_Text, kError);
        ImGui::TextWrapped("%s", run.error.c_str());
        ImGui::PopStyleColor();
    }

    const json& r = run.result;
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
        if (!str(r, "warning").empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, kWarn);
            ImGui::TextWrapped("%s", str(r, "warning").c_str());
            ImGui::PopStyleColor();
        }

        // The commit step: the run was a side-effect-free preview; this does the job's real work.
        if (flag(r, "has_payload") && !job.commit_label.empty()) {
            ImGui::Spacing();
            switch (run.commit) {
                case RunState::Commit::Committed:
                    ImGui::TextColored(kOk, "%s: %s", job.committed_label.c_str(), run.commit_message.c_str());
                    break;
                case RunState::Commit::Committing:
                    ImGui::TextDisabled("Working...");
                    break;
                case RunState::Commit::Failed:
                    ImGui::PushStyleColor(ImGuiCol_Text, run.confirm_label.empty() ? kError : kWarn);
                    ImGui::TextWrapped("%s", run.commit_message.c_str());
                    ImGui::PopStyleColor();
                    if (!run.confirm_label.empty() && ImGui::Button(run.confirm_label.c_str())) {
                        run.commit = RunState::Commit::Committing;
                        host_.send({{"op", "commit"}, {"run", run.id}, {"settings", json::object()}, {"confirm", true}});
                    }
                    if (run.confirm_label.empty() && ImGui::Button("Try again")) {
                        run.commit = RunState::Commit::Committing;
                        host_.send({{"op", "commit"}, {"run", run.id}, {"settings", json::object()}});
                    }
                    break;
                case RunState::Commit::Idle:
                    if (ImGui::Button(job.commit_label.c_str())) {
                        run.commit = RunState::Commit::Committing;
                        host_.send({{"op", "commit"}, {"run", run.id}, {"settings", json::object()}});
                    }
                    break;
            }
        }
    }
    if (run.ok) ImGui::TextDisabled("Done in %.2f s", run.seconds);
    ImGui::PopID();
}

void Shell::draw_palette() {
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f, vp->WorkPos.y + vp->WorkSize.y * 0.15f),
                            ImGuiCond_Always, ImVec2(0.5f, 0));
    ImGui::SetNextWindowSize(ImVec2(vp->WorkSize.x * 0.5f, 0));
    if (ImGui::Begin("Command palette", &palette_open_, ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        if (palette_focus_) { ImGui::SetKeyboardFocusHere(); palette_focus_ = false; }
        const bool enter = ImGui::InputTextWithHint("##q", "Type a job or command...", palette_query_,
                                                    sizeof(palette_query_), ImGuiInputTextFlags_EnterReturnsTrue);
        const std::string query = lower(palette_query_);
        const JobSpec* first = nullptr;
        int shown = 0;
        for (const auto& job : jobs_) {
            if (!query.empty() && job.search.find(query) == std::string::npos) continue;
            if (!first) first = &job;
            if (++shown > 12) break;
            ImGui::PushID(job.id.c_str());
            if (ImGui::Selectable(job.title.c_str())) open_job(job.id);
            ImGui::PopID();
            ImGui::SameLine();
            ImGui::TextDisabled("%s", job.category.c_str());
        }
        if (enter && first) open_job(first->id);
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) palette_open_ = false;
    }
    ImGui::End();
}
