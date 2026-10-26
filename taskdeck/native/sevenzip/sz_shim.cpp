// sz_shim.cpp: a plain C API over 7-Zip's archive library (7z.so), for Python's ctypes.
//
// 7-Zip's library speaks C++ COM-style interfaces (IInArchive, callbacks...). This file
// implements those callbacks once and exposes a handful of C functions:
//
//   sz_load(lib)            load 7z.so (lib7z.so on Android)
//   sz_open(path, pw, &a)   open an archive (format from extension or signature), optional password
//   sz_count / sz_item      list entries (name, size, folder?, encrypted?)
//   sz_extract(a, dir, ...) extract everything into dir, overwriting, with progress + cancel
//   sz_close(a)
//
// Based on 7-Zip's own example client (CPP/7zip/UI/Client7z/Client7z.cpp). Build: build.sh.

#include "Common/Common.h"

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

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

#include "7zip/Common/FileStreams.h"
#include "7zip/Archive/IArchive.h"
#include "7zip/IPassword.h"
#include "../C/7zVersion.h"

using namespace NWindows;
using namespace NFile;

#define SZ_API extern "C" __attribute__((visibility("default")))

enum
{
  SZR_OK = 0,
  SZR_E_LOAD = 1,            // 7z library not loaded / not found
  SZR_E_OPEN_FILE = 2,       // can't read the archive file
  SZR_E_UNSUPPORTED = 3,     // not an archive 7-Zip can open
  SZR_E_NEED_PASSWORD = 4,
  SZR_E_WRONG_PASSWORD = 5,
  SZR_E_DATA = 6,            // damaged archive / CRC error
  SZR_E_CANCELLED = 7,
  SZR_E_WRITE = 8,           // can't create a folder or file (permission, disk full)
  SZR_E_UNSAFE_PATH = 9,     // entry would land outside the target folder
  SZR_E_UNSUPPORTED_METHOD = 10,
  SZR_E_OTHER = 11
};

typedef int (*sz_progress_cb)(UInt64 done, UInt64 total, void *user);  // non-zero = cancel

// ---- the library ------------------------------------------------------------------

static void *g_lib = NULL;
static Func_CreateObject g_CreateObject = NULL;
static Func_GetNumberOfFormats g_GetNumberOfFormats = NULL;
static Func_GetHandlerProperty2 g_GetHandlerProperty2 = NULL;

SZ_API int sz_load(const char *lib_path)
{
  if (g_lib)
    return SZR_OK;
  void *lib = dlopen(lib_path, RTLD_NOW | RTLD_LOCAL);
  if (!lib)
    return SZR_E_LOAD;
  g_CreateObject = (Func_CreateObject)dlsym(lib, "CreateObject");
  g_GetNumberOfFormats = (Func_GetNumberOfFormats)dlsym(lib, "GetNumberOfFormats");
  g_GetHandlerProperty2 = (Func_GetHandlerProperty2)dlsym(lib, "GetHandlerProperty2");
  if (!g_CreateObject || !g_GetNumberOfFormats || !g_GetHandlerProperty2)
  {
    dlclose(lib);
    return SZR_E_LOAD;
  }
  g_lib = lib;
  return SZR_OK;
}

SZ_API const char *sz_version(void)
{
  return MY_VERSION;
}

SZ_API const char *sz_load_error(void)
{
  const char *e = dlerror();
  return e ? e : "";
}

// ---- helpers ------------------------------------------------------------------------

static UString FromUtf8(const char *s)
{
  UString u;
  if (s)
    ConvertUTF8ToUnicode(AString(s), u);
  return u;
}

static AString ToUtf8(const UString &u)
{
  AString a;
  ConvertUnicodeToUTF8(u, a);
  return a;
}

static bool GetBoolProp(IInArchive *arc, UInt32 index, PROPID id)
{
  NCOM::CPropVariant prop;
  if (arc->GetProperty(index, id, &prop) != S_OK)
    return false;
  return prop.vt == VT_BOOL && VARIANT_BOOLToBool(prop.boolVal);
}

