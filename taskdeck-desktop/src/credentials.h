// Secret job settings (API keys, the Google service-account key, archive passwords) in Windows Credential
// Manager: generic credentials, kept on this PC (not roamed), readable only by this Windows user while signed
// in. No extra prompt (decision 3 in docs/PLAN.md). They show in Control Panel > Credential Manager > Windows
// Credentials as "TaskDeck/settings:<job>:<field>", the phone vault's key names (core/secrets.py).
#pragma once
#include <optional>
#include <string>

namespace credentials {

// The credential target for one secret setting: "TaskDeck/settings:<settings id>:<field>".
std::wstring target(const std::string& settings_id, const std::string& field);

// The secret, or nullopt if none is stored. `error` is set when reading failed for another reason.
std::optional<std::string> read(const std::wstring& target, std::string* error = nullptr);
// Stores the secret (UTF-8). Values over Credential Manager's 2,560-byte limit are kept in parts:
// "<target>", "<target>#2", "<target>#3"... Returns false with `error` set if Windows refused.
bool write(const std::wstring& target, const std::string& value, std::string* error = nullptr);
// Removes the secret and its parts. Removing one that isn't there is fine.
void remove(const std::wstring& target);

}  // namespace credentials
