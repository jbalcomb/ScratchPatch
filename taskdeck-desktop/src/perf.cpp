#include "perf.h"

#include <windows.h>
#include <psapi.h>

#include <algorithm>

static double filetime_ms(const FILETIME& ft) {
    ULARGE_INTEGER v;
    v.LowPart = ft.dwLowDateTime;
    v.HighPart = ft.dwHighDateTime;
    return v.QuadPart / 10000.0;  // 100 ns units
}

double Perf::now_ms() const {
    static LARGE_INTEGER freq = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return t.QuadPart * 1000.0 / freq.QuadPart;
}

void Perf::frame_begin() { frame_start_ = now_ms(); }

void Perf::frame_end() {
    const double ms = now_ms() - frame_start_;
    samples_[next_] = ms;
    next_ = (next_ + 1) % 120;
    count_ = std::min(count_ + 1, 120);
    if (frames_++ == 0) {
        FILETIME created, exited, kernel, user, now;
        GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user);
        GetSystemTimePreciseAsFileTime(&now);
        first_frame_ms_ = filetime_ms(now) - filetime_ms(created);
    }
}

PerfSnapshot Perf::snapshot() {
    PerfSnapshot s;
    s.first_frame_ms = first_frame_ms_;
    s.frames = frames_;
    for (int i = 0; i < count_; ++i) {
        s.frame_ms_avg += samples_[i];
        s.frame_ms_max = std::max(s.frame_ms_max, samples_[i]);
    }
    if (count_) s.frame_ms_avg /= count_;

    FILETIME created, exited, kernel, user;
    GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user);
    const double cpu = filetime_ms(kernel) + filetime_ms(user), wall = now_ms();
    if (last_wall_ms_ > 0 && wall - last_wall_ms_ >= 500) {
        last_cpu_percent_ = 100.0 * (cpu - last_cpu_ms_) / (wall - last_wall_ms_);
        last_cpu_ms_ = cpu;
        last_wall_ms_ = wall;
    } else if (last_wall_ms_ == 0) {
        last_cpu_ms_ = cpu;
        last_wall_ms_ = wall;
    }
    s.cpu_percent = last_cpu_percent_;

    PROCESS_MEMORY_COUNTERS_EX pmc{};
    if (GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&pmc), sizeof(pmc))) {
        s.working_set_mb = pmc.WorkingSetSize / (1024.0 * 1024.0);
        s.private_mb = pmc.PrivateUsage / (1024.0 * 1024.0);
    }
    return s;
}