static UString GetPathProp(IInArchive *arc, UInt32 index)
{
  NCOM::CPropVariant prop;
  if (arc->GetProperty(index, kpidPath, &prop) == S_OK && prop.vt == VT_BSTR)
    return UString(prop.bstrVal);
  return UString(L"[content]");  // e.g. the single file inside a .gz
}

// Keep entries inside the target folder: drop leading slashes and "./", refuse "..".
static bool SafeRelativePath(const UString &in, UString &out)
{
  UString s = in;
  s.Replace(L'\\', L'/');
  out.Empty();
  int start = 0;
  const int len = (int)s.Len();
  while (start <= len)
  {
    int end = s.Find(L'/', (unsigned)start);
    if (end < 0)
      end = len;
    UString part = s.Mid((unsigned)start, (unsigned)(end - start));
    if (part == L"..")
      return false;
    if (!part.IsEmpty() && part != L".")
    {
      if (!out.IsEmpty())
        out += L'/';
      out += part;
    }
    start = end + 1;
  }
  return !out.IsEmpty();
}

// ---- open callback (asks for the password of encrypted headers) ----------------------

class COpenCallback Z7_final:
  public IArchiveOpenCallback,
  public ICryptoGetTextPassword,
  public CMyUnknownImp
{
  Z7_IFACES_IMP_UNK_2(IArchiveOpenCallback, ICryptoGetTextPassword)
public:
  bool HasPassword;
  bool PasswordAsked;
  UString Password;
  COpenCallback(): HasPassword(false), PasswordAsked(false) {}
};

Z7_COM7F_IMF(COpenCallback::SetTotal(const UInt64 *, const UInt64 *)) { return S_OK; }
Z7_COM7F_IMF(COpenCallback::SetCompleted(const UInt64 *, const UInt64 *)) { return S_OK; }

Z7_COM7F_IMF(COpenCallback::CryptoGetTextPassword(BSTR *password))
{
  PasswordAsked = true;
  if (!HasPassword)
    return E_ABORT;
  return StringToBstr(Password, password);
}

// ---- the archive handle ----------------------------------------------------------------

struct sz_archive
{
  CMyComPtr<IInArchive> arc;
  CMyComPtr<IInStream> file;
  AString format;
  UString password;
  bool hasPassword;
};

struct CFormat
{
  GUID clsid;
  AString name;
  AString exts;   // space-separated, lowercase
  CByteBuffer sig;
  CObjectVector<CByteBuffer> multiSig;
  UInt32 sigOffset;
};

static bool GetFormats(CObjectVector<CFormat> &formats)
{
  UInt32 n = 0;
  if (g_GetNumberOfFormats(&n) != S_OK)
    return false;
  for (UInt32 i = 0; i < n; i++)
  {
    CFormat f;
    NCOM::CPropVariant prop;
    if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kClassID, &prop) != S_OK
        || prop.vt != VT_BSTR || SysStringByteLen(prop.bstrVal) != sizeof(GUID))
      continue;
    memcpy(&f.clsid, prop.bstrVal, sizeof(GUID));
    prop.Clear();
    if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kName, &prop) == S_OK && prop.vt == VT_BSTR)
      f.name = ToUtf8(UString(prop.bstrVal));
    prop.Clear();
    if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kExtension, &prop) == S_OK && prop.vt == VT_BSTR)
    {
      f.exts = ToUtf8(UString(prop.bstrVal));
      f.exts.MakeLower_Ascii();
    }
    prop.Clear();
    if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kSignature, &prop) == S_OK && prop.vt == VT_BSTR)
      f.sig.CopyFrom((const Byte *)prop.bstrVal, SysStringByteLen(prop.bstrVal));
    prop.Clear();
    if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kMultiSignature, &prop) == S_OK && prop.vt == VT_BSTR)
    {
      const Byte *p = (const Byte *)prop.bstrVal;
      UInt32 size = SysStringByteLen(prop.bstrVal);
      while (size > 0)
      {
        UInt32 len = *p++;
        size--;
        if (len > size)
          break;
        CByteBuffer b;
        b.CopyFrom(p, len);
        f.multiSig.Add(b);
        p += len;
        size -= len;
      }
    }
    prop.Clear();
    f.sigOffset = 0;
    if (g_GetHandlerProperty2(i, NArchive::NHandlerPropID::kSignatureOffset, &prop) == S_OK && prop.vt == VT_UI4)
      f.sigOffset = prop.ulVal;
    formats.Add(f);
  }
  return true;
}

