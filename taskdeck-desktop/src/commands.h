// The command palette's matching and ranking, ported from the phone app (taskdeck/src/taskdeck/core/commands.py)
// so both rank the same way. Query prefixes work like VS Code's quick open: ">" actions only, "@" jobs (and
// job presets) only, no prefix: everything, jobs first on ties. Text is matched byte by byte, lowercasing ASCII.
#pragma once
#include <map>
#include <optional>
#include <string>
#include <vector>

enum class CommandKind { Job, Quick, Action };

struct Command {
    std::string id, title, subtitle, shortcut;
    CommandKind kind = CommandKind::Action;
    std::vector<std::string> keywords;
    bool applicable = true;  // e.g. "Unpin current job" only while a pinned job is open
};

struct FuzzyHit {
    double score = 0;
    std::vector<int> positions;  // matched byte offsets in the title
};

// How well `query` matches `text` as an in-order subsequence (case-insensitive), or nullopt if some query
// character isn't found. Consecutive runs, word starts and an early first hit add points; gaps cost a little.
std::optional<FuzzyHit> fuzzy_score(const std::string& query, const std::string& text);

// Looser: every query word occurs in `text`. Scored below a typical title hit, so titles win ties.
std::optional<double> keyword_score(const std::string& query, const std::string& text);

struct Match {
    const Command* command;
    double score;
    std::vector<int> positions;
};

// Applicable commands matching `query`, best first. `usage` (command id -> times used) adds a small boost, so
// the palette learns habits.
std::vector<Match> search_commands(const std::vector<Command>& commands, const std::string& query,
                                   const std::map<std::string, int>& usage = {}, size_t limit = 50);
