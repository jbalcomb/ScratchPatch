#pragma once
#include <windows.h>

#include <string>
#include <vector>

// The Windows file picker. `extensions` like {".exe", ".com"} (empty = any file). Returns the chosen paths
// (none if cancelled). Runs its own modal loop: call it outside an ImGui frame. Needs COM initialized (STA).
std::vector<std::wstring> pick_files(HWND owner, bool multiple, const std::vector<std::string>& extensions);
// The Windows folder picker, starting in `start` if given. Returns the folder, or "" if cancelled.
std::wstring pick_folder(HWND owner, const std::wstring& start);
