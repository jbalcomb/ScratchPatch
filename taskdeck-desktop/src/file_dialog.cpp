#include "file_dialog.h"

#include "job_host.h"  // from_utf8

#include <shobjidl.h>

std::wstring pick_folder(HWND owner, const std::wstring& start) {
    std::wstring out;
    IFileOpenDialog* dlg = nullptr;
    if (FAILED(::CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dlg)))) return out;
    DWORD options = 0;
    dlg->GetOptions(&options);
    dlg->SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
    IShellItem* folder = nullptr;
    if (!start.empty() && SUCCEEDED(::SHCreateItemFromParsingName(start.c_str(), nullptr, IID_PPV_ARGS(&folder)))) {
        dlg->SetFolder(folder);
        folder->Release();
    }
    if (SUCCEEDED(dlg->Show(owner))) {
        IShellItem* item = nullptr;
        PWSTR path = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item)) && SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
            out = path;
            ::CoTaskMemFree(path);
        }
        if (item) item->Release();
    }
    dlg->Release();
    return out;
}

std::vector<std::wstring> pick_files(HWND owner, bool multiple, const std::vector<std::string>& extensions) {
    std::vector<std::wstring> out;
    IFileOpenDialog* dlg = nullptr;
    if (FAILED(::CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dlg)))) return out;
    DWORD options = 0;
    dlg->GetOptions(&options);
    dlg->SetOptions(options | FOS_FORCEFILESYSTEM | FOS_FILEMUSTEXIST | (multiple ? FOS_ALLOWMULTISELECT : 0));
    std::wstring spec;
    for (const auto& ext : extensions) spec += (spec.empty() ? L"*" : L";*") + from_utf8(ext);
    const std::wstring name = L"Supported files (" + spec + L")";
    const COMDLG_FILTERSPEC filters[] = {{name.c_str(), spec.c_str()}, {L"All files", L"*.*"}};
    if (!spec.empty()) dlg->SetFileTypes(2, filters);
    if (SUCCEEDED(dlg->Show(owner))) {
        IShellItemArray* items = nullptr;
        if (SUCCEEDED(dlg->GetResults(&items))) {
            DWORD count = 0;
            items->GetCount(&count);
            for (DWORD i = 0; i < count; ++i) {
                IShellItem* item = nullptr;
                PWSTR path = nullptr;
                if (SUCCEEDED(items->GetItemAt(i, &item)) && SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
                    out.emplace_back(path);
                    ::CoTaskMemFree(path);
                }
                if (item) item->Release();
            }
            items->Release();
        }
    }
    dlg->Release();
    return out;
}
