// The TaskDeck window: the same four places as the phone app (Home, All jobs, History, Settings) and
// the command palette (Ctrl+K). Jobs are placeholders until the job model is decided (docs/PLAN.md).
#pragma once
#include "perf.h"

#include <string>
#include <vector>

struct JobEntry {
    std::string id, title, category, description;
};

class Shell {
public:
    Shell();
    // Draws one frame. Returns true while something animates (the palette opening), so the loop keeps
    // drawing; otherwise the window sleeps until the next input.
    bool draw(const PerfSnapshot& perf);

private:
    void draw_home();
    void draw_jobs();
    void draw_history();
    void draw_settings(const PerfSnapshot& perf);
    void draw_palette();
    void open_job(const JobEntry& job);

    enum class Tab { Home, Jobs, History, Settings } tab_ = Tab::Home;
    std::vector<JobEntry> jobs_;
    std::vector<std::string> history_;
    bool palette_open_ = false, palette_focus_ = false, show_demo_ = false;
    char palette_query_[128] = {};
    std::string open_job_id_;
};
