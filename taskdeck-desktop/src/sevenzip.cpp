// sevenzip.cpp: 7-Zip's archive library (7z.dll) behind the API in sevenzip.h.
//
// Adapted from the phone app's shim (taskdeck/native/sevenzip/sz_shim.cpp, itself based on 7-Zip's example
// client CPP/7zip/UI/Client7z/Client7z.cpp) for Windows: 7z.dll is loaded with LoadLibrary, paths are UTF-16
// with backslashes, and the API is C++ (no ctypes in between). Error codes and safety rules are the same.
// Compiled with 7-Zip's headers and a few of its helper sources (CMakeLists.txt, target `sevenzip`).

#include "Common/Common.h"

#include <windows.h>

#include "Common/MyWindows.h"
#include "Common/MyInitGuid.h"

#include "Common/MyCom.h"
#include "Common/MyString.h"
#include "Common/StringConvert.h"
#include "Common/UTFConvert.h"

#include "Windows/FileDir.h"
#include "Windows/FileFind.h"
#include "Windows/PropVariant.h"
#include "Windows/PropVariantConv.h"

#include "7zip/Archive/IArchive.h"
#include "7zip/Common/FileStreams.h"
#include "7zip/IPassword.h"
#include "../C/7zVersion.h"

#include "sevenzip.h"

#include <cctype>
#include <mutex>

using namespace NWindows;
using namespace NFile;

