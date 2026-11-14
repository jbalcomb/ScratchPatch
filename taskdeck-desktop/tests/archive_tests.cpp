// Extract archive on 7-Zip: the same cases as the phone app's taskdeck/tests/test_archive.py, on its test
// archives (taskdeck/tests/fixtures/archives), plus the job's checks and a .tar.gz.
// Run: build\Release\archive_tests.exe (7z.dll next to it; exit code 0 = all passed). CI runs it too.
#include "../src/extract_job.h"
#include "../src/sevenzip.h"

#include <windows.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <string>

namespace fs = std::filesystem;
using namespace sevenzip;

static int g_failed = 0, g_checks = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        ++g_checks;                                                              \
        if (!(cond)) {                                                           \
            ++g_failed;                                                          \
            std::printf("FAILED %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
        }                                                                        \
    } while (0)

static const fs::path kFix = fs::path(TASKDECK_FIXTURES);
static const std::string kPassword = "Secret1";

static std::map<std::string, std::string> content() {
    return {{"docs/readme.txt", "hello archive\n"}, {"docs/notes/a.txt", "note a\n"}, {"big.txt", std::string(3000, 'x')}};
}

static std::map<std::string, std::string> files_under(const fs::path& root) {
    std::map<std::string, std::string> out;
    std::error_code ec;
    if (!fs::exists(root, ec)) return out;
    for (const auto& e : fs::recursive_directory_iterator(root)) {
        if (!e.is_regular_file()) continue;
        std::ifstream in(e.path(), std::ios::binary);
        std::stringstream s;
        s << in.rdbuf();
        out[fs::relative(e.path(), root).generic_string()] = s.str();
    }
    return out;
}

static fs::path fresh(const fs::path& base, const char* name) {
    const fs::path p = base / name;
    std::error_code ec;
    fs::remove_all(p, ec);
    return p;
}

static void list_and_extract(const fs::path& tmp) {
    for (const char* name : {"plain.zip", "plain.7z"}) {
        Archive a;
        CHECK(a.open((kFix / name).wstring(), std::nullopt) == OK);
        CHECK(a.format() == (std::string(name) == "plain.zip" ? "zip" : "7z"));
        std::map<std::string, size_t> sizes;
        for (const auto& e : a.entries())
            if (!e.dir) sizes[e.name] = e.size;
        std::map<std::string, size_t> expected;
        for (const auto& [k, v] : content()) expected[k] = v.size();
        CHECK(sizes == expected);
        uint64_t last_done = 0, last_total = 0;
        const fs::path out = fresh(tmp, "out");
        CHECK(a.extract(out.wstring(), std::nullopt, [&](uint64_t d, uint64_t t) { last_done = d; last_total = t; return false; },
                        nullptr) == OK);
        CHECK(files_under(out) == content());
        CHECK(last_done == 3000 + 14 + 7 && last_total == last_done);
    }
}

static void encrypted(const fs::path& tmp) {
    for (const char* name : {"aes.zip", "zipcrypto.zip", "data-encrypted.7z"}) {
        Archive a;
        CHECK(a.open((kFix / name).wstring(), std::nullopt) == OK);  // names are readable without the password
        bool all = true;
        for (const auto& e : a.entries())
            if (!e.dir) all &= e.encrypted;
        CHECK(all);
        CHECK(a.extract(fresh(tmp, "none").wstring(), std::nullopt, nullptr, nullptr) == E_NEED_PASSWORD);
        CHECK(a.extract(fresh(tmp, "wrong").wstring(), std::string("nope"), nullptr, nullptr) == E_WRONG_PASSWORD);
        const fs::path right = fresh(tmp, "right");
        CHECK(a.extract(right.wstring(), kPassword, nullptr, nullptr) == OK);
        CHECK(files_under(right) == content());
    }
    Archive h;
    CHECK(h.open((kFix / "headers-encrypted.7z").wstring(), std::nullopt) == E_NEED_PASSWORD);
    CHECK(h.open((kFix / "headers-encrypted.7z").wstring(), std::string("nope")) == E_WRONG_PASSWORD);
    CHECK(h.open((kFix / "headers-encrypted.7z").wstring(), kPassword) == OK);
    const fs::path out = fresh(tmp, "hidden");
    CHECK(h.extract(out.wstring(), std::nullopt, nullptr, nullptr) == OK);
    CHECK(files_under(out) == content());
}

