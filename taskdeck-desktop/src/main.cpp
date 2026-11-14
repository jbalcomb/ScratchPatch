// TaskDeck for Windows: Dear ImGui on Win32 + Direct3D 11.
// Based on Dear ImGui's examples/example_win32_directx11 (v1.92.9), plus:
// * an idle-friendly loop: it draws only after input (and a few frames more), then sleeps, so an idle
//   window uses ~0% CPU. `--continuous` draws every frame (vsync) instead, for frame-time measurements.
// * performance numbers for comparing with the WinUI 3 build later (src/perf.h, Settings tab).
// * the tray: closing the window while jobs or background tasks run can keep TaskDeck running in the
//   notification area until they finish (src/tray.h). One TaskDeck per data folder: starting another shows
//   the one already running.

#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"
#include "perf.h"
#include "shell.h"
#include "store.h"
#include "tray.h"

#include <d3d11.h>
#include <objbase.h>
#include <windows.h>
#include <shellapi.h>

#include <cwchar>
#include <cwctype>
#include <functional>
#include <string>
#include <vector>

static ID3D11Device* g_device = nullptr;
static ID3D11DeviceContext* g_context = nullptr;
static IDXGISwapChain* g_swap_chain = nullptr;
static ID3D11RenderTargetView* g_rtv = nullptr;
static bool g_occluded = false;
static UINT g_resize_w = 0, g_resize_h = 0;
static Shell* g_shell = nullptr;  // asked before closing while background tasks run
static Tray g_tray;
static bool g_hidden = false;                 // closed to the tray: no window, no frames
static std::vector<Activity> g_ended_hidden;  // what ended since then, for the notification
static std::wstring g_instance;               // the window property that marks this data folder's TaskDeck
static UINT g_show_message = 0;               // "show your window", from a second TaskDeck started
static UINT g_taskbar_created = 0;            // Explorer restarted
static constexpr UINT_PTR kQuitTimer = 1;     // closes TaskDeck a little after the last task ended in the tray

static bool create_device(HWND hwnd);
static void cleanup_device();
static void create_render_target();
static void cleanup_render_target();
static LRESULT WINAPI wnd_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam);