namespace sevenzip {

std::string message(int code, const std::string& entry) {
    std::string m;
    switch (code) {
        case E_LOAD: m = "The archive engine (7z.dll) is missing or can't be loaded. Reinstall TaskDeck."; break;
        case E_OPEN_FILE: m = "Can't read the file."; break;
        case E_UNSUPPORTED: m = "This isn't an archive TaskDeck can open (or it is damaged)."; break;
        case E_NEED_PASSWORD: m = "This archive is password-protected. Enter the password."; break;
        case E_WRONG_PASSWORD: m = "Wrong password."; break;
        case E_DATA: m = "The archive is damaged."; break;
        case E_CANCELLED: m = "Cancelled."; break;
        case E_WRITE: m = "Can't write the files there (no permission, or the disk is full)."; break;
        case E_UNSAFE_PATH: m = "The archive contains a path that points outside the target folder; nothing was extracted from it."; break;
        case E_UNSUPPORTED_METHOD: m = "The archive uses a compression method that isn't supported."; break;
        case E_OTHER: m = "Extracting failed."; break;
        default: m = "Archive error " + std::to_string(code) + "."; break;
    }
    const bool names_entry = code == E_DATA || code == E_WRONG_PASSWORD || code == E_WRITE || code == E_UNSAFE_PATH ||
                             code == E_UNSUPPORTED_METHOD;
    if (names_entry && !entry.empty()) m += " (" + entry + ")";
    return m;
}

// ---- the library --------------------------------------------------------------------------------------------

static std::mutex g_load_mutex;
static HMODULE g_lib = nullptr;
static Func_CreateObject g_CreateObject = nullptr;
static Func_GetNumberOfFormats g_GetNumberOfFormats = nullptr;
static Func_GetHandlerProperty2 g_GetHandlerProperty2 = nullptr;

bool load(const std::wstring& dll_path, std::string* error) {
    std::lock_guard lock(g_load_mutex);
    if (g_lib) return true;
    HMODULE lib = ::LoadLibraryExW(dll_path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!lib) {
        if (error) {
            const DWORD code = ::GetLastError();
            *error = code == ERROR_MOD_NOT_FOUND ? "not found" : "error " + std::to_string(code);
        }
        return false;
    }
    g_CreateObject = reinterpret_cast<Func_CreateObject>(::GetProcAddress(lib, "CreateObject"));
    g_GetNumberOfFormats = reinterpret_cast<Func_GetNumberOfFormats>(::GetProcAddress(lib, "GetNumberOfFormats"));
    g_GetHandlerProperty2 = reinterpret_cast<Func_GetHandlerProperty2>(::GetProcAddress(lib, "GetHandlerProperty2"));
    if (!g_CreateObject || !g_GetNumberOfFormats || !g_GetHandlerProperty2) {
        ::FreeLibrary(lib);
        if (error) *error = "not 7-Zip's 7z.dll (CreateObject is missing)";
        return false;
    }
    g_lib = lib;
    return true;
}

bool loaded() {
    std::lock_guard lock(g_load_mutex);
    return g_lib != nullptr;
}

std::string version() { return MY_VERSION; }

// ---- helpers ------------------------------------------------------------------------------------------------

static UString FromUtf8(const std::string& s) {
    UString u;
    ConvertUTF8ToUnicode(AString(s.c_str()), u);
    return u;
}

static std::string ToUtf8(const UString& u) {
    AString a;
    ConvertUnicodeToUTF8(u, a);
    return std::string(a.Ptr(), a.Len());
}

static bool GetBoolProp(IInArchive* arc, UInt32 index, PROPID id) {
    NCOM::CPropVariant prop;
    if (arc->GetProperty(index, id, &prop) != S_OK) return false;
    return prop.vt == VT_BOOL && VARIANT_BOOLToBool(prop.boolVal);
}

// The entry's path with '/' separators, as the phone gets it (7-Zip on Windows gives backslashes).
static UString GetPathProp(IInArchive* arc, UInt32 index) {
    NCOM::CPropVariant prop;
    if (arc->GetProperty(index, kpidPath, &prop) == S_OK && prop.vt == VT_BSTR) {
        UString path(prop.bstrVal);
        path.Replace(L'\\', L'/');
        return path;
    }
    return UString(L"[content]");  // e.g. the single file inside a .gz
}

bool safe_relative(const std::string& name, std::string* out) {
    std::string s = name;
    for (char& c : s)
        if (c == '\\') c = '/';
    if (s.size() >= 2 && s[1] == ':' && std::isalpha(static_cast<unsigned char>(s[0]))) s.erase(0, 2);  // "C:"
    std::string result;
    size_t start = 0;
    while (start <= s.size()) {
        size_t end = s.find('/', start);
        if (end == std::string::npos) end = s.size();
        const std::string part = s.substr(start, end - start);
        if (part == "..") return false;
        if (!part.empty() && part != ".") {
            if (!result.empty()) result += '/';
            result += part;
        }
        start = end + 1;
    }
    if (out) *out = result;  // "" for "." or "/": the target folder itself
    return true;
}

// ---- open callback (asks for the password of encrypted names) -----------------------------------------------

class COpenCallback Z7_final : public IArchiveOpenCallback, public ICryptoGetTextPassword, public CMyUnknownImp {
    Z7_IFACES_IMP_UNK_2(IArchiveOpenCallback, ICryptoGetTextPassword)
public:
    bool HasPassword = false;
    bool PasswordAsked = false;
    UString Password;
};

Z7_COM7F_IMF(COpenCallback::SetTotal(const UInt64*, const UInt64*)) { return S_OK; }
Z7_COM7F_IMF(COpenCallback::SetCompleted(const UInt64*, const UInt64*)) { return S_OK; }

Z7_COM7F_IMF(COpenCallback::CryptoGetTextPassword(BSTR* password)) {
    PasswordAsked = true;
    if (!HasPassword) return E_ABORT;
    return StringToBstr(Password, password);
}

// ---- formats ------------------------------------------------------------------------------------------------

struct CFormat {
    GUID clsid;
    AString name;
    AString exts;  // space-separated, lowercase
    CByteBuffer sig;
    CObjectVector<CByteBuffer> multiSig;
    UInt32 sigOffset = 0;
};

static bool GetFormats(CObjectVector<CFormat>& formats) {
    UInt32 n = 0;
    if (g_GetNumberOfFormats(&n) != S_OK) return false;
    for (UInt32 i = 0; i < n; i++) {
        CFormat f;
        NCOM::CPropVariant prop;
        if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kClassID, &prop) != S_OK || prop.vt != VT_BSTR ||
            SysStringByteLen(prop.bstrVal) != sizeof(GUID))
            continue;
        memcpy(&f.clsid, prop.bstrVal, sizeof(GUID));
        prop.Clear();
        if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kName, &prop) == S_OK && prop.vt == VT_BSTR)
            f.name = ToUtf8(UString(prop.bstrVal)).c_str();
        prop.Clear();
        if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kExtension, &prop) == S_OK && prop.vt == VT_BSTR) {
            f.exts = ToUtf8(UString(prop.bstrVal)).c_str();
            f.exts.MakeLower_Ascii();
        }
        prop.Clear();
        if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kSignature, &prop) == S_OK && prop.vt == VT_BSTR)
            f.sig.CopyFrom((const Byte*)prop.bstrVal, SysStringByteLen(prop.bstrVal));
        prop.Clear();
        if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kMultiSignature, &prop) == S_OK && prop.vt == VT_BSTR) {
            const Byte* p = (const Byte*)prop.bstrVal;
            UInt32 size = SysStringByteLen(prop.bstrVal);
            while (size > 0) {
                const UInt32 len = *p++;
                size--;
                if (len > size) break;
                CByteBuffer b;
                b.CopyFrom(p, len);
                f.multiSig.Add(b);
                p += len;
                size -= len;
            }
        }
        prop.Clear();
        if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kSignatureOffset, &prop) == S_OK && prop.vt == VT_UI4)
            f.sigOffset = prop.ulVal;
        formats.Add(f);
    }
    return true;
}