static void safety_and_errors(const fs::path& tmp) {
    const fs::path out = fresh(tmp, "over");
    fs::create_directories(out / "docs");
    std::ofstream(out / "docs" / "readme.txt", std::ios::binary) << "old and longer than the new content";
    Archive a;
    CHECK(a.open((kFix / "plain.zip").wstring(), std::nullopt) == OK);
    CHECK(a.extract(out.wstring(), std::nullopt, nullptr, nullptr) == OK);
    CHECK(files_under(out)["docs/readme.txt"] == "hello archive\n");  // replaced

    Archive t;
    CHECK(t.open((kFix / "traversal.zip").wstring(), std::nullopt) == OK);
    CHECK(t.unsafe_entries() == std::vector<std::string>{"../evil.txt"});
    std::string failed;
    const fs::path tout = fresh(tmp, "trav");
    CHECK(t.extract(tout.wstring(), std::nullopt, nullptr, &failed) == E_UNSAFE_PATH);
    CHECK(failed == "../evil.txt");
    CHECK(!fs::exists(tout) && !fs::exists(tmp / "evil.txt"));

    Archive n;
    CHECK(n.open((kFix / "not-an-archive.zip").wstring(), std::nullopt) == E_UNSUPPORTED);
    CHECK(n.open((tmp / "missing.7z").wstring(), std::nullopt) == E_OPEN_FILE);

    Archive g;
    CHECK(g.open((kFix / "single.txt.gz").wstring(), std::nullopt) == OK);
    const fs::path gout = fresh(tmp, "gz");
    CHECK(g.extract(gout.wstring(), std::nullopt, nullptr, nullptr) == OK);
    const auto gfiles = files_under(gout);
    CHECK(gfiles.size() == 1 && gfiles.begin()->second == "hello archive\n");

    Archive c;  // cancel from the progress callback
    CHECK(c.open((kFix / "plain.7z").wstring(), std::nullopt) == OK);
    CHECK(c.extract(fresh(tmp, "cancel").wstring(), std::nullopt, [](uint64_t, uint64_t) { return true; }, nullptr) == E_CANCELLED);

    CHECK(message(E_WRONG_PASSWORD, "d.txt") == "Wrong password. (d.txt)");
    std::string rel;
    CHECK(safe_relative("./docs/a.txt", &rel) && rel == "docs/a.txt");
    CHECK(safe_relative("/etc/passwd", &rel) && rel == "etc/passwd");
    CHECK(safe_relative("C:\\x\\y", &rel) && rel == "x/y");
    CHECK(safe_relative(".", &rel) && rel.empty());  // the folder itself
    CHECK(!safe_relative("docs/../../evil", &rel) && !safe_relative("..\\evil", &rel));
}

static void rar(const fs::path& tmp) {
    Archive a;
    CHECK(a.open((kFix / "rar5-plain.rar").wstring(), std::nullopt) == OK);
    CHECK(a.format() == "Rar5" && a.entries().size() == 4);
    const fs::path out = fresh(tmp, "rar5");
    CHECK(a.extract(out.wstring(), std::nullopt, nullptr, nullptr) == OK);
    CHECK(files_under(out).size() == 4 && files_under(out).count("test1.bin"));

    for (const char* name : {"rar4-encrypted-names.rar", "rar5-encrypted-names.rar"}) {
        Archive r;
        CHECK(r.open((kFix / name).wstring(), std::nullopt) == E_NEED_PASSWORD);
        CHECK(r.open((kFix / name).wstring(), std::string("wrong")) == E_WRONG_PASSWORD);
        CHECK(r.open((kFix / name).wstring(), std::string("password")) == OK);
        const fs::path rout = fresh(tmp, "rarenc");
        CHECK(r.extract(rout.wstring(), std::nullopt, nullptr, nullptr) == OK);
        CHECK(!files_under(rout).empty());
    }
    Archive m;
    CHECK(m.open((kFix / "rar5-mixed-passwords.rar").wstring(), std::nullopt) == OK);
    std::string failed;
    CHECK(m.extract(fresh(tmp, "mixed").wstring(), std::string("password"), nullptr, &failed) == E_WRONG_PASSWORD);
    CHECK(failed == "d.txt");
}

