#include "commands.h"

#include <algorithm>

namespace {
char lower(char c) { return (c >= 'A' && c <= 'Z') ? static_cast<char>(c - 'A' + 'a') : c; }

std::string lowered(std::string s) {
    for (char& c : s) c = lower(c);
    return s;
}

bool alnum(char c) {
    const unsigned char u = static_cast<unsigned char>(c);
    return (u >= '0' && u <= '9') || (u >= 'a' && u <= 'z') || (u >= 'A' && u <= 'Z');
}

bool word_start(const std::string& t, size_t i) { return i == 0 || !alnum(t[i - 1]); }

std::optional<size_t> next_word_start(const std::string& t, char ch, size_t start) {
    for (size_t i = start; i < t.size(); ++i)
        if (t[i] == ch && word_start(t, i)) return i;
    return std::nullopt;
}

std::vector<std::string> split_words(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            if (!cur.empty()) out.push_back(std::move(cur));
            cur.clear();
        } else {
            cur += c;
        }
    }
    if (!cur.empty()) out.push_back(std::move(cur));
    return out;
}

std::string trimmed(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return {};
    return s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
}
}  // namespace

std::optional<FuzzyHit> fuzzy_score(const std::string& query, const std::string& text) {
    std::string q;
    for (char c : query)
        if (c != ' ') q += lower(c);
    if (q.empty()) return FuzzyHit{};
    const std::string t = lowered(text);
    FuzzyHit hit;
    size_t ti = 0;
    long prev = -2;
    for (char ch : q) {
        const size_t found = t.find(ch, ti);
        if (found == std::string::npos) return std::nullopt;
        long idx = static_cast<long>(found);
        // Prefer a later occurrence at a word start if it is close by.
        if (const auto ws = next_word_start(t, ch, ti);
            ws && static_cast<long>(*ws) != idx && static_cast<long>(*ws) - idx <= 8 && prev != idx - 1)
            idx = static_cast<long>(*ws);
        double bonus = 1.0;
        if (idx == prev + 1) bonus += 2.0;  // consecutive
        if (word_start(t, static_cast<size_t>(idx))) bonus += 1.5;
        if (prev >= 0) bonus -= std::min<long>(idx - prev - 1, 5) * 0.1;  // gap
        hit.score += bonus;
        hit.positions.push_back(static_cast<int>(idx));
        prev = idx;
        ti = static_cast<size_t>(idx) + 1;
    }
    hit.score -= hit.positions[0] * 0.05;  // earlier is better
    if (t.rfind(q, 0) == 0) hit.score += 2.0;
    return hit;
}

std::optional<double> keyword_score(const std::string& query, const std::string& text) {
    const auto terms = split_words(lowered(query));
    if (terms.empty()) return 0.0;
    const std::string hay = lowered(text);
    std::vector<std::string> words;  // runs of letters, digits and _ (Python's \w, ASCII)
    std::string cur;
    for (char c : hay) {
        if (alnum(c) || c == '_' || static_cast<unsigned char>(c) >= 0x80) cur += c;
        else if (!cur.empty()) { words.push_back(cur); cur.clear(); }
    }
    if (!cur.empty()) words.push_back(cur);
    double score = 0;
    for (const auto& term : terms) {
        if (hay.find(term) == std::string::npos) return std::nullopt;
        const bool starts = std::any_of(words.begin(), words.end(), [&](const std::string& w) { return w.rfind(term, 0) == 0; });
        score += starts ? 1.5 : 0.75;
    }
    return score;
}

std::vector<Match> search_commands(const std::vector<Command>& commands, const std::string& raw_query,
                                   const std::map<std::string, int>& usage, size_t limit) {
    std::string query = trimmed(raw_query);
    bool actions_only = false, jobs_only = false;
    if (!query.empty() && (query[0] == '>' || query[0] == '@')) {
        actions_only = query[0] == '>';
        jobs_only = query[0] == '@';
        query = trimmed(query.substr(1));
    }
    std::vector<Match> matches;
    for (const auto& c : commands) {
        if (actions_only && c.kind != CommandKind::Action) continue;
        if (jobs_only && c.kind == CommandKind::Action) continue;
        if (!c.applicable) continue;
        Match m{&c, 0, {}};
        if (auto hit = fuzzy_score(query, c.title)) {
            m.score = hit->score;
            m.positions = std::move(hit->positions);
        } else {
            std::string hay = c.title + " " + c.subtitle;
            for (const auto& k : c.keywords) hay += " " + k;
            const auto kw = keyword_score(query, hay);
            if (!kw) continue;
            m.score = *kw;  // a keyword hit: nothing to highlight in the title
        }
        if (auto it = usage.find(c.id); it != usage.end()) m.score += std::min(it->second, 10) * 0.3;
        if (c.kind == CommandKind::Job) m.score += 0.25;
        matches.push_back(std::move(m));
    }
    std::stable_sort(matches.begin(), matches.end(), [](const Match& a, const Match& b) {
        if (a.score != b.score) return a.score > b.score;
        return lowered(a.command->title) < lowered(b.command->title);
    });
    if (matches.size() > limit) matches.resize(limit);
    return matches;
}
