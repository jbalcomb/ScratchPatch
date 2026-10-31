# IDA bridge

IDA Pro 5.5 (32-bit `idaw.exe`) on a Windows machine, exposed as a **REST API**, an **MCP server** and a small
**CLI** (`idactl`), for you and your agents on your Tailscale network. It runs as a Windows service under NSSM.

Each IDA operation is its own endpoint/tool with typed parameters (`list_functions file=… filter=…`), never
a raw command line. Behind each one is an IDC script written for IDA 5.5.

```
 coder workstation ─┐                         Windows machine
 AI workstation ────┼── tailnet (HTTPS) ──▶  tailscale serve ─▶ 127.0.0.1:8765  idabridge (NSSM service)
 Pixel 6 ───────────┘                                               │  REST  /ops/<op>, /jobs/…
                                                                    │  MCP   /mcp/
                                                                    └─▶ idaw.exe -A -S<job.idc> (≤ 8 at once)
```

## Operations

| Kind | Operations |
|---|---|
| Read (run on a copy of the database, in parallel) | `info`, `list_segments`, `list_functions`, `list_entry_points`, `list_names` (`segment=.idata` = PE imports), `list_strings`, `disassemble`, `function_info` (callers/callees), `xrefs_to`, `xrefs_from`, `read_bytes`, `find_bytes` |
| Files | `export` with `kind` = asm, lst, map, idc, dif or exe and your `output_name`. The file comes back with the result. |
| Change the database (one at a time per file, saved; rolled back if IDA fails) | `rename`, `set_comment`, `set_function_comment`, `make_code`, `make_function`, `make_data`, `undefine`, `set_type`, `reanalyze` |
| Database | `analyze` (happens automatically the first time a file is used; `force=true` rebuilds and **drops your renames**) |
| Anything else | `run_idc`: your own IDC code. Off unless `IDABRIDGE_ALLOW_IDC=1`. |

`file` is always a path relative to `IDA_INPUT_DIR`. Addresses are `0x401000`, `401000h`, decimal, or a name IDA
knows (`WinMain`, `sub_401000`). List operations are paged (`offset`, `limit`) and report `total`.
`GET /ops` (or `idactl ops`) lists every operation with its parameter schema; `/docs` is the interactive API.

## Answers and errors

Every request becomes a **job**. The request waits up to `wait` seconds (default 50), then answers:

| HTTP | state | Meaning |
|---|---|---|
| 200 | `succeeded` | `result.info`, `result.rows`, `result.total`; `files` for exports |
| 202 | `queued` / `running` | Still going. `live` shows elapsed time, the IDA process's CPU and memory, and the tail of IDA's log. Poll `GET /jobs/{id}?wait=50`. |
| 502 | `failed` | `error.kind` says which: `ida_error` (IDA refused, with its message), `script_not_run` (IDA exited before the script ran: load failure, IDC compile error, licence prompt), `script_aborted` (crashed part-way; partial rows kept), `database_not_created`, `output_missing`. Also: `exit_code` and its meaning (e.g. `0xC0000005` access violation), `ida_log_tail` and the exact command. |
| 504 | `timed_out` | `error.diagnostics` is what Windows said just before IDA was killed: alive or not, **busy** (CPU/disk moving) or **idle**, CPU seconds, memory, I/O, child processes, and any **windows IDA had open** (an idle IDA with a window is almost always stuck on a dialog). Then the process tree is killed. |
| 502 | `cancelled` / `interrupted` | `POST /jobs/{id}/cancel`; or the service restarted mid-job (says whether the process was still alive). |
| 4xx | | Refused before IDA ran: 401 token, 403 disabled operation, 404 file not found, 422 bad parameter. |

`GET /jobs/{id}/diagnose` takes the same OS snapshot of a running job on demand.

Files: `GET /jobs/{id}/files/{name}` downloads one. `GET /jobs/{id}/text/{name}?offset_line=&max_lines=` pages
through a big `.asm` in pieces an agent can read. `POST /ops/export?respond=file` answers with the file itself.

## Set up on the Windows machine

