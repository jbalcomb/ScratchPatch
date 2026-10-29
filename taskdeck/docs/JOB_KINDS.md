# Two kinds of copying jobs

Jobs that copy files somewhere (today: Extract Archive to Google Drive) come in two kinds with a different
basic nature. Which kind a job is decides what happens when a file with the same name is already at the
destination. Code: `taskdeck/core/copying.py` (`JobNature`, `plan()`); a job declares its kind with
`JobPlugin.nature`.

## One-time copy (`JobNature.ONE_TIME_COPY`)

Copy a **fixed snapshot**, once. Example: extract an archive and upload it. The archive's contents can't
change, so the destination should end up an **exact, complete copy** of it.

* A same-named file with the **same content** (size and MD5 checksum) is already done: skipped. That's what
  makes running it again after a cancel or crash cheap: only what's missing is sent.
* A same-named file with **different content can't be a newer version**: it's a broken leftover of an
  earlier, interrupted run. The file is uploaded **fresh** (a new file, with a new created time), and only then
  is the leftover moved to Drive's trash (recoverable for 30 days). Uploading first means there is always a
  copy, even if the run stops in between.
* Duplicates of a name (Drive allows several files with the same name in a folder) are leftovers too: one good
  copy stays, the rest go to the trash.
* Nothing about a leftover is worth keeping (its link, sharing, version history). Complete and correct is
  what matters, not history.

## Ongoing sync (`JobNature.ONGOING_SYNC`)

Keep copies of files that **keep changing**: the same source is copied again and again (no such job yet).
The destination files **live on**: people link to them, share them, and rely on their history.

* A same-named file with the same content is unchanged: skipped.
* A different one is an **older version of the same file**: it is **updated in place**. Same file ID, link,
  sharing and version history (Drive keeps earlier versions). Its created time stays (it's the same
  document); its modified time follows the changes.

## Adding a copying job

Set `nature` on the job and route every "is it already there?" decision through `copying.plan()`, so the two
kinds can't get mixed up again (Extract Archive once updated broken leftovers in place, the sync rule, which
is wrong for a snapshot).