static bool HasExt(const AString& exts, const AString& ext) {
    if (ext.IsEmpty()) return false;
    int pos = 0;
    const int len = (int)exts.Len();
    while (pos < len) {
        int end = exts.Find(' ', (unsigned)pos);
        if (end < 0) end = len;
        if (exts.Mid((unsigned)pos, (unsigned)(end - pos)) == ext) return true;
        pos = end + 1;
    }
    return false;
}

static bool SigMatches(const CByteBuffer& sig, UInt32 offset, const Byte* head, size_t headLen) {
    if (sig.Size() == 0 || offset + sig.Size() > headLen) return false;
    return memcmp(head + offset, sig, sig.Size()) == 0;
}

// ---- extract callback ---------------------------------------------------------------------------------------

class CExtractCallback Z7_final : public IArchiveExtractCallback, public ICryptoGetTextPassword, public CMyUnknownImp {
    Z7_IFACES_IMP_UNK_2(IArchiveExtractCallback, ICryptoGetTextPassword)
    Z7_IFACE_COM7_IMP(IProgress)

    COutFileStream* _outSpec = nullptr;
    CMyComPtr<ISequentialOutStream> _out;
    bool _encrypted = false;

public:
    CMyComPtr<IInArchive> Arc;
    UString Dir;         // ends with '\'
    UString SingleFile;  // extract_first(): the file entry 0 goes to
    bool HasPassword = false;
    UString Password;
    const Progress* OnProgress = nullptr;
    UInt64 Total = 0;
    int Error = OK;      // the first error
    UString ErrorName;   // the entry it happened on
    UString Current;

    void Fail(int code, const UString& name) {
        if (Error == OK) {
            Error = code;
            ErrorName = name;
        }
    }

private:
    HRESULT Open(const UString& path) {
        NFind::CFileInfo fi;
        if (fi.Find(us2fs(path)) && !NDir::DeleteFileAlways(us2fs(path))) {  // existing files are replaced
            Fail(E_WRITE, Current);
            return E_ABORT;
        }
        _outSpec = new COutFileStream;
        CMyComPtr<ISequentialOutStream> loc(_outSpec);
        if (!_outSpec->Create_ALWAYS(us2fs(path))) {
            Fail(E_WRITE, Current);
            return E_ABORT;
        }
        _out = loc;
        return S_OK;
    }
};

Z7_COM7F_IMF(CExtractCallback::SetTotal(UInt64 size)) {
    Total = size;
    return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::SetCompleted(const UInt64* completeValue)) {
    if (OnProgress && *OnProgress && completeValue && (*OnProgress)(*completeValue, Total)) {
        Fail(E_CANCELLED, Current);
        return E_ABORT;
    }
    return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::GetStream(UInt32 index, ISequentialOutStream** outStream, Int32 askExtractMode)) {
    *outStream = nullptr;
    _out.Release();
    _outSpec = nullptr;
    Current = GetPathProp(Arc, index);
    _encrypted = GetBoolProp(Arc, index, kpidEncrypted);
    if (askExtractMode != NArchive::NExtract::NAskMode::kExtract) return S_OK;

    if (!SingleFile.IsEmpty()) {
        if (index != 0) return S_OK;  // skipped
        const HRESULT res = Open(SingleFile);
        if (res != S_OK) return res;
        *outStream = _out;
        (*outStream)->AddRef();
        return S_OK;
    }

    std::string rel;
    const bool is_dir = GetBoolProp(Arc, index, kpidIsDir);
    if (!safe_relative(ToUtf8(Current), &rel) || (rel.empty() && !is_dir)) {
        Fail(E_UNSAFE_PATH, Current);
        return E_ABORT;
    }
    if (rel.empty()) return S_OK;  // "./" in a tar made with "tar -C folder .": the target folder itself
    UString full = Dir + FromUtf8(rel);
    full.Replace(L'/', L'\\');

    if (is_dir) {
        if (!NDir::CreateComplexDir(us2fs(full))) {
            Fail(E_WRITE, Current);
            return E_ABORT;
        }
        return S_OK;
    }
    const int slash = full.ReverseFind(L'\\');
    if (slash > 0 && !NDir::CreateComplexDir(us2fs(full.Left((unsigned)slash)))) {
        Fail(E_WRITE, Current);
        return E_ABORT;
    }
    const HRESULT res = Open(full);
    if (res != S_OK) return res;
    *outStream = _out;
    (*outStream)->AddRef();
    return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::PrepareOperation(Int32)) { return S_OK; }