// Segoe UI, the Windows UI font, from the Windows fonts folder. Dear ImGui 1.92 rasterizes glyphs at the size
// they are drawn (base size x DPI scale), so text stays sharp at any scaling. If the font is missing, ImGui's
// built-in font is used.
static void load_ui_font(ImGuiIO& io, ImGuiStyle& style) {
    wchar_t dir[MAX_PATH];
    const UINT n = ::GetWindowsDirectoryW(dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return;
    char path[MAX_PATH * 3];
    const std::wstring wpath = std::wstring(dir) + L"\\Fonts\\segoeui.ttf";
    if (::GetFileAttributesW(wpath.c_str()) == INVALID_FILE_ATTRIBUTES) return;
    if (!::WideCharToMultiByte(CP_UTF8, 0, wpath.c_str(), -1, path, sizeof(path), nullptr, nullptr)) return;
    style.FontSizeBase = 16.0f;
    io.Fonts->AddFontFromFileTTF(path, style.FontSizeBase);
}

// Menu labels: "&" would underline the next letter.
static std::wstring menu_text(std::string s) {
    if (s.size() > 90) s = s.substr(0, 87) + "...";
    std::wstring out;
    for (wchar_t c : from_utf8(s)) {
        out += c;
        if (c == L'&') out += L'&';
    }
    return out;
}

static void show_window(HWND hwnd) {
    ::KillTimer(hwnd, kQuitTimer);
    g_hidden = false;
    g_ended_hidden.clear();
    ::ShowWindow(hwnd, ::IsIconic(hwnd) ? SW_RESTORE : SW_SHOW);
    ::SetForegroundWindow(hwnd);
}

// Closes the window; TaskDeck keeps running in the tray while something runs, and quits when nothing does.
static void hide_to_tray(HWND hwnd) {
    if (!g_shell || g_shell->activities().empty()) {
        ::DestroyWindow(hwnd);
        return;
    }
    ::ShowWindow(hwnd, SW_HIDE);
    g_hidden = true;
    g_ended_hidden.clear();
    // Hand back the memory only the window used; the job host and the tasks go on.
    ::SetProcessWorkingSetSize(::GetCurrentProcess(), static_cast<SIZE_T>(-1), static_cast<SIZE_T>(-1));
}

// Quits, stopping whatever runs (asked first, from the tray menu).
static void quit(HWND hwnd, bool ask) {
    const size_t n = g_shell ? g_shell->activities().size() : 0;
    if (ask && n > 0) {
        const std::wstring text = (n == 1 ? std::wstring(L"1 job or task is still running. Quitting stops it")
                                          : std::to_wstring(n) + L" jobs and tasks are still running. Quitting stops them") +
                                  L"; what's already done stays.\n\nQuit anyway?";
        if (::MessageBoxW(hwnd, text.c_str(), L"TaskDeck", MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND) != IDYES)
            return;
    }
    ::DestroyWindow(hwnd);
}

// The tray icon follows what runs: its tooltip names it, and while the window is closed to the tray, a
// notification says when everything has ended. TaskDeck then quits, unless something needs you (a failed
// upload to retry, a result waiting for its commit button): then it stays in the tray until opened.
static void update_tray(HWND hwnd) {
    for (auto& a : g_shell->take_ended())
        if (g_hidden) g_ended_hidden.push_back(std::move(a));
    const auto running = g_shell->activities();
    if (!g_hidden && running.empty()) {
        g_tray.remove();
        return;
    }
    std::wstring tip = L"TaskDeck";
    if (running.size() == 1) tip += L": " + from_utf8(running[0].title + " (" + running[0].status + ")");
    else if (running.size() > 1) tip += L": " + std::to_wstring(running.size()) + L" running";
    else tip += L": nothing running";
    g_tray.show(hwnd, tip.size() > 127 ? tip.substr(0, 124) + L"..." : tip);

    if (!g_hidden || !running.empty() || g_ended_hidden.empty()) return;
    int failed = 0;
    const Activity* first_failed = nullptr;
    for (const auto& a : g_ended_hidden)
        if (a.needs_you && failed++ == 0) first_failed = &a;
    std::wstring title, text;
    if (failed) {
        title = failed == 1 ? from_utf8(first_failed->title) : std::to_wstring(failed) + L" need you";
        text = from_utf8(first_failed->status) + L"\nOpen TaskDeck from this icon to see it.";
    } else if (g_ended_hidden.size() == 1) {
        title = from_utf8(g_ended_hidden[0].title);
        text = from_utf8(g_ended_hidden[0].status) + L". TaskDeck has closed.";
    } else {
        title = L"TaskDeck";
        text = std::to_wstring(g_ended_hidden.size()) + L" jobs and tasks have ended. TaskDeck has closed.";
    }
    g_tray.balloon(title, text, failed > 0);
    g_ended_hidden.clear();
    if (!failed) ::SetTimer(hwnd, kQuitTimer, 8000, nullptr);  // a little later: the notification goes with the icon
}

enum : UINT { kOpen = 1, kQuit, kCancelAll, kCancelFirst = 100 };

static void tray_menu(HWND hwnd, POINT at) {
    const auto list = g_shell->activities();
    HMENU menu = ::CreatePopupMenu();
    if (list.empty()) ::AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, L"Nothing running");
    for (size_t i = 0; i < list.size() && i < 50; ++i) {
        const Activity& a = list[i];
        HMENU sub = ::CreatePopupMenu();
        ::AppendMenuW(sub, MF_STRING | (a.stopping ? MF_GRAYED : 0), kCancelFirst + i, a.stopping ? L"Stopping..." : L"Cancel");
        ::AppendMenuW(menu, MF_POPUP, reinterpret_cast<UINT_PTR>(sub), menu_text(a.title + "    " + a.status).c_str());
    }
    ::AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    ::AppendMenuW(menu, MF_STRING | (list.empty() ? MF_GRAYED : 0), kCancelAll, L"Cancel all");
    ::AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    ::AppendMenuW(menu, MF_STRING, kOpen, L"Open TaskDeck");
    ::AppendMenuW(menu, MF_STRING, kQuit, L"Quit TaskDeck");
    ::SetMenuDefaultItem(menu, kOpen, FALSE);
    ::SetForegroundWindow(hwnd);  // so the menu closes when clicking elsewhere
    const UINT cmd = static_cast<UINT>(
        ::TrackPopupMenuEx(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, at.x, at.y, hwnd, nullptr));
    ::PostMessageW(hwnd, WM_NULL, 0, 0);
    ::DestroyMenu(menu);  // and its submenus
    if (cmd == kOpen) {
        show_window(hwnd);
    } else if (cmd == kQuit) {
        quit(hwnd, true);
    } else if (cmd == kCancelAll) {
        const std::wstring text = list.size() == 1 ? L"Cancel \u201c" + from_utf8(list[0].title) + L"\u201d?"
                                                   : L"Cancel all " + std::to_wstring(list.size()) + L" jobs and tasks?";
        if (::MessageBoxW(hwnd, text.c_str(), L"TaskDeck", MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND) == IDYES)
            g_shell->cancel_all();
    } else if (cmd >= kCancelFirst && cmd - kCancelFirst < list.size()) {
        g_shell->cancel(list[cmd - kCancelFirst]);
    }
}