static bool HasExt(const AString &exts, const AString &ext)
{
  if (ext.IsEmpty())
    return false;
  int pos = 0;
  const int len = (int)exts.Len();
  while (pos < len)
  {
    int end = exts.Find(' ', (unsigned)pos);
    if (end < 0)
      end = len;
    if (exts.Mid((unsigned)pos, (unsigned)(end - pos)) == ext)
      return true;
    pos = end + 1;
  }
  return false;
}

static bool SigMatches(const CByteBuffer &sig, UInt32 offset, const Byte *head, size_t headLen)
{
  if (sig.Size() == 0 || offset + sig.Size() > headLen)
    return false;
  return memcmp(head + offset, sig, sig.Size()) == 0;
}

SZ_API int sz_open(const char *path_utf8, const char *password_utf8, sz_archive **out)
{
  *out = NULL;
  if (!g_lib)
    return SZR_E_LOAD;

  CInFileStream *fileSpec = new CInFileStream;
  CMyComPtr<IInStream> file = fileSpec;
  if (!fileSpec->Open(FString(path_utf8)))
    return SZR_E_OPEN_FILE;

  Byte head[1 << 12];
  UInt32 headLen = 0;
  file->Read(head, sizeof(head), &headLen);

  AString ext;
  {
    AString p(path_utf8);
    int dot = p.ReverseFind('.');
    int slash = p.ReverseFind('/');
    if (dot > slash)
      ext = p.Ptr((unsigned)dot + 1);
    ext.MakeLower_Ascii();
  }

  CObjectVector<CFormat> formats;
  if (!GetFormats(formats))
    return SZR_E_LOAD;

  // Candidates: formats claiming this extension first, then formats whose signature matches.
  CRecordVector<unsigned> order;
  for (unsigned i = 0; i < formats.Size(); i++)
    if (HasExt(formats[i].exts, ext))
      order.Add(i);
  for (unsigned i = 0; i < formats.Size(); i++)
  {
    bool match = SigMatches(formats[i].sig, formats[i].sigOffset, head, headLen);
    for (unsigned k = 0; !match && k < formats[i].multiSig.Size(); k++)
      match = SigMatches(formats[i].multiSig[k], formats[i].sigOffset, head, headLen);
    if (match && order.FindInSorted(i) < 0)
    {
      bool already = false;
      for (unsigned k = 0; k < order.Size(); k++)
        if (order[k] == i)
          already = true;
      if (!already)
        order.Add(i);
    }
  }

  const bool hasPassword = password_utf8 != NULL;
  const UString password = FromUtf8(password_utf8);
  bool passwordAsked = false;

  for (unsigned k = 0; k < order.Size(); k++)
  {
    const CFormat &f = formats[order[k]];
    CMyComPtr<IInArchive> arc;
    if (g_CreateObject(&f.clsid, &IID_IInArchive, (void **)&arc) != S_OK || !arc)
      continue;
    if (file->Seek(0, STREAM_SEEK_SET, NULL) != S_OK)
      return SZR_E_OPEN_FILE;
    COpenCallback *cbSpec = new COpenCallback;
    CMyComPtr<IArchiveOpenCallback> cb(cbSpec);
    cbSpec->HasPassword = hasPassword;
    cbSpec->Password = password;
    const UInt64 scanSize = 1 << 23;
    const HRESULT res = arc->Open(file, &scanSize, cb);
    if (cbSpec->PasswordAsked)
      passwordAsked = true;
    if (res == S_OK)
    {
      sz_archive *a = new sz_archive;
      a->arc = arc;
      a->file = file;
      a->format = f.name;
      a->password = password;
      a->hasPassword = hasPassword;
      *out = a;
      return SZR_OK;
    }
    arc->Close();
    if (cbSpec->PasswordAsked)  // encrypted headers: the right format, the wrong (or no) password
      return hasPassword ? SZR_E_WRONG_PASSWORD : SZR_E_NEED_PASSWORD;
  }
  if (passwordAsked)
    return hasPassword ? SZR_E_WRONG_PASSWORD : SZR_E_NEED_PASSWORD;
  return SZR_E_UNSUPPORTED;
}

