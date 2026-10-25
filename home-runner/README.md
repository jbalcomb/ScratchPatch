# Home Runner

A small HTTP service on the workstation that runs a fixed list of jobs on request, so the
TaskDeck app can start them from anywhere on your tailnet. Its first jobs are Discord Capture's.

Pure Python 3.11+ standard library: no dependencies to install.

```
TaskDeck (phone) ──HTTPS over Tailscale──▶ tailscale serve ──▶ Home Runner (127.0.0.1:8765) ──▶ python -m discord_capture ...
```

## Install

```powershell
# Windows: a scheduled task that starts at logon and is restarted within 5 minutes if it dies
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```
```bash
# Linux: a systemd user service (Restart=always)
bash scripts/install-linux.sh
```

The script creates the access key, starts the runner, checks that it answers, and runs
`tailscale serve` to publish it on your tailnet at `https://<machine>.<tailnet>.ts.net:8765`, with
a real certificate. It then prints the **Runner URL** and **Runner key** to enter in TaskDeck
(*Home Jobs*, then the tune icon). The first time, Tailscale may ask you to enable HTTPS for the
tailnet; follow its link and rerun. The uninstall scripts remove the service and the
`tailscale serve` entry.

## Safety

* **No arbitrary commands.** A request names a job from [`jobs.toml`](jobs.toml) and supplies
  its parameters. Each parameter must fully match that job's regex, and the command runs without a
  shell. Unknown jobs, unknown parameters and bad values are refused with a 400.
* **Only on this machine, plus your tailnet.** The runner listens on 127.0.0.1. `tailscale serve`
  makes it reachable from your tailnet devices only. Nothing is exposed to the internet.
* **A key as well.** Every endpoint except `/health` needs `Authorization: Bearer <key>`. The
  key lives in `~/.config/home-runner/key`, readable only by you. A wrong key gets a 401 after a
  one-second delay.
* **One job at a time.** Queued runs go in order, so taps from the phone can't stack up into
  parallel Discord captures. Quick jobs (short, read-only lookups) run right away.

## API

| | |
|---|---|
| `GET /health` | `{"ok": true}`, needs no key |
| `GET /jobs` | the jobs and their parameters |
| `GET /runs?limit=20` | recent runs, newest first; `&all=1` includes quick lookups |
| `POST /runs` `{"job": "...", "params": {...}}` | 202 with the queued run, or 200 with a quick job's output (`result` if it printed JSON) |
| `GET /runs/<id>?tail=40` | one run: status, timing, exit code, the end of its log, and how many runs are `ahead` of it |
| `POST /runs/<id>/cancel` | cancel a queued run, or stop a running one |

A run's status is `queued`, `running`, `succeeded`, `failed`, `cancelled`, or `interrupted`
(the runner stopped while the run was active). History is in `data/runner.sqlite3`, and each
run's output is in `data/runs/<id>.log`.

## Adding a job

Add a `[jobs.<name>]` table to `jobs.toml` (the format is described at the top of the file) and
restart the runner. To use it from the phone, add a TaskDeck job that calls it. See
`taskdeck/src/taskdeck/jobs/home_runner/`.

## Run it by hand

```bash
cd src
python -m home_runner key                 # show the key (creates it the first time)
python -m home_runner serve               # 127.0.0.1:8765; --port, --jobs, --data, --log-file
```

Tests: `python -m unittest discover -s tests` with `PYTHONPATH=src`.

## Later

* **Schedules in the runner.** It's always on now, so it could run the hourly Discord sync itself,
  and the phone could see and change the schedule. Then the Discord Capture task and cron scripts
  could go.
* **Push notifications** when a long run finishes, instead of checking Home Jobs.