// One TaskDeck per data folder (two would run two job hosts and overwrite each other's state.json). If one
// is running, ask it to show its window (it may be in the tray).
static void show_running_instance() {
    struct Find { const std::wstring* prop; HWND found; } find{&g_instance, nullptr};
    ::EnumWindows([](HWND h, LPARAM lp) -> BOOL {
        auto* f = reinterpret_cast<Find*>(lp);
        if (::GetPropW(h, f->prop->c_str())) { f->found = h; return FALSE; }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&find));
    if (!find.found) return;  // still starting: it shows itself
    DWORD pid = 0;
    ::GetWindowThreadProcessId(find.found, &pid);
    ::AllowSetForegroundWindow(pid);
    ::PostMessageW(find.found, g_show_message, 0, 0);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR cmdline, int) {
    const bool continuous = cmdline && std::wcsstr(cmdline, L"--continuous");

    g_show_message = ::RegisterWindowMessageW(L"TaskDeck.Show");
    g_taskbar_created = ::RegisterWindowMessageW(L"TaskbarCreated");
    std::wstring data = Store::default_path().wstring();
    for (auto& c : data) c = static_cast<wchar_t>(std::towlower(c));
    g_instance = L"TaskDeck." + std::to_wstring(std::hash<std::wstring>{}(data));
    HANDLE single = ::CreateMutexW(nullptr, FALSE, (L"Local\\" + g_instance).c_str());
    if (single && ::GetLastError() == ERROR_ALREADY_EXISTS) {
        show_running_instance();
        ::CloseHandle(single);
        return 0;
    }

    ImGui_ImplWin32_EnableDpiAwareness();
    ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);  // the file picker
    const float scale = ImGui_ImplWin32_GetDpiScaleForMonitor(::MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY));

    WNDCLASSEXW wc = {sizeof(wc), CS_CLASSDC, wnd_proc, 0, 0, instance, nullptr, nullptr, nullptr, nullptr, L"TaskDeck", nullptr};
    ::RegisterClassExW(&wc);
    HWND hwnd = ::CreateWindowW(wc.lpszClassName, L"TaskDeck", WS_OVERLAPPEDWINDOW, 100, 100,
                                static_cast<int>(1100 * scale), static_cast<int>(720 * scale), nullptr, nullptr, instance, nullptr);
    if (!create_device(hwnd)) {
        cleanup_device();
        ::UnregisterClassW(wc.lpszClassName, wc.hInstance);
        return 1;
    }
    ::SetPropW(hwnd, g_instance.c_str(), reinterpret_cast<HANDLE>(1));
    ::ShowWindow(hwnd, SW_SHOWDEFAULT);
    ::UpdateWindow(hwnd);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.IniFilename = nullptr;  // no imgui.ini next to the exe; window state comes later with the app's settings
    ImGui::StyleColorsDark();
    ImGuiStyle& style = ImGui::GetStyle();
    style.ScaleAllSizes(scale);
    style.FontScaleDpi = scale;
    style.WindowRounding = style.FrameRounding = 6 * scale;
    load_ui_font(io, style);

    ImGui_ImplWin32_Init(hwnd);
    ImGui_ImplDX11_Init(g_device, g_context);

    Shell shell;
    g_shell = &shell;
    shell.start(hwnd);  // the job host loads in the background; its messages wake the loop
    Perf perf;
    PerfSnapshot snap;
    int frames_left = 3;  // draw this many more frames before sleeping (ImGui needs a couple after input)
    bool done = false;
    while (!done) {
        MSG msg;
        while (::PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
            ::TranslateMessage(&msg);
            ::DispatchMessage(&msg);
            if (msg.message == WM_QUIT) done = true;
            frames_left = 3;
        }
        if (done) break;
        shell.pump();  // also while closed to the tray: the job host's messages keep coming
        update_tray(hwnd);
        if (g_hidden) {
            ::MsgWaitForMultipleObjects(0, nullptr, FALSE, INFINITE, QS_ALLINPUT);
            frames_left = 3;
            continue;
        }
        if (!continuous && frames_left <= 0) {
            // Idle: sleep until input. While typing, wake twice a second for the blinking cursor.
            ::MsgWaitForMultipleObjects(0, nullptr, FALSE, io.WantTextInput ? 500 : INFINITE, QS_ALLINPUT);
            frames_left = 1;
            continue;
        }
        if (g_occluded && g_swap_chain->Present(0, DXGI_PRESENT_TEST) == DXGI_STATUS_OCCLUDED) {
            ::Sleep(10);  // minimized or screen locked
            continue;
        }
        g_occluded = false;
        if (g_resize_w != 0 && g_resize_h != 0) {
            cleanup_render_target();
            g_swap_chain->ResizeBuffers(0, g_resize_w, g_resize_h, DXGI_FORMAT_UNKNOWN, 0);
            g_resize_w = g_resize_h = 0;
            create_render_target();
        }

        perf.frame_begin();
        ImGui_ImplDX11_NewFrame();
        ImGui_ImplWin32_NewFrame();
        ImGui::NewFrame();
        const bool busy = shell.draw(snap);
        ImGui::Render();
        const float clear[4] = {0.07f, 0.07f, 0.09f, 1.0f};
        g_context->OMSetRenderTargets(1, &g_rtv, nullptr);
        g_context->ClearRenderTargetView(g_rtv, clear);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        const HRESULT hr = g_swap_chain->Present(1, 0);  // vsync
        g_occluded = (hr == DXGI_STATUS_OCCLUDED);
        perf.frame_end();
        snap = perf.snapshot();
        frames_left = busy ? 3 : frames_left - 1;
        shell.after_frame();
        switch (shell.take_close_action()) {  // the close notice's answer
            case Shell::CloseAction::Tray: hide_to_tray(hwnd); break;
            case Shell::CloseAction::Quit: ::DestroyWindow(hwnd); break;
            case Shell::CloseAction::None: break;
        }
    }

    g_tray.remove();
    shell.stop();
    g_shell = nullptr;
    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();
    cleanup_device();
    ::DestroyWindow(hwnd);
    ::UnregisterClassW(wc.lpszClassName, wc.hInstance);
    ::CoUninitialize();
    if (single) ::CloseHandle(single);
    return 0;
}