SZ_API void sz_close(sz_archive *a)
{
  if (!a)
    return;
  if (a->arc)
    a->arc->Close();
  delete a;
}

SZ_API const char *sz_format(sz_archive *a)
{
  return a ? a->format.Ptr() : "";
}

SZ_API UInt32 sz_count(sz_archive *a)
{
  UInt32 n = 0;
  if (a && a->arc)
    a->arc->GetNumberOfItems(&n);
  return n;
}

// Name (UTF-8, truncated to buf_len-1), size, folder?, encrypted? of entry i. Returns the full name length.
SZ_API UInt32 sz_item(sz_archive *a, UInt32 i, char *name_buf, UInt32 buf_len,
    UInt64 *size, int *is_dir, int *encrypted)
{
  AString name = ToUtf8(GetPathProp(a->arc, i));
  if (name_buf && buf_len)
  {
    UInt32 n = name.Len() < buf_len - 1 ? name.Len() : buf_len - 1;
    memcpy(name_buf, name.Ptr(), n);
    name_buf[n] = 0;
  }
  if (size)
  {
    NCOM::CPropVariant prop;
    UInt64 v = 0;
    if (a->arc->GetProperty(i, kpidSize, &prop) == S_OK)
      ConvertPropVariantToUInt64(prop, v);
    *size = v;
  }
  if (is_dir)
    *is_dir = GetBoolProp(a->arc, i, kpidIsDir) ? 1 : 0;
  if (encrypted)
    *encrypted = GetBoolProp(a->arc, i, kpidEncrypted) ? 1 : 0;
  return name.Len();
}

// ---- extract callback ----------------------------------------------------------------------

class CExtractCallback Z7_final:
  public IArchiveExtractCallback,
  public ICryptoGetTextPassword,
  public CMyUnknownImp
{
  Z7_IFACES_IMP_UNK_2(IArchiveExtractCallback, ICryptoGetTextPassword)
  Z7_IFACE_COM7_IMP(IProgress)

  COutFileStream *_outSpec;
  CMyComPtr<ISequentialOutStream> _out;
  bool _encrypted;
public:
  CMyComPtr<IInArchive> Arc;
  AString Dir;             // ends with '/'
  bool HasPassword;
  bool PasswordAsked;
  UString Password;
  sz_progress_cb Progress;
  void *User;
  UInt64 Total;
  int Error;               // first error, SZ_*
  AString ErrorName;       // entry it happened on

  CExtractCallback(): _outSpec(NULL), _encrypted(false), HasPassword(false), PasswordAsked(false),
      Progress(NULL), User(NULL), Total(0), Error(SZR_OK) {}

  void Fail(int code, const UString &name)
  {
    if (Error == SZR_OK)
    {
      Error = code;
      ErrorName = ToUtf8(name);
    }
  }
  UString Current;
};

