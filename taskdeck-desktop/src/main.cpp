// TaskDeck for Windows: Dear ImGui on Win32 + Direct3D 11.
// Based on Dear ImGui's examples/example_win32_directx11 (v1.92.9), plus:
// * an idle-friendly loop: it draws only after input (and a few frames more), then sleeps, so an idle
//   window uses ~0% CPU. `--continuous` draws every frame (vsync) instead, for frame-time measurements.
// * performance numbers for comparing with the WinUI 3 build later (src/perf.h, Settings tab).

#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"
#include "perf.h"
#include "shell.h"

#include <d3d11.h>
#include <windows.h>

#include <cwchar>
#include <string>

static ID3D11Device* g_device = nullptr;
static ID3D11DeviceContext* g_context = nullptr;
static IDXGISwapChain* g_swap_chain = nullptr;
static ID3D11RenderTargetView* g_rtv = nullptr;
static bool g_occluded = false;
static UINT g_resize_w = 0, g_resize_h = 0;

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

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR cmdline, int) {
    const bool continuous = cmdline && std::wcsstr(cmdline, L"--continuous");

    ImGui_ImplWin32_EnableDpiAwareness();
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
    }

    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();
    cleanup_device();
    ::DestroyWindow(hwnd);
    ::UnregisterClassW(wc.lpszClassName, wc.hInstance);
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
    switch (msg) {
        case WM_SIZE:
            if (wparam == SIZE_MINIMIZED) return 0;
            g_resize_w = LOWORD(lparam);
            g_resize_h = HIWORD(lparam);
            return 0;
        case WM_SYSCOMMAND:
            if ((wparam & 0xfff0) == SC_KEYMENU) return 0;  // no Alt menu
            break;
        case WM_DESTROY:
            ::PostQuitMessage(0);
            return 0;
    }
    return ::DefWindowProcW(hwnd, msg, wparam, lparam);
}
