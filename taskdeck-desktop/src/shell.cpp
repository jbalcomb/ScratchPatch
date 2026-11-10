#include "shell.h"

#include "imgui.h"

#include <algorithm>
#include <cctype>

namespace {
bool contains_ci(const std::string& hay, const char* needle) {
    std::string a = hay, b = needle;
    std::transform(a.begin(), a.end(), a.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    std::transform(b.begin(), b.end(), b.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return a.find(b) != std::string::npos;
}
}  // namespace

Shell::Shell() {
    // The phone app's built-in jobs (taskdeck/src/taskdeck/jobs), as placeholders.
    jobs_ = {
        {"daily_log", "Daily Log to Sheet", "Work", "Paste a delivery log; the AI Workstation extracts it into the sheet."},
        {"extract_archive", "Extract archive", "Tools", "Unpack zip, 7z, RAR, tar and gz, to a folder or Google Drive."},
        {"exe_parse", "Parse executable", "Tools", "Split an .exe into small files for an AI agent."},
        {"unit_convert", "Unit Converter", "Everyday", "Length, weight, temperature..."},
        {"tip_split", "Tip & Split", "Everyday", "Split a bill with tip."},
        {"text_stats", "Text stats", "Text", "Words, characters, reading time."},
        {"case_convert", "Change case", "Text", "UPPER, lower, Title, snake_case..."},
        {"json_format", "Format JSON", "Text", "Pretty-print or minify JSON."},
        {"hash_text", "Hash text", "Text", "MD5, SHA-1, SHA-256."},
    };
}

void Shell::open_job(const JobEntry& job) {
    open_job_id_ = job.id;
    history_.insert(history_.begin(), job.title);
    palette_open_ = false;
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
        if (ImGui::Selectable(it.label, tab_ == it.tab)) tab_ = it.tab;
    ImGui::Separator();
    if (ImGui::Button("Search  (Ctrl+K)")) { palette_open_ = true; palette_focus_ = true; }
    ImGui::EndChild();
    ImGui::SameLine();

    ImGui::BeginChild("page");
    switch (tab_) {
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

void Shell::draw_home() {
    ImGui::TextUnformatted("Pinned");
    ImGui::Separator();
    const float tile = 140 * ImGui::GetStyle().FontScaleDpi;
    const float avail = ImGui::GetContentRegionAvail().x;
    int per_row = std::max(1, static_cast<int>(avail / (tile + ImGui::GetStyle().ItemSpacing.x)));
    for (size_t i = 0; i < std::min<size_t>(jobs_.size(), 6); ++i) {
        if (i % per_row) ImGui::SameLine();
        if (ImGui::Button(jobs_[i].title.c_str(), ImVec2(tile, tile * 0.6f))) open_job(jobs_[i]);
    }
    if (!open_job_id_.empty()) {
        ImGui::Spacing();
        ImGui::Text("Opened: %s (job screens come once the job model is decided)", open_job_id_.c_str());
    }
}

void Shell::draw_jobs() {
    if (ImGui::BeginTable("jobs", 3, ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_Resizable)) {
        ImGui::TableSetupColumn("Job");
        ImGui::TableSetupColumn("Category");
        ImGui::TableSetupColumn("What it does", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableHeadersRow();
        for (const auto& job : jobs_) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            if (ImGui::Selectable(job.title.c_str(), false, ImGuiSelectableFlags_SpanAllColumns)) open_job(job);
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(job.category.c_str());
            ImGui::TableSetColumnIndex(2);
            ImGui::TextUnformatted(job.description.c_str());
        }
        ImGui::EndTable();
    }
}

void Shell::draw_history() {
    if (history_.empty()) {
        ImGui::TextDisabled("No runs yet. Every job you run is listed here so you can run it again.");
        return;
    }
    for (const auto& h : history_) ImGui::BulletText("%s", h.c_str());
}

void Shell::draw_settings(const PerfSnapshot& perf) {
    ImGui::TextUnformatted("Performance (for comparing with the WinUI 3 build)");
    ImGui::Separator();
    ImGui::Text("First frame:   %.0f ms after start", perf.first_frame_ms);
    ImGui::Text("Frame time:    %.2f ms average, %.2f ms worst (last 120 frames)", perf.frame_ms_avg, perf.frame_ms_max);
    ImGui::Text("CPU:           %.1f %% of one core", perf.cpu_percent);
    ImGui::Text("Memory:        %.1f MB working set, %.1f MB private", perf.working_set_mb, perf.private_mb);
    ImGui::Text("Frames drawn:  %llu", static_cast<unsigned long long>(perf.frames));
    ImGui::Spacing();
    ImGui::Checkbox("Show the Dear ImGui demo window", &show_demo_);
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
        const JobEntry* first = nullptr;
        for (const auto& job : jobs_) {
            if (palette_query_[0] && !contains_ci(job.title, palette_query_) && !contains_ci(job.description, palette_query_))
                continue;
            if (!first) first = &job;
            if (ImGui::Selectable(job.title.c_str())) open_job(job);
        }
        if (enter && first) open_job(*first);
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) palette_open_ = false;
    }
    ImGui::End();
}
