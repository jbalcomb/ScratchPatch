import threading

from idabridge import ops
from idabridge.runner import JobManager


def run(m, op, wait=30, **kw):
    job = m.submit(op, ops.OPS[op].params(**kw))
    return m.wait(job.id, wait)


def test_first_use_builds_the_database_then_reads_a_copy(manager):
    job = run(manager, "list_functions", file="game/MAGIC.EXE", filter="Draw")
    assert job.state == "succeeded", job.error
    assert [s.name for s in job.steps] == ["analyze", "list_functions"]
    assert job.result["rows"] == [{"start": "0x401010", "end": "0x401020", "size": "16",
                                   "name": "Draw\tthing", "flags": "0x0"}]
    assert job.result["total"] == 2
    again = run(manager, "info", file="game/MAGIC.EXE")
    assert [s.name for s in again.steps] == ["info"]


def test_analyze_reports_on_an_existing_database(manager):
    assert run(manager, "analyze", file="game/MAGIC.EXE").state == "succeeded"
    job = run(manager, "analyze", file="game/MAGIC.EXE")
    assert job.state == "succeeded" and [s.name for s in job.steps] == ["info"]


def test_reads_run_in_parallel(manager):
    run(manager, "analyze", file="game/MAGIC.EXE")
    jobs = [manager.submit("info", ops.OPS["info"].params(file="game/MAGIC.EXE")) for _ in range(4)]
    assert all(manager.wait(j.id, 30).state == "succeeded" for j in jobs)


def test_write_is_saved_and_failed_write_restores(manager, settings):
    assert run(manager, "rename", file="game/MAGIC.EXE", address="0x401000", new_name="Main").state == "succeeded"
    assert "renamed Main" in run(manager, "info", file="game/MAGIC.EXE").result["info"]["idb"]
    bad = run(manager, "rename", file="game/MAGIC.EXE", address="0x401000", new_name="fail")
    assert bad.state == "failed" and bad.error["kind"] == "ida_error"
    assert bad.error["message"].startswith("IDA refused")
    assert bad.error["database"].startswith("restored")
    assert not list(settings.db_dir.glob("*/*.bak"))


def test_export_returns_the_file(manager):
    job = run(manager, "export", file="game/MAGIC.EXE", kind="asm", output_name="magic.asm")
    assert job.state == "succeeded", job.error
    assert job.files[0]["name"] == "magic.asm"
    assert manager.job_file(job.id, "magic.asm").read_text().startswith("; fake asm")


def test_timeout_reports_what_the_os_saw_and_kills_ida(manager):
    run(manager, "analyze", file="game/MAGIC.EXE")
    # the database exists for MAGIC.EXE only; hang.exe hangs while it is being built
    job = run(manager, "info", wait=60, file="game/hang.exe", timeout=5)
    assert job.state == "timed_out"
    d = job.error["diagnostics"]
    assert d["alive"] is True and "verdict" in d and d["processes"]
    pid = job.steps[-1].pid
    import psutil
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_wait_returns_running_with_live_info(manager):
    job = manager.submit("info", ops.OPS["info"].params(file="game/slow.exe"))
    early = manager.wait(job.id, 0.5)
    assert early.state in ("queued", "running")
    assert manager.wait(job.id, 30).state == "succeeded"


def test_crash_noscript_and_missing_db(manager):
    crash = run(manager, "analyze", file="game/crash.exe")
    assert crash.state == "failed" and crash.error["kind"] == "script_aborted"
    assert crash.steps[-1].exit_code == 139
    assert run(manager, "info", file="game/noscript.exe").error["kind"] == "script_not_run"
    assert run(manager, "info", file="game/nodb.exe").error["kind"] == "database_not_created"


def test_cancel(manager):
    job = manager.submit("info", ops.OPS["info"].params(file="game/hang.exe"))
    for _ in range(100):
        if job.steps and job.steps[-1].pid:
            break
        threading.Event().wait(0.05)
    manager.cancel(job.id)
    assert manager.wait(job.id, 20).state == "cancelled"


def test_refusals(manager, settings):
    import pytest
    from idabridge.runner import JobError
    with pytest.raises(JobError) as e:
        manager.submit("info", ops.OPS["info"].params(file="game/none.exe"))
    assert e.value.status == 404
    with pytest.raises(JobError) as e:
        manager.submit("run_idc", ops.OPS["run_idc"].params(file="game/MAGIC.EXE", code="return 0;"))
    assert e.value.status == 403


def test_restart_marks_running_jobs_interrupted(settings):
    m = JobManager(settings)
    job = m.submit("info", ops.OPS["info"].params(file="game/hang.exe"))
    for _ in range(100):
        if job.steps and job.steps[-1].pid:
            break
        threading.Event().wait(0.05)
    m.pool.shutdown(wait=False)  # pretend the service died: the worker thread is abandoned
    job.state = "running"
    m._save(job)
    m2 = JobManager(settings)
    again = m2.get(job.id)
    assert again.state == "interrupted"
    assert "process_after_restart" in again.error
    m.cancel(job.id)
    m2.shutdown()