// The job: the folder name, the checks before starting, a run with phases, a .tar.gz in two passes.
static void job(const fs::path& tmp) {
    CHECK(extract::archive_stem("CoM2ModWarlord1.5.12.6.2.7z") == "CoM2ModWarlord1.5.12.6.2");
    CHECK(extract::archive_stem("site.tar.gz") == "site");
    CHECK(extract::archive_stem("site.tgz") == "site");
    CHECK(extract::archive_stem("notes.txt") == "notes");
    CHECK(extract::destination(kFix / "plain.7z", "") == kFix / "plain");
    CHECK(extract::destination(kFix / "plain.7z", "C:\\x") == fs::path("C:\\x"));
    CHECK(extract::human_size(820) == "820 bytes" && extract::human_size(1536) == "1.5 KB");
    CHECK(extract::thousands(1234567) == "1,234,567");

    auto s = extract::scan(kFix / "plain.7z", std::nullopt);
    CHECK(s.error.empty() && s.files == 3 && s.unpacked == 3021 && !s.encrypted);
    CHECK(extract::problems(s, tmp / "new-folder", "").empty());  // created when extracting
    CHECK(!extract::problems(s, tmp / "no" / "such" / "folder", "").empty());
    s = extract::scan(kFix / "aes.zip", std::nullopt);
    CHECK(s.encrypted && extract::problems(s, tmp, "") == std::vector<std::string>{"Enter the archive's password."});
    CHECK(extract::problems(s, tmp, kPassword).empty());
    s = extract::scan(kFix / "headers-encrypted.7z", std::nullopt);
    CHECK(s.names_hidden && s.encrypted && s.error.empty());
    s = extract::scan(kFix / "traversal.zip", std::nullopt);
    CHECK(s.error.find("outside the folder") != std::string::npos);

    std::vector<std::string> labels;
    uint64_t done = 0;
    extract::Reporter r;
    r.set_phases = [&](const std::vector<std::pair<std::string, uint64_t>>& p) { for (auto& x : p) labels.push_back(x.first); };
    r.advance = [&](int, uint64_t d, uint64_t) { done = d; };
    r.cancelled = [] { return false; };
    const fs::path out = fresh(tmp, "job");
    auto o = extract::run(kFix / "plain.7z", out, std::nullopt, r);
    CHECK(o.status == extract::Outcome::Done);
    CHECK(o.message == "Extracted 3 files (3.0 KB) to " + extract::utf8(out));
    CHECK(labels == std::vector<std::string>{"Extracting"} && done == 3021);
    CHECK(files_under(out) == content());

    r.cancelled = [] { return true; };
    o = extract::run(kFix / "plain.7z", fresh(tmp, "job2"), std::nullopt, r);
    CHECK(o.status == extract::Outcome::Cancelled && o.message.rfind("Stopped.", 0) == 0);
    r.cancelled = [] { return false; };
    o = extract::run(kFix / "aes.zip", fresh(tmp, "job3"), std::string("nope"), r);
    CHECK(o.status == extract::Outcome::Failed && o.message.rfind("Wrong password.", 0) == 0);

    // A .tar.gz, made with Windows' own tar (bsdtar) from the extracted files. "-C folder ." as people make them:
    // the tar then holds "./" (the folder itself) and "./docs/...".
    const fs::path tgz = tmp / "site.tar.gz";
    std::error_code ec;
    fs::remove(tgz, ec);
    wchar_t sys[MAX_PATH];
    ::GetSystemDirectoryW(sys, MAX_PATH);
    const std::wstring cmd = L"\"\"" + std::wstring(sys) + L"\\tar.exe\" -czf \"" + tgz.wstring() + L"\" -C \"" + out.wstring() + L"\" .\"";
    if (_wsystem(cmd.c_str()) != 0 || !fs::exists(tgz)) {
        std::printf("skipped: .tar.gz (tar.exe didn't make one)\n");
        return;
    }
    s = extract::scan(tgz, std::nullopt);
    CHECK(s.compressed_tar && s.error.empty() && s.format == "tar.gzip");
    labels.clear();
    const fs::path tout = fresh(tmp, "site");
    o = extract::run(tgz, tout, std::nullopt, r);
    CHECK(o.status == extract::Outcome::Done);
    CHECK(labels == (std::vector<std::string>{"Decompressing", "Extracting"}));
    CHECK(files_under(tout) == content());  // and the temporary .tar is gone
}

int main() {
    wchar_t exe[MAX_PATH];
    const DWORD n = ::GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::string why;
    if (!load((fs::path(std::wstring(exe, n)).parent_path() / L"7z.dll").wstring(), &why)) {
        std::printf("7z.dll didn't load: %s\n", why.c_str());
        return 1;
    }
    const fs::path tmp = fs::temp_directory_path() / L"taskdeck-archive-tests";
    std::error_code ec;
    fs::remove_all(tmp, ec);
    fs::create_directories(tmp);
    list_and_extract(tmp);
    encrypted(tmp);
    safety_and_errors(tmp);
    rar(tmp);
    job(tmp);
    fs::remove_all(tmp, ec);
    std::printf("%d checks, %d failed (7-Zip %s)\n", g_checks, g_failed, version().c_str());
    return g_failed ? 1 : 0;
}
