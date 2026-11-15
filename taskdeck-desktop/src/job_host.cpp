#include "job_host.h"

#include "updater.h"

#include <filesystem>

namespace fs = std::filesystem;

std::string to_utf8(const std::wstring& w) {
    if (w.empty()) return {};
    const int n = ::WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), s.data(), n, nullptr, nullptr);
    return s;
}

std::wstring from_utf8(const std::string& s) {
    if (s.empty()) return {};
    const int n = ::MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(n, L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

namespace {
std::wstring env(const wchar_t* name) {
    wchar_t buf[32768];
    const DWORD n = ::GetEnvironmentVariableW(name, buf, 32768);
    return (n > 0 && n < 32768) ? std::wstring(buf, n) : std::wstring();
}

// The Python interpreter: TASKDECK_PYTHON, else the one a release comes with (python-runtime, next to the exe),
// else python.exe on PATH (not the Microsoft Store stub, which only opens the Store), else the py launcher.
bool find_python(std::wstring& exe, std::wstring& args) {
    exe = env(L"TASKDECK_PYTHON");
    if (!exe.empty()) return fs::exists(exe);
    const fs::path bundled = install::dir() / L"python-runtime" / L"python.exe";
    if (install::packaged() && fs::exists(bundled)) {
        exe = bundled.wstring();
        return true;
    }
    wchar_t found[MAX_PATH];
    if (::SearchPathW(nullptr, L"python.exe", nullptr, MAX_PATH, found, nullptr) &&
        std::wstring(found).find(L"\\WindowsApps\\") == std::wstring::npos) {
        exe = found;
        return true;
    }
    if (::SearchPathW(nullptr, L"py.exe", nullptr, MAX_PATH, found, nullptr)) {
        exe = found;
        args = L" -3";
        return true;
    }
    return false;
}

std::wstring quote(const fs::path& p) { return L"\"" + p.wstring() + L"\""; }
}  // namespace

JobHost::~JobHost() { stop(); }

bool JobHost::start(HWND notify, std::string& error) {
    stop();  // a restart: clean up the previous host first
    take();
    notify_ = notify;
    std::wstring python, python_args;
    if (!find_python(python, python_args)) {
        error = "Python wasn't found. Install Python 3.10 or newer (python.org) or set TASKDECK_PYTHON to python.exe.";
        return false;
    }
    // The host script and the phone app's job sources: in a release, python\ and jobs\ next to the exe; else
    // the source tree this exe was built from. TASKDECK_DESKTOP_DIR and TASKDECK_SRC override either.
    const bool packaged = install::packaged();
    std::wstring desktop_dir = env(L"TASKDECK_DESKTOP_DIR");
    if (desktop_dir.empty()) desktop_dir = packaged ? install::dir().wstring() : from_utf8(TASKDECK_DEV_DIR);
    const fs::path script = fs::path(desktop_dir) / L"python" / L"taskdeck_host.py";
    std::wstring src = env(L"TASKDECK_SRC");
    const fs::path src_dir = !src.empty() ? fs::path(src)
                             : packaged   ? install::dir() / L"jobs"
                                          : (fs::path(desktop_dir) / L".." / L"taskdeck" / L"src").lexically_normal();
    if (!fs::exists(script)) {
        error = "The job host script is missing: " + to_utf8(script.wstring());
        return false;
    }
    if (!fs::exists(src_dir / L"taskdeck" / L"core")) {
        error = "The TaskDeck job sources are missing: " + to_utf8(src_dir.wstring()) + " (set TASKDECK_SRC)";
        return false;
    }
    fs::path log_dir = fs::path(env(L"LOCALAPPDATA")) / L"TaskDeck";
    std::error_code ec;
    fs::create_directories(log_dir, ec);
    log_path_ = (log_dir / L"job-host.log").wstring();

    // Pipes for the child's stdin and stdout; our ends aren't inherited. Its stderr goes nowhere: the host
    // writes its errors and the jobs' prints to the log file instead.
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    HANDLE stdin_r = nullptr, stdout_w = nullptr;
    if (!::CreatePipe(&stdin_r, &stdin_w_, &sa, 0) || !::CreatePipe(&stdout_r_, &stdout_w, &sa, 0)) {
        error = "Couldn't create pipes for the job host.";
        return false;
    }
    ::SetHandleInformation(stdin_w_, HANDLE_FLAG_INHERIT, 0);
    ::SetHandleInformation(stdout_r_, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{sizeof(si)};
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = stdin_r;
    si.hStdOutput = stdout_w;
    si.hStdError = nullptr;
    command_line_ = quote(python) + python_args + L" -u -X utf8 " + quote(script) + L" --src " + quote(src_dir) +
                    L" --log " + quote(log_path_);
    std::wstring cmd = command_line_;  // CreateProcessW may write to it
    PROCESS_INFORMATION pi{};
    const BOOL ok = ::CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED,
                                     nullptr, nullptr, &si, &pi);
    ::CloseHandle(stdin_r);
    ::CloseHandle(stdout_w);
    if (!ok) {
        error = "Couldn't start Python (" + to_utf8(python) + "), error " + std::to_string(::GetLastError()) + ".";
        return false;
    }
    // A job object, so the host (and anything it starts) ends when TaskDeck does, even if TaskDeck crashes.
    job_ = ::CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    ::SetInformationJobObject(job_, JobObjectExtendedLimitInformation, &limits, sizeof(limits));
    ::AssignProcessToJobObject(job_, pi.hProcess);
    ::ResumeThread(pi.hThread);
    ::CloseHandle(pi.hThread);
    process_ = pi.hProcess;
    exited_ = false;
    reader_ = std::thread(&JobHost::read_loop, this);
    return true;
}

void JobHost::send(const nlohmann::json& msg) {
    if (!stdin_w_ || exited_) return;
    const std::string line = msg.dump() + "\n";  // dump() escapes newlines, so one message is one line
    DWORD written = 0;
    ::WriteFile(stdin_w_, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
}

std::vector<nlohmann::json> JobHost::take() {
    std::lock_guard lock(mutex_);
    std::vector<nlohmann::json> out;
    out.swap(inbox_);
    return out;
}

void JobHost::push(nlohmann::json msg) {
    {
        std::lock_guard lock(mutex_);
        inbox_.push_back(std::move(msg));
    }
    if (notify_) ::PostMessageW(notify_, WM_APP, 0, 0);
}

void JobHost::read_loop() {
    std::string pending;
    char buf[65536];
    DWORD n = 0;
    while (::ReadFile(stdout_r_, buf, sizeof(buf), &n, nullptr) && n > 0) {
        pending.append(buf, n);
        size_t start = 0, nl;
        while ((nl = pending.find('\n', start)) != std::string::npos) {
            std::string line = pending.substr(start, nl - start);
            start = nl + 1;
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.empty()) continue;
            auto msg = nlohmann::json::parse(line, nullptr, /*allow_exceptions=*/false);
            if (msg.is_discarded())
                msg = {{"event", "garbled"}, {"line", line.substr(0, 200)}};
            push(std::move(msg));
        }
        pending.erase(0, start);
    }
    DWORD code = 0;
    if (process_) {
        ::WaitForSingleObject(process_, 2000);
        ::GetExitCodeProcess(process_, &code);
    }
    exited_ = true;
    push({{"event", "exited"}, {"code", code}});
}

void JobHost::stop() {
    if (stdin_w_) { ::CloseHandle(stdin_w_); stdin_w_ = nullptr; }  // the host's loop ends at end of input
    if (process_ && ::WaitForSingleObject(process_, 1500) == WAIT_TIMEOUT) ::TerminateProcess(process_, 1);
    if (reader_.joinable()) reader_.join();
    if (stdout_r_) { ::CloseHandle(stdout_r_); stdout_r_ = nullptr; }
    if (process_) { ::CloseHandle(process_); process_ = nullptr; }
    if (job_) { ::CloseHandle(job_); job_ = nullptr; }
    notify_ = nullptr;
}
