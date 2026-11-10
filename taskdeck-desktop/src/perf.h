// Numbers to compare the Dear ImGui build against the WinUI 3 one later (docs/PLAN.md, "Measuring").
#pragma once
#include <cstdint>

struct PerfSnapshot {
    double first_frame_ms = 0;  // process start -> first frame on screen
    double frame_ms_avg = 0;    // over the last frames drawn
    double frame_ms_max = 0;
    double cpu_percent = 0;     // of one core, since the last snapshot
    double working_set_mb = 0;
    double private_mb = 0;
    uint64_t frames = 0;
};

class Perf {
public:
    void frame_begin();
    void frame_end();        // call after Present
    PerfSnapshot snapshot(); // cheap; call once a second or so
private:
    double now_ms() const;
    double frame_start_ = 0;
    double samples_[120] = {};
    int count_ = 0, next_ = 0;
    uint64_t frames_ = 0;
    double first_frame_ms_ = 0;
    double last_cpu_ms_ = 0, last_wall_ms_ = 0, last_cpu_percent_ = 0;
};