1. Install Python 3.12 (python.org, with the `py` launcher), [NSSM](https://nssm.cc) (`winget install NSSM.NSSM`)
   and Tailscale.
2. Copy this folder to the machine. From an **elevated** PowerShell in it:
   ```powershell
   .\scripts\install-service.ps1 -IdaDir "C:\IDA 5.5" -InputDir "D:\re\binaries"
   ```
   It creates `.venv`, makes a token, installs the service as the account you name (see below), and runs
   `tailscale serve --bg 8765`. The service then answers at `https://<machine>.<tailnet>.ts.net`, on the
   tailnet only. The server itself only listens on 127.0.0.1.
3. From any tailnet device:
   ```bash
   pip install ./idabridge        # or copy idabridge/cli.py: it is standard library only (works in Termux)
   export IDABRIDGE_URL=https://<machine>.<tailnet>.ts.net IDABRIDGE_TOKEN=<token>
   idactl selftest game/SOME32BIT.EXE
   ```

Settings are environment variables (the install script sets them in NSSM): `IDA_DIR`, `IDA_INPUT_DIR`,
`IDABRIDGE_HOME` (default `%ProgramData%\idabridge`), `IDABRIDGE_TOKEN`, `IDABRIDGE_HOST`/`PORT`,
`IDABRIDGE_MAX_PARALLEL` (8), `IDABRIDGE_TIMEOUT` (900 s per IDA run), `IDABRIDGE_WAIT` (50 s),
`IDABRIDGE_RETENTION_HOURS` (72; old job folders are deleted), `IDABRIDGE_ALLOW_IDC`, `IDABRIDGE_LOG_SWITCH`.

## Use it from agents

**Your pi harness.** If it's the pi coding agent, as far as I know it has no MCP support built in. It gives the
model a bash tool, so `idactl` is the natural fit. Put something like this in its system prompt or `AGENTS.md`:

> IDA Pro 5.5 is available through `idactl` (IDABRIDGE_URL/TOKEN are set). `idactl ops` lists operations;
> `idactl run <op> key=value …` prints rows as TSV; exports are saved to `-o <folder>`. Exit code 0 = ok,
> 1 = IDA failed (reason on stderr), 2 = timed out.

The phone needs nothing extra: you drive the harness running on your workstation, and that is what calls the
bridge. If you run an agent on the phone itself (Termux), `idactl` works there too.

**MCP clients** (Claude Code, Claude Desktop through a bridge, anything that does Streamable HTTP):
```bash
claude mcp add --transport http ida https://<machine>.<tailnet>.ts.net/mcp/ --header "Authorization: Bearer <token>"
```
Every operation is a tool, plus `job_status`, `cancel_job`, `diagnose_job`, `list_jobs`, `read_job_file` and
`list_inputs`. Tools wait `wait_seconds` (default 50, under typical client timeouts), then return a `job_id`.
Cloud-hosted connectors (claude.ai or ChatGPT "custom connectors", including their phone apps) connect from
the vendor's servers, so they **cannot** reach a tailnet-only server.

## Check it on real IDA (to do before relying on it)

This was built and tested against a fake `idaw.exe` (`tests/fake_idaw.py`). The IDC scripts were written from
the IDA 5.5 IDC API, but **they have not yet been run on IDA 5.5**. Each operation is in its own file
(`idabridge/idc/ops/*.idc`), so a function 5.5 lacks breaks only that one operation. Work through this list:

1. **Headless as the service account.** Log in as that account, start IDA once by hand, accept the licence and
   any first-run prompts, and open a 32-bit sample. Then try `idaw.exe -A -Stest.idc -Llog.txt sample.exe`
   from a console. Common snags: Session 0 has no desktop, and a dialog nobody sees means a hang. The timeout
   diagnostics list the window titles, which tells you which dialog to turn off in `ida.cfg`.
2. **`-L` log switch.** If 5.5 refuses `-L`, set `IDABRIDGE_LOG_SWITCH=0` (errors then come from `ida.out` and
   the result file only).
3. **`idactl selftest <file>`.** It runs every read operation and prints ok/FAIL for each. Fix any failing IDC
   file (usually a renamed IDC function) and run it again.
4. **Database prompts.** "Database was not closed properly" or "unpacked files found" dialogs after a crash.
   The bridge removes stray `.id0/.id1/.nam/.til` files and restores from a backup, but watch for others
   (PDB loading, "file is packed"). Answer them once in `ida.cfg` / plugin cfg.
5. **Typical run time.** Look at `steps[].seconds` on your real files, then set `IDABRIDGE_TIMEOUT` and the wait.
6. **Parallel IDA.** Check that 8 `idaw.exe` at once is fine for your licence and RAM. Lower
   `IDABRIDGE_MAX_PARALLEL` if not.
7. **Saving.** Check that `rename` then `info` shows the new name. That means `SaveBase` works in batch mode.

## How it works

* `db/<name>-<sha256>/` holds a copy of the binary and its `.idb`, keyed by content, so a changed file gets a
  fresh database. Read jobs copy the `.idb` into their job folder and work there, so up to 8 run at once, even
  on one file. Write jobs lock the file's database, back it up, run, and save. If anything goes wrong they
  restore the backup, including after a service crash.
* Each job folder holds `job.json`, the generated `job.idc`, `result.tsv` (the script's output protocol is
  documented in `idc/common.idc`), `ida.log`, `ida.out` and any output files.
* On Windows each IDA process is placed in a Job Object that kills it and its children when the job is
  cancelled, times out, or the service stops. No orphan IDA keeps a database locked.
* User text reaches IDC only as escaped string literals. Paths are confined to `IDA_INPUT_DIR` and the job
  folder, and output names are plain file names.

## Develop

```bash
cd idabridge
pip install -e ".[test]"
pytest -q          # uses tests/fake_idaw.py in place of IDA; CI runs it on Linux and Windows
```