Z7_COM7F_IMF(CExtractCallback::SetTotal(UInt64 size))
{
  Total = size;
  return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::SetCompleted(const UInt64 *completeValue))
{
  if (Progress && completeValue && Progress(*completeValue, Total, User) != 0)
  {
    Fail(SZR_E_CANCELLED, Current);
    return E_ABORT;
  }
  return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::GetStream(UInt32 index, ISequentialOutStream **outStream, Int32 askExtractMode))
{
  *outStream = NULL;
  _out.Release();
  _outSpec = NULL;
  Current = GetPathProp(Arc, index);
  _encrypted = GetBoolProp(Arc, index, kpidEncrypted);
  if (askExtractMode != NArchive::NExtract::NAskMode::kExtract)
    return S_OK;

  UString rel;
  if (!SafeRelativePath(Current, rel))
  {
    Fail(SZR_E_UNSAFE_PATH, Current);
    return E_ABORT;
  }
  const AString full = Dir + ToUtf8(rel);
  const FString fullPath(full.Ptr());

  if (GetBoolProp(Arc, index, kpidIsDir))
  {
    if (!NDir::CreateComplexDir(fullPath))
    {
      Fail(SZR_E_WRITE, Current);
      return E_ABORT;
    }
    return S_OK;
  }
  const int slash = full.ReverseFind('/');
  if (slash > 0 && !NDir::CreateComplexDir(FString(full.Left((unsigned)slash).Ptr())))
  {
    Fail(SZR_E_WRITE, Current);
    return E_ABORT;
  }
  NFind::CFileInfo fi;
  if (fi.Find(fullPath) && !NDir::DeleteFileAlways(fullPath))  // existing files are overwritten
  {
    Fail(SZR_E_WRITE, Current);
    return E_ABORT;
  }
  _outSpec = new COutFileStream;
  CMyComPtr<ISequentialOutStream> loc(_outSpec);
  if (!_outSpec->Create_ALWAYS(fullPath))
  {
    Fail(SZR_E_WRITE, Current);
    return E_ABORT;
  }
  _out = loc;
  *outStream = loc.Detach();
  return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::PrepareOperation(Int32)) { return S_OK; }

Z7_COM7F_IMF(CExtractCallback::SetOperationResult(Int32 result))
{
  if (_out)
  {
    _outSpec->Close();
    _out.Release();
  }
  using namespace NArchive::NExtract::NOperationResult;
  switch (result)
  {
    case kOK: break;
    case kWrongPassword: Fail(SZR_E_WRONG_PASSWORD, Current); break;
    case kUnsupportedMethod: Fail(SZR_E_UNSUPPORTED_METHOD, Current); break;
    case kCRCError:
    case kDataError:
      // With encryption, bad data almost always means a wrong password.
      Fail(_encrypted ? SZR_E_WRONG_PASSWORD : SZR_E_DATA, Current);
      break;
    default: Fail(SZR_E_DATA, Current); break;
  }
  return S_OK;
}

Z7_COM7F_IMF(CExtractCallback::CryptoGetTextPassword(BSTR *password))
{
  PasswordAsked = true;
  if (!HasPassword)
  {
    Fail(SZR_E_NEED_PASSWORD, Current);
    return E_ABORT;
  }
  return StringToBstr(Password, password);
}

// Extract every entry into out_dir (created if needed), overwriting existing files.
// password may be NULL (then the one given to sz_open, if any, is used).
// On failure, the entry's name is copied to failed_name (UTF-8).
SZ_API int sz_extract(sz_archive *a, const char *out_dir_utf8, const char *password_utf8,
    sz_progress_cb progress, void *user, char *failed_name, UInt32 failed_len)
{
  if (!a || !a->arc)
    return SZR_E_OTHER;
  AString dir(out_dir_utf8);
  while (dir.Len() > 1 && dir.Back() == '/')
    dir.DeleteBack();
  if (!NDir::CreateComplexDir(FString(dir.Ptr())))
    return SZR_E_WRITE;
  dir += '/';

  CExtractCallback *cbSpec = new CExtractCallback;
  CMyComPtr<IArchiveExtractCallback> cb(cbSpec);
  cbSpec->Arc = a->arc;
  cbSpec->Dir = dir;
  cbSpec->HasPassword = password_utf8 != NULL || a->hasPassword;
  cbSpec->Password = password_utf8 ? FromUtf8(password_utf8) : a->password;
  cbSpec->Progress = progress;
  cbSpec->User = user;

  const HRESULT res = a->arc->Extract(NULL, (UInt32)(Int32)-1, 0, cb);
  int code = cbSpec->Error;
  if (code == SZR_OK && res != S_OK)
    code = res == E_ABORT ? SZR_E_CANCELLED : SZR_E_OTHER;
  if (code != SZR_OK && failed_name && failed_len)
  {
    UInt32 n = cbSpec->ErrorName.Len() < failed_len - 1 ? cbSpec->ErrorName.Len() : failed_len - 1;
    memcpy(failed_name, cbSpec->ErrorName.Ptr(), n);
    failed_name[n] = 0;
  }
  return code;
}