static bool create_device(HWND hwnd) {
    DXGI_SWAP_CHAIN_DESC sd{};
    sd.BufferCount = 2;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferDesc.RefreshRate = {60, 1};
    sd.Flags = DXGI_SWAP_CHAIN_FLAG_ALLOW_MODE_SWITCH;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = hwnd;
    sd.SampleDesc.Count = 1;
    sd.Windowed = TRUE;
    sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;  // the example's choice; FLIP_DISCARD is a later optimisation

    const D3D_FEATURE_LEVEL levels[2] = {D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0};
    D3D_FEATURE_LEVEL level;
    HRESULT res = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, levels, 2, D3D11_SDK_VERSION,
                                                &sd, &g_swap_chain, &g_device, &level, &g_context);
    if (res == DXGI_ERROR_UNSUPPORTED)  // no GPU driver: the WARP software renderer
        res = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, levels, 2, D3D11_SDK_VERSION, &sd,
                                            &g_swap_chain, &g_device, &level, &g_context);
    if (res != S_OK) return false;
    create_render_target();
    return true;
}

static void cleanup_device() {
    cleanup_render_target();
    if (g_swap_chain) { g_swap_chain->Release(); g_swap_chain = nullptr; }
    if (g_context) { g_context->Release(); g_context = nullptr; }
    if (g_device) { g_device->Release(); g_device = nullptr; }
}