Z7_COM7F_IMF(CExtractCallback::SetOperationResult(Int32 result)) {
    if (_out) {
        _outSpec->Close();
        _out.Release();
    }
    using namespace NArchive::NExtract::NOperationResult;
    switch (result) {
        case kOK: break;
        case kWrongPassword: Fail(E_WRONG_PASSWORD, Current); break;
        case kUnsupportedMethod: Fail(E_UNSUPPORTED_METHOD, Current); break;
        case kCRCError:
        case kDataError:
            Fail(_encrypted ? E_WRONG_PASSWORD : E_DATA, Current);  // with encryption, bad data = a wrong password
            break;
        default: Fail(E_DATA, Current); break;
    }
    return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::CryptoGetTextPassword(BSTR* password)) {
    if (!HasPassword) {
        Fail(E_NEED_PASSWORD, Current);
        return E_ABORT;
    }
    return StringToBstr(Password, password);
}

// ---- the archive --------------------------------------------------------------------------------------------

struct Archive::Impl {
    CMyComPtr<IInArchive> arc;
    CMyComPtr<IInStream> file;
    std::string format;
    std::optional<std::string> password;

    ~Impl() {
        if (arc) arc->Close();
    }
};

Archive::Archive() = default;
Archive::~Archive() = default;

const std::string& Archive::format() const {
    static const std::string none;
    return impl_ ? impl_->format : none;
}

int Archive::open(const std::wstring& path, const std::optional<std::string>& password) {
    impl_.reset();
    if (!loaded()) return E_LOAD;

    CInFileStream* fileSpec = new CInFileStream;
    CMyComPtr<IInStream> file = fileSpec;
    if (!fileSpec->Open(us2fs(UString(path.c_str())))) return E_OPEN_FILE;

    Byte head[1 << 12];
    UInt32 headLen = 0;
    file->Read(head, sizeof(head), &headLen);

    AString ext;
    {
        const size_t dot = path.find_last_of(L'.');
        const size_t slash = path.find_last_of(L"\\/");
        if (dot != std::wstring::npos && (slash == std::wstring::npos || dot > slash))
            ext = ToUtf8(UString(path.substr(dot + 1).c_str())).c_str();
        ext.MakeLower_Ascii();
    }

    CObjectVector<CFormat> formats;
    if (!GetFormats(formats)) return E_LOAD;

    // Candidates: formats claiming this extension first, then formats whose signature matches.
    CRecordVector<unsigned> order;
    for (unsigned i = 0; i < formats.Size(); i++)
        if (HasExt(formats[i].exts, ext)) order.Add(i);
    for (unsigned i = 0; i < formats.Size(); i++) {
        bool match = SigMatches(formats[i].sig, formats[i].sigOffset, head, headLen);
        for (unsigned k = 0; !match && k < formats[i].multiSig.Size(); k++)
            match = SigMatches(formats[i].multiSig[k], formats[i].sigOffset, head, headLen);
        if (!match) continue;
        bool already = false;
        for (unsigned k = 0; k < order.Size(); k++)
            if (order[k] == i) already = true;
        if (!already) order.Add(i);
    }

    const UString pw = FromUtf8(password.value_or(""));
    bool passwordAsked = false;
    for (unsigned k = 0; k < order.Size(); k++) {
        const CFormat& f = formats[order[k]];
        CMyComPtr<IInArchive> arc;
        if (g_CreateObject(&f.clsid, &IID_IInArchive, (void**)&arc) != S_OK || !arc) continue;
        if (file->Seek(0, STREAM_SEEK_SET, nullptr) != S_OK) return E_OPEN_FILE;
        COpenCallback* cbSpec = new COpenCallback;
        CMyComPtr<IArchiveOpenCallback> cb(cbSpec);
        cbSpec->HasPassword = password.has_value();
        cbSpec->Password = pw;
        const UInt64 scanSize = 1 << 23;
        const HRESULT res = arc->Open(file, &scanSize, cb);
        if (cbSpec->PasswordAsked) passwordAsked = true;
        if (res == S_OK) {
            impl_ = std::make_unique<Impl>();
            impl_->arc = arc;
            impl_->file = file;
            impl_->format = f.name.Ptr();
            impl_->password = password;
            return OK;
        }
        arc->Close();
        if (cbSpec->PasswordAsked)  // encrypted names: the right format, the wrong (or no) password
            return password ? E_WRONG_PASSWORD : E_NEED_PASSWORD;
    }
    if (passwordAsked) return password ? E_WRONG_PASSWORD : E_NEED_PASSWORD;
    return E_UNSUPPORTED;
}

