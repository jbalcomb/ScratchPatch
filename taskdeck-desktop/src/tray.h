// TaskDeck's icon in the notification area (the tray): shown while jobs or background tasks run, and while
// the window is closed to the tray so they can finish. Its menu is built in main.cpp from Shell::activities().
#pragma once
#include <windows.h>

#include <string>

class Tray {
public:
    // Sent to the window for the icon's clicks (NOTIFYICON_VERSION_4: the event is LOWORD(lparam)).
    static constexpr UINT kMessage = WM_APP + 1;

    // Adds the icon, or changes its tooltip if it's there already.
    void show(HWND hwnd, const std::wstring& tip);
    void remove();
    // Explorer restarted (the "TaskbarCreated" message): the icon has to be added again.
    void readd();
    void balloon(const std::wstring& title, const std::wstring& text, bool warning);
    bool visible() const { return added_; }

private:
    bool add();

    HWND hwnd_ = nullptr;
    bool added_ = false;
    std::wstring tip_;
};