static void create_render_target() {
    ID3D11Texture2D* back = nullptr;
    g_swap_chain->GetBuffer(0, IID_PPV_ARGS(&back));
    g_device->CreateRenderTargetView(back, nullptr, &g_rtv);
    back->Release();
}

static void cleanup_render_target() {
    if (g_rtv) { g_rtv->Release(); g_rtv = nullptr; }
}

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam);

static LRESULT WINAPI wnd_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    if (ImGui_ImplWin32_WndProcHandler(hwnd, msg, wparam, lparam)) return true;
    if (msg != 0 && msg == g_show_message) {
        show_window(hwnd);
        return 0;
    }
    if (msg != 0 && msg == g_taskbar_created) {
        g_tray.readd();
        return 0;
    }
    switch (msg) {
        case Tray::kMessage:
            switch (LOWORD(lparam)) {
                case NIN_SELECT:
                case NIN_KEYSELECT:
                    show_window(hwnd);
                    break;
                case WM_CONTEXTMENU:
                    if (g_shell) tray_menu(hwnd, POINT{static_cast<short>(LOWORD(wparam)), static_cast<short>(HIWORD(wparam))});
                    break;
            }
            return 0;
        case WM_TIMER:
            if (wparam == kQuitTimer) {
                ::KillTimer(hwnd, kQuitTimer);
                if (g_hidden && g_shell && g_shell->activities().empty()) ::DestroyWindow(hwnd);
                return 0;
            }
            break;
        case WM_SIZE:
            if (wparam == SIZE_MINIMIZED) return 0;
            g_resize_w = LOWORD(lparam);
            g_resize_h = HIWORD(lparam);
            return 0;
        case WM_SYSCOMMAND:
            if ((wparam & 0xfff0) == SC_KEYMENU) return 0;  // no Alt menu
            break;
        case WM_APP:  // the job host sent something; waking the loop is all that's needed
            return 0;
        case WM_CLOSE:  // jobs and background tasks (uploads) run in the job host, which stops with the app
            if (!g_shell || g_hidden || g_shell->activities().empty()) break;  // hidden: a close from outside
            switch (g_shell->close_choice()) {
                case Shell::CloseChoice::Ask:
                    show_window(hwnd);  // the notice is drawn in the window
                    g_shell->ask_close();
                    return 0;
                case Shell::CloseChoice::Tray:
                    hide_to_tray(hwnd);
                    return 0;
                case Shell::CloseChoice::Quit:
                    break;
            }
            break;
        case WM_DESTROY:
            ::RemovePropW(hwnd, g_instance.c_str());
            g_tray.remove();
            ::PostQuitMessage(0);
            return 0;
    }
    return ::DefWindowProcW(hwnd, msg, wparam, lparam);
}