std::vector<Entry> Archive::entries() const {
    std::vector<Entry> out;
    if (!impl_) return out;
    IInArchive* arc = impl_->arc;
    UInt32 n = 0;
    arc->GetNumberOfItems(&n);
    out.reserve(n);
    for (UInt32 i = 0; i < n; i++) {
        Entry e;
        e.name = ToUtf8(GetPathProp(arc, i));
        NCOM::CPropVariant prop;
        UInt64 size = 0;
        if (arc->GetProperty(i, kpidSize, &prop) == S_OK) ConvertPropVariantToUInt64(prop, size);
        e.size = size;
        e.dir = GetBoolProp(arc, i, kpidIsDir);
        e.encrypted = GetBoolProp(arc, i, kpidEncrypted);
        out.push_back(std::move(e));
    }
    return out;
}

std::vector<std::string> Archive::unsafe_entries() const {
    std::vector<std::string> out;
    for (const auto& e : entries())
        if (!safe_relative(e.name, nullptr)) out.push_back(e.name);
    return out;
}

static int run_extract(IInArchive* arc, CExtractCallback* cbSpec, const UInt32* indices, UInt32 count,
                       std::string* failed_entry) {
    CMyComPtr<IArchiveExtractCallback> cb(cbSpec);
    const HRESULT res = arc->Extract(indices, count, 0, cb);
    int code = cbSpec->Error;
    if (code == OK && res != S_OK) code = res == E_ABORT ? E_CANCELLED : E_OTHER;
    if (code != OK && failed_entry) *failed_entry = ToUtf8(cbSpec->ErrorName);
    return code;
}

int Archive::extract(const std::wstring& dir, const std::optional<std::string>& password, const Progress& progress,
                     std::string* failed_entry) {
    if (!impl_) return E_OTHER;
    const auto unsafe = unsafe_entries();
    if (!unsafe.empty()) {
        if (failed_entry) *failed_entry = unsafe[0];
        return E_UNSAFE_PATH;
    }
    UString d(dir.c_str());
    d.Replace(L'/', L'\\');
    while (d.Len() > 3 && d.Back() == L'\\') d.DeleteBack();
    if (!NDir::CreateComplexDir(us2fs(d))) return E_WRITE;
    d += L'\\';

    CExtractCallback* cbSpec = new CExtractCallback;
    cbSpec->Arc = impl_->arc;
    cbSpec->Dir = d;
    const std::optional<std::string>& pw = password ? password : impl_->password;
    cbSpec->HasPassword = pw.has_value();
    cbSpec->Password = FromUtf8(pw.value_or(""));
    cbSpec->OnProgress = &progress;
    return run_extract(impl_->arc, cbSpec, nullptr, (UInt32)(Int32)-1, failed_entry);
}

int Archive::extract_first(const std::wstring& target, const Progress& progress, std::string* failed_entry) {
    if (!impl_) return E_OTHER;
    CExtractCallback* cbSpec = new CExtractCallback;
    cbSpec->Arc = impl_->arc;
    cbSpec->SingleFile = UString(target.c_str());
    cbSpec->HasPassword = impl_->password.has_value();
    cbSpec->Password = FromUtf8(impl_->password.value_or(""));
    cbSpec->OnProgress = &progress;
    const UInt32 first = 0;
    return run_extract(impl_->arc, cbSpec, &first, 1, failed_entry);
}

}  // namespace sevenzip
