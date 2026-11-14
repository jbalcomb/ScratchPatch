// sevenzip.h without 7-Zip: for compilers 7z.dll isn't built with yet (CMakeLists.txt). Every call says the
// engine is missing, so Extract archive reports that instead of failing to build.
#include "sevenzip.h"

namespace sevenzip {

std::string message(int code, const std::string&) {
    return code == E_LOAD ? "The archive engine (7z.dll) isn't part of this build." : "Extracting failed.";
}
bool load(const std::wstring&, std::string* error) {
    if (error) *error = "not built with this compiler";
    return false;
}
bool loaded() { return false; }
std::string version() { return ""; }
bool safe_relative(const std::string& name, std::string* out) {
    if (out) *out = name;
    return name.find("..") == std::string::npos;
}

struct Archive::Impl {};
Archive::Archive() = default;
Archive::~Archive() = default;
int Archive::open(const std::wstring&, const std::optional<std::string>&) { return E_LOAD; }
const std::string& Archive::format() const {
    static const std::string none;
    return none;
}
std::vector<Entry> Archive::entries() const { return {}; }
std::vector<std::string> Archive::unsafe_entries() const { return {}; }
int Archive::extract(const std::wstring&, const std::optional<std::string>&, const Progress&, std::string*) { return E_LOAD; }
int Archive::extract_first(const std::wstring&, const Progress&, std::string*) { return E_LOAD; }

}  // namespace sevenzip
