# ReMoM Multiplayer: what needs figuring out

A scoping study for adding multiplayer to [ReMoM](https://github.com/jbalcomb/ReMoM), the Master of Magic
reconstruction. No code yet. This is the list of decisions, the parts of the codebase that get in the way, and a
suggested order of work.

Surveyed against ReMoM `main` at commit `a9cc082`. Counts below are word-boundary `grep` results over `MoM/`,
`MoX/` and `src/`, so treat them as rough sizes, not exact numbers.

---

## TL;DR

1. **Struct sizes are not the problem.** Every owner field is already an `int8_t`, and the game already has 5 wizard
   slots plus a neutral player. Multiplayer with **up to 5 humans sharing the existing 5 wizard slots needs no
   change to any struct size or to the save format.**
2. **The problem is the assumption that player 0 is the only human.** `HUMAN_PLAYER_IDX` (hard-wired to `0`) shows
   up about **844 times**. Most of the work is changing "is this player 0?" into "is this player a human?" and
   "is this the player sitting at this screen?". Those are two different questions, and the code currently uses one
   constant for both.
3. **Some game state only exists for the human.** Fog of war (`_square_explored`), turn reports, messages and
   advisors all track one viewer. These need a copy per human player.
4. **Networked play needs a deterministic simulation.** ReMoM is already set up for this: there is a seedable,
   counted RNG, a record/replay layer and a headless build. One thing breaks it: drawing code also calls the game
   RNG.
5. **The fidelity rule in `AGENTS.md` (100% faithful to the disassembly) conflicts with multiplayer.** That rule
   has to be settled first; see Decision 0.
6. **Removing the fixed caps can be done.** The trick is to keep indices as they are and change what sits behind
   them. Details are in [Removing the static caps](#removing-the-static-caps). It is a separate project from
   multiplayer, and it should come after it.

---

## Decision 0: fidelity versus multiplayer

`AGENTS.md` makes faithfulness to the disassembly a hard rule, and the test suite compares against original-game
captures. Multiplayer means changing logic in hundreds of places that are faithful today. Options:

| Option | What it means | Cost |
|---|---|---|
| **A. Long-lived fork/branch** | Multiplayer lives on its own branch and keeps merging `main` | Merge conflicts in hot files (`DIPLOMAC.c`, `NEXTTURN.c`, `Combat.c`) for as long as reconstruction continues |
| **B. Compile-time mode** (recommended) | `#ifdef REMOM_MULTIPLAYER` / a separate target, like `HeMoM` | The OG build stays byte-for-byte faithful; changes follow the existing "comment out original, add replacement, mark `/* CLAUDE */`" convention |
| **C. Runtime abstraction everywhere** | Swap `HUMAN_PLAYER_IDX` for `Player_Is_Human(idx)` in `main` and have it return `idx == 0` in classic mode | Cleanest in the long run. It touches faithful code in `main`, though, so the rule would need an explicit exception |

**My recommendation:** B for the first prototype, then move to C once the abstraction has proved itself. In classic
mode, `Player_Is_Human(i)` compiles to `(i) == HUMAN_PLAYER_IDX`, so behaviour stays identical and the existing
tests keep passing.

---

## Decision 1: what kind of multiplayer?

MoM is turn-based, so you have choices a real-time game doesn't. In order of cost:

| Mode | Description | Needs determinism? | Needs networking? | Relative cost |
|---|---|---|---|---|
| **Hotseat** | Humans take turns at one machine | No | No | 1x. Mostly the "who is human" refactor plus per-player views |
| **PBEM / async** | Each player takes a turn and sends the save file on | No | No (file transfer) | ~1.2x. Hotseat plus save hand-off and a turn token |
| **Lockstep, turn-based** | Every peer runs the same simulation; only orders and the seed travel | **Yes** | Yes (small) | ~2–3x |
| **Authoritative server** | One host simulates; clients see filtered state | No (host is truth) | Yes (state diffs) | ~3–4x. Needs state serialization and hidden-information filtering |

**Recommendation:** start with **hotseat**. Every other mode needs the same groundwork (Phases 1–3 below), and it
can be tested with the existing `HeMoM` headless player without writing any network code. Pick between lockstep
and server once hotseat works.

Questions only you can answer:

- **Turn order:** MoM is "human moves, then `Next_Turn_Calc()` runs every AI". With several humans, do they move
  one after another, or simultaneously with their orders resolved together? Simultaneous turns are much nicer
  online but are a real design change. Two players moving into the same square, or attacking the same city, need
  resolution rules the original never had.
- **Human-versus-human tactical combat:** the battle screen assumes one side is AI-driven (`CMBTAI.c`). Should two
  humans fight battles interactively, which means both have to be online, or do battles between humans
  auto-resolve through `Strategic_Combat()`?
- **Human-to-human diplomacy:** `DIPLOMAC.c` (216 `HUMAN_PLAYER_IDX` hits) is the human talking to an AI
  personality. Between humans it probably becomes a simple trade/treaty proposal screen, which would be a new UI.
- **Player count:** do you want more than 5 wizards? That is a separate and much larger job. See the next section.

---

## What has to change (the multiplayer work proper)

### 1. "Human" versus "local viewer"

`HUMAN_PLAYER_IDX` (defined as `0` in `MoX/src/MOX_DEF.h`) is used for two different things:

- **Game rules:** AI skips its own logic for the human, the difficulty modifiers apply only to AI, and some events
  target only the human.
- **Presentation:** whose fog of war is drawn, whose messages pop up, whose spellbook opens, whose banner sits in
  the HUD.

Introduce two concepts:

```c
int16_t Player_Is_Human(int16_t player_idx);   /* rules:        is this slot human-controlled?    */
extern int16_t _local_player_idx;              /* presentation: who is looking at the screen now? */
```

Every one of the ~844 sites has to be classified as one or the other. The heaviest files:

| File | `HUMAN_PLAYER_IDX` hits | Notes |
|---|---|---|
| `MoM/src/DIPLOMAC.c` | 216 | Mostly human↔AI conversation logic |
| `MoM/src/Spellbook.c` | 68 | UI / presentation |
| `MoM/src/NEXTTURN.c` | 53 | Turn processing, mixed rules and messages |
| `MoM/src/Combat.c` | 49 | Who is the "player side" in battle |
| `MoM/src/Spells129.c`, `Spells130.c`, `Spells132.c`, `OverSpel.c` | 38–48 each | Spell targeting/effects; mixed |
| `MoM/src/SBookScr.c`, `CMBMAGIC.c`, `MagicScr.c` | 26–35 each | Mostly UI |

There are also ~240 `== HUMAN_PLAYER_IDX` / `!= HUMAN_PLAYER_IDX` comparisons, which are the easy mechanical
cases. Beware loops like `_UNITS[i].owner_idx < HUMAN_PLAYER_IDX` (in `Delete_Dead_Units`, `NEXTTURN.c`), which
use the constant as a lower bound rather than as an identity.

A tool can do most of the classification: grep every site, record its function, and tag it rules / presentation /
unclear in a CSV. That makes it reviewable as data instead of one enormous diff.

### 2. State that exists once but has to exist per human

| State | Today | Needs |
|---|---|---|
| Fog of war, `_square_explored` (`MoM/src/ALLOC.c`, `NUM_PLANES * WORLD_SIZE` bytes) | One map, for the human | One per human player (2 × 2400 bytes each, which is cheap) |
| `square_scouted_p0` / `p1` bitfields | One set | One set per human |
| Turn messages (`MSG_Building_Complete`, `g_bldg_msg_ctr`, event notices, Chancellor report) | Queued for "the" human | A queue per human, shown when that human's turn begins |
| Map camera (`_map_x`, `_map_y`, `_map_plane`) and the selected stack | Globals | Saved and restored per human on hand-off |
| Cast-in-progress (`Cast_Spell_Overland(_current_player_idx)` at end of `Next_Turn_Proc`) | Happens right away | Deferred until that human's turn |
| `grand_vizier` and other settings | One global | Per human |

AI players have no fog (they "see everything", as in the original). That should stay as it is.

### 3. The turn loop

`Next_Turn_Proc()` (`MoM/src/NEXTTURN.c:310`) runs end of turn and everyone's AI in one go, then goes straight back
to the human's main screen. For hotseat, it needs:

1. A **hand-off screen** ("Player 2, your turn"), so nobody sees someone else's map.
2. Turn processing split per player, or a "processed" flag per player, so a human's city growth and messages are
   computed once and shown to the right person.
3. A clear point in the loop where control goes to the next human rather than the AI.

### 4. Determinism (required for lockstep, useful for everything else)

The good news is that ReMoM already has most of what lockstep needs:

- `Random()` (`MoX/src/random.c`) is a single seeded 32-bit LFSR, verified against original-game vectors.
- `g_random_call_count` counts every draw, so desyncs are easy to spot: compare the counter and a state hash each
  turn.
- `--seed` / `--seed2` command-line seeding (`MoX/src/MOX2.h`).
- Input record/replay (`platform/replay/Replay.c`) and a headless build (`platform/headless`, `HeMoM`).
- The simulation uses almost no floating point (a handful of `float`/`double` uses across `MoM/` and `MoX/`, which
  still need auditing), so cross-platform results stay identical.

The bad news:

- **Drawing consumes the game RNG.** For example, `MoM/src/CITYSCAP.c:1379` picks cityscape house sprites with
  `Random(5)`. In lockstep, a player who opens a city screen advances their RNG while the other player doesn't,
  and they desync on the next random roll. The fix is two RNG streams: `Random()` for simulation, and
  `Random_Cosmetic()` for anything UI-only. There are about 651 `Random(` call sites to classify, but only the
  ones reachable from screens/drawing actually matter.
- **Deliberate out-of-bounds reads.** The static-pool allocator (`doc/@AI_Plans/PRD-Static-Pool-Allocator.md`)
  makes these deterministic within one build. Every peer must run the **same build**, so put a build hash in the
  handshake.
- **Lockstep must not send UI input.** Replaying mouse clicks only works on the same screen state. Lockstep needs a
  **command layer**: `CMD_MOVE_STACK(unit_idx, dst)`, `CMD_SET_PRODUCTION(city, item)`, `CMD_CAST(spell, target)`,
  and so on. UI code creates commands instead of mutating state directly, and every peer applies the same command
  list in the same order. This is the biggest architectural piece. It also pays off without networking: it gives
  undo, cleaner tests and AI/UI separation.

### 5. Hidden information (online modes only)

In lockstep, every peer holds the full game state, so a modified client can see through the fog. For a
friends-only game that is acceptable. If it isn't, you need the authoritative-server model and must filter which
units, cities and spells each client is sent. Decide how much you care about cheating before you pick the network
model.

---

## Data types and struct sizes: what actually limits player count

You assumed the data types would need big changes. For up to 5 humans, they don't. Beyond 5 wizards, these are the
real constraints:

| Limit | Where | Why it matters |
|---|---|---|
| `NUM_PLAYERS 6`, `NUM_NONNPC_PLAYERS 5` | `MoX/src/MOM_DEF.h` | Sizes ~118 arrays |
| `NEUTRAL_PLAYER_IDX 5` | `MoX/src/MOX_DEF.h` | 185 uses. Many loops use it as "past the last wizard". If player count grows, neutral moves, or it gets a fixed sentinel index |
| `struct s_WIZ_DIPL`, embedded in `struct s_WIZARD` | `MoX/src/MOM_DAT.h:1367` | ~35 `[NUM_PLAYERS]` arrays inside each wizard, so `s_WIZARD` grows with N² and stops being 1224 bytes |
| Save format | `MoX/src/LOADSAVE.c` | Writes raw arrays with literal sizes: `_players, NUM_PLAYERS, 1224`, `_UNITS, NUM_UNITS, 32`, `_CITIES, NUM_CITIES, 114`, and per-player `_HEROES2[0..5]` lines written out one by one |
| `s_CITY.contacts` is a `uint8_t` bitfield | `MoX/src/MOM_DAT.h:1755` | Hard cap of 8 players |
| City enchantments store `owner + 1` per byte | `s_CITY.enchantments[]` | Fine to 127 players |
| `owner_idx` is `int8_t` everywhere | `s_UNIT`, `s_CITY`, `s_NODE`, etc. | Fine to 127 players. Note `owner_idx = 100` is a magic "recalled during combat" value in `s_UNIT` |
| 5 banner colours | `COL_Banners` / `banner_id`, sprite colour remaps | **An art limit, not a code limit.** A 6th wizard needs new remap palettes |
| Per-player hero tables `_HEROES2[NUM_PLAYERS]` | `MoX/src/LOADSAVE.c` | Fixed count |
| 6 fortresses, 6 towers, starting-position logic in map generation | `MAPGEN.c` | Map generation assumes ≤5 wizards on a 60×40 world |

Conclusion: **keep 5 wizards for multiplayer v1**. Going past 6 players is a separate "expand the game" project.
That project is where the dynamic-cap work below pays off.

---

## Removing the static caps

You asked whether there is a clever way to get rid of the fixed limits on players, units, cities and so on. There
is, and it fits ReMoM well, because **the code refers to everything by integer index, not by pointer**
(`_UNITS[unit_idx]`, `_CITIES[city_idx]`, `_players[player_idx]`). That means:

### Technique 1: keep the index, make the array growable

`_UNITS`, `_CITIES`, `_LAIRS`, `_NODES`, `_TOWERS` and `_FORTRESSES` are already global **pointers** carved out of an
arena (`MoM/src/ALLOC.c`), not fixed arrays. Change them to growable buffers (capacity doubles and the buffer is
reallocated when full). Every `_UNITS[i]` stays as it is. The only rule is that **nobody may hold a
`struct s_UNIT *` across a call that could create a unit.** Audit for cached pointers such as
`ptr = &_UNITS[i]; Create_Unit(...); ptr->...`.

Replace each compile-time cap used as a limit (`MAX_UNIT_COUNT`, `NUM_CITIES`, ...) with a runtime value:

```c
extern int16_t g_unit_capacity;   /* classic mode: fixed at 1009 so behaviour is unchanged */
```

The classic build keeps the original numbers, so the "1000 unit" limit and the bugs at its edges reproduce exactly.

### Technique 2: replace "index ranges" with tagged IDs

The map/click code packs different entity types into one integer by range (`MoX/src/MOX_DEF.h:709-712`): units are
`0..999`, cities are `1000 + city_idx`, and nodes come after `MAX_WHAT_COUNT`. Raising the unit cap would make
units collide with cities. Change it to a tagged ID, `(type << 12) | idx` or a small struct, so each type can grow on
its own.

### Technique 3: N×N player data goes into its own matrix

Per-pair data (`s_WIZ_DIPL`'s `[NUM_PLAYERS]` arrays, `Hostility[]`, `peace_duration[]`) moves out of `s_WIZARD`
into a separately allocated `relations[n_players * n_players]` table, accessed through a macro:

```c
#define DIPL(a, b)  (_diplomacy[(a) * _num_players + (b)])
```

`s_WIZARD` then has a fixed size again, no matter how many players there are. Do the same with player bitfields:
use a `bitset` type sized from `_num_players` in place of the `uint8_t contacts` field.

### Technique 4: neutral as a fixed sentinel, not "the last slot"

Give neutral/monster a fixed index that doesn't depend on N (for example `NEUTRAL_PLAYER_IDX = 0x7F` mapped to a
dedicated slot, or keep it as "last" but compute it as `_num_players`). Then rewrite the loops that use
`NEUTRAL_PLAYER_IDX` as an upper bound to use `_num_players`.

### Technique 5: a versioned save format, with the original kept as import/export

`LOADSAVE.c` writes raw memory with literal record sizes. Add a second, chunked save format (an IFF/RIFF-style
`tag, length, payload` per section, with a header holding `n_players`, `n_units`, `n_cities` and a format version).
Keep the original `SAVE*.GAM` reader and writer as they are, for games that fit the classic limits. A chunked format
also makes it simple to send state over the network later.

### Watch-outs

- `Delete_Dead_Units()` **compacts** the unit array, which shifts indices. That is fine in lockstep (every peer
  compacts the same way) but bad for anything that stores a unit index across turns, such as network commands,
  UI selection or AI target lists. Either resolve commands before compaction, or add stable unit IDs (a
  generational handle: index plus a reuse counter).
- Some caps exist for balance or performance, not just memory. The AI loops over units and stacks, sometimes
  nested (`MAX_AI_STACKS 80`, `MAX_AI_TARGETS 25`). Without caps, a 5000-unit late game could make AI turns slow.
- The map size (`WORLD_WIDTH 60`, `WORLD_HEIGHT 40`) is baked into the 4800-byte terrain records and the movement
  cost maps. It is a cap too, but a separate project.
- Each cap you lift is a place where the multiplayer build stops being faithful. Keep the classic values as the
  default in the classic build.

---

## Suggested phases

| Phase | Work | Proves it works |
|---|---|---|
| **0** | Decide fidelity policy (Decision 0) and mode (Decision 1). Add `REMOM_MULTIPLAYER` build option | Classic build and tests unchanged |
| **1** | Add `Player_Is_Human()` and `_local_player_idx`. Classify all `HUMAN_PLAYER_IDX` sites (tooling + CSV) and convert them | Classic mode: all existing tests pass, `g_random_call_count` identical |
| **2** | Per-human fog of war, message queues, camera/selection save and restore | Two-human hotseat game where each player sees only their own map |
| **3** | Hotseat turn loop and hand-off screen. Human-versus-human battles auto-resolve | Play a full hotseat game. A `HeMoM` `.hms` scenario drives two humans headless |
| **4** | Split cosmetic RNG from simulation RNG. Add a per-turn state hash | Same seed + same commands → identical hash, even after opening different screens |
| **5** | Command layer (orders as data). UI creates commands | Replay a game from a command log alone |
| **6** | Networking: lockstep over TCP (or the server model), lobby, build-hash handshake, desync detector | Two machines finish a game. Desyncs are reported with the turn and RNG count |
| **7** (optional, separate) | Dynamic caps: growable arrays, tagged entity IDs, N×N diplomacy matrix, chunked saves, more than 5 wizards with new banner art | 8-player game, 3000+ units, save/load round-trips |

Phases 1–3 deliver a playable feature (hotseat) on their own. Phase 4 can run in parallel with 2–3, because it
also helps the existing replay/determinism tests.

---

## Open questions

1. Which fidelity option (A/B/C)?
2. Hotseat first, or straight to online?
3. Sequential or simultaneous turns?
4. Interactive or auto-resolved human-versus-human battles?
5. Do you care about cheating (lockstep versus server)?
6. Is more than 5 wizards a goal, or is "5 slots, any mix of humans and AI" enough?
