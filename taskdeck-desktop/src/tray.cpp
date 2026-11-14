#include "tray.h"

#include <shellapi.h>

#include <cwchar>

namespace {
NOTIFYICONDATAW base(HWND hwnd) {
    NOTIFYICONDATAW d{};
    d.cbSize = sizeof(d);
    d.hWnd = hwnd;
    d.uID = 1;
    return d;
}

void copy(wchar_t* dst, size_t size, const std::wstring& src) {
    const size_t n = src.size() < size ? src.size() : size - 1;
    std::wmemcpy(dst, src.data(), n);
    dst[n] = L'\0';
}
}  // namespace

bool Tray::add() {
    NOTIFYICONDATAW d = base(hwnd_);
    d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP;
    d.uCallbackMessage = kMessage;
    // The exe's own icon when it has one (resource 1, later with releases), else Windows' application icon.
    d.hIcon = ::LoadIconW(::GetModuleHandleW(nullptr), MAKEINTRESOURCEW(1));
    if (!d.hIcon) d.hIcon = ::LoadIconW(nullptr, IDI_APPLICATION);
    copy(d.szTip, ARRAYSIZE(d.szTip), tip_);
    if (!::Shell_NotifyIconW(NIM_ADD, &d)) return false;
    d.uVersion = NOTIFYICON_VERSION_4;
    ::Shell_NotifyIconW(NIM_SETVERSION, &d);
    return true;
}

void Tray::show(HWND hwnd, const std::wstring& tip) {
    if (added_ && tip == tip_) return;  // updated a few times a second while a task moves: only on change
    hwnd_ = hwnd;
    tip_ = tip;
    if (!added_) {
        added_ = add();
        return;
    }
    NOTIFYICONDATAW d = base(hwnd_);
    d.uFlags = NIF_TIP | NIF_SHOWTIP;
    copy(d.szTip, ARRAYSIZE(d.szTip), tip_);
    ::Shell_NotifyIconW(NIM_MODIFY, &d);
}

void Tray::remove() {
    if (!added_) return;
    NOTIFYICONDATAW d = base(hwnd_);
    ::Shell_NotifyIconW(NIM_DELETE, &d);
    added_ = false;
}

void Tray::readd() {
    if (added_) added_ = add();
}

void Tray::balloon(const std::wstring& title, const std::wstring& text, bool warning) {
    if (!added_) return;
    NOTIFYICONDATAW d = base(hwnd_);
    d.uFlags = NIF_INFO;
    d.dwInfoFlags = (warning ? NIIF_WARNING : NIIF_INFO) | NIIF_RESPECT_QUIET_TIME;
    copy(d.szInfoTitle, ARRAYSIZE(d.szInfoTitle), title);
    copy(d.szInfo, ARRAYSIZE(d.szInfo), text);
    ::Shell_NotifyIconW(NIM_MODIFY, &d);
}
