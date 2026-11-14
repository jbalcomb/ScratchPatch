#include "credentials.h"

#include <windows.h>
#include <wincred.h>

#include <vector>

namespace credentials {
namespace {
std::wstring widen(const std::string& s) {
    if (s.empty()) return {};
    const int n = ::MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(n, L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

std::wstring part_name(const std::wstring& target, int part) {
    return part == 1 ? target : target + L"#" + std::to_wstring(part);
}

std::string why(DWORD code) {
    wchar_t* text = nullptr;
    ::FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                     nullptr, code, 0, reinterpret_cast<wchar_t*>(&text), 0, nullptr);
    std::string out = "Windows error " + std::to_string(code);
    if (text) {
        std::wstring w(text);
        ::LocalFree(text);
        while (!w.empty() && (w.back() == L'\n' || w.back() == L'\r' || w.back() == L' ')) w.pop_back();
        const int n = ::WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, nullptr, 0, nullptr, nullptr);
        std::string u(n > 0 ? n - 1 : 0, '\0');
        ::WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, u.data(), n, nullptr, nullptr);
        out += ": " + u;
    }
    return out;
}

// One part: the bytes, or nullopt if it isn't there.
std::optional<std::string> read_part(const std::wstring& name, std::string* error) {
    PCREDENTIALW cred = nullptr;
    if (!::CredReadW(name.c_str(), CRED_TYPE_GENERIC, 0, &cred)) {
        const DWORD code = ::GetLastError();
        if (code != ERROR_NOT_FOUND && error) *error = why(code);
        return std::nullopt;
    }
    std::string value(reinterpret_cast<const char*>(cred->CredentialBlob), cred->CredentialBlobSize);
    ::SecureZeroMemory(cred->CredentialBlob, cred->CredentialBlobSize);
    ::CredFree(cred);
    return value;
}
}  // namespace

std::wstring target(const std::string& settings_id, const std::string& field) {
    return L"TaskDeck/settings:" + widen(settings_id) + L":" + widen(field);
}

std::optional<std::string> read(const std::wstring& target, std::string* error) {
    auto first = read_part(target, error);
    if (!first) return std::nullopt;
    std::string value = std::move(*first);
    for (int part = 2;; ++part) {
        auto more = read_part(part_name(target, part), error);
        if (!more) break;
        value += *more;
    }
    return value;
}

bool write(const std::wstring& target, const std::string& value, std::string* error) {
    constexpr size_t kMax = CRED_MAX_CREDENTIAL_BLOB_SIZE;  // 5 * 512 bytes
    int part = 1;
    for (size_t at = 0; part == 1 || at < value.size(); at += kMax, ++part) {
        const std::wstring name = part_name(target, part);
        std::string chunk = value.substr(at, kMax);
        CREDENTIALW cred{};
        cred.Type = CRED_TYPE_GENERIC;
        cred.TargetName = const_cast<wchar_t*>(name.c_str());
        cred.Comment = const_cast<wchar_t*>(L"TaskDeck job setting");
        cred.CredentialBlobSize = static_cast<DWORD>(chunk.size());
        cred.CredentialBlob = reinterpret_cast<LPBYTE>(chunk.data());
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE;  // this PC only; never roams with the profile
        cred.UserName = const_cast<wchar_t*>(L"TaskDeck");
        const BOOL ok = ::CredWriteW(&cred, 0);
        const DWORD code = ::GetLastError();
        ::SecureZeroMemory(chunk.data(), chunk.size());
        if (!ok) {
            if (error) *error = why(code);
            return false;
        }
    }
    // Parts left over from a longer value stored before.
    for (; ::CredDeleteW(part_name(target, part).c_str(), CRED_TYPE_GENERIC, 0); ++part) {}
    return true;
}

void remove(const std::wstring& target) {
    for (int part = 1; ::CredDeleteW(part_name(target, part).c_str(), CRED_TYPE_GENERIC, 0); ++part) {}
}

}  // namespace credentials
