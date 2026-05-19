// Read-only investigation script — queries SyncLog + SyncBoardResult to
// characterize recent sync failures. Loads env from the launchpad repo.
import { config as loadEnv } from "/Users/glennfiocca/launchpad/node_modules/dotenv"
loadEnv({ path: "/Users/glennfiocca/launchpad/.env" })

import { PrismaClient } from "/Users/glennfiocca/launchpad/node_modules/@prisma/client"

const db = new PrismaClient()

function fmtDur(ms: number | null): string {
  if (ms === null || ms === undefined) return "n/a"
  const s = Math.round(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h${m}m${sec}s`
  if (m > 0) return `${m}m${sec}s`
  return `${sec}s`
}

function trunc(s: string | null, n: number): string {
  if (!s) return ""
  if (s.length <= n) return s.replace(/\n/g, " | ")
  return s.slice(0, n).replace(/\n/g, " | ") + "..."
}

async function main() {
  const logs = await db.syncLog.findMany({
    orderBy: { startedAt: "desc" },
    take: 30,
    select: {
      id: true,
      triggeredBy: true,
      startedAt: true,
      completedAt: true,
      status: true,
      totalBoards: true,
      boardsSynced: true,
      boardsFailed: true,
      totalAdded: true,
      totalUpdated: true,
      durationMs: true,
      errorSummary: true,
    },
  })

  console.log("\n=== SyncLog: last 30 ===")
  console.log(
    [
      "started",
      "trig",
      "status",
      "dur",
      "synced/fail/total",
      "added",
      "updated",
      "error",
    ].join(" | "),
  )
  for (const r of logs) {
    console.log(
      [
        r.startedAt.toISOString(),
        r.triggeredBy.padEnd(10),
        r.status.padEnd(15),
        fmtDur(r.durationMs).padStart(8),
        `${r.boardsSynced}/${r.boardsFailed}/${r.totalBoards}`.padStart(15),
        String(r.totalAdded).padStart(5),
        String(r.totalUpdated).padStart(6),
        trunc(r.errorSummary, 120),
      ].join(" | "),
    )
  }

  console.log("\n=== Status distribution ===")
  const byStatus = await db.syncLog.groupBy({
    by: ["status"],
    _count: { _all: true },
    orderBy: { _count: { status: "desc" } as never },
  })
  for (const s of byStatus) {
    console.log(`${s.status}: ${s._count._all}`)
  }

  // Count failures in last 7d / 30d
  const now = Date.now()
  const last7d = await db.syncLog.count({
    where: {
      startedAt: { gte: new Date(now - 7 * 24 * 3600 * 1000) },
      status: "FAILURE",
    },
  })
  const last30d = await db.syncLog.count({
    where: {
      startedAt: { gte: new Date(now - 30 * 24 * 3600 * 1000) },
      status: "FAILURE",
    },
  })
  const partial7d = await db.syncLog.count({
    where: {
      startedAt: { gte: new Date(now - 7 * 24 * 3600 * 1000) },
      status: "PARTIAL_FAILURE",
    },
  })
  console.log(`\nFAILURE last 7d: ${last7d}`)
  console.log(`FAILURE last 30d: ${last30d}`)
  console.log(`PARTIAL_FAILURE last 7d: ${partial7d}`)

  // For the most recent FAILUREs, surface top failing boards
  console.log("\n=== Most recent 5 FAILURE/PARTIAL runs with top failing boards ===")
  const recentFails = await db.syncLog.findMany({
    where: { status: { in: ["FAILURE", "PARTIAL_FAILURE"] } },
    orderBy: { startedAt: "desc" },
    take: 5,
    select: { id: true, startedAt: true, status: true, errorSummary: true, durationMs: true },
  })

  for (const f of recentFails) {
    console.log(`\n--- ${f.startedAt.toISOString()} | ${f.status} | dur=${fmtDur(f.durationMs)} ---`)
    console.log(`SyncLog id: ${f.id}`)
    console.log(`errorSummary: ${trunc(f.errorSummary, 300)}`)
    const boardFails = await db.syncBoardResult.findMany({
      where: { syncLogId: f.id, status: "FAILURE" },
      orderBy: { durationMs: "desc" },
      take: 10,
      select: { boardName: true, boardToken: true, durationMs: true, errors: true },
    })
    console.log(`  failing boards: ${boardFails.length}`)
    for (const b of boardFails) {
      console.log(
        `  - ${b.boardName} (${b.boardToken}) dur=${fmtDur(b.durationMs)}: ${trunc(JSON.stringify(b.errors), 200)}`,
      )
    }
  }

  // Active board count
  const activeBoards = await db.companyBoard.count({
    where: { isActive: true, reviewStatus: { not: "REJECTED" } },
  })
  console.log(`\n=== Active boards (current): ${activeBoards} ===`)

  // Currently running sync (if any)
  const running = await db.syncLog.findFirst({
    where: { status: "RUNNING" },
    select: { id: true, startedAt: true, triggeredBy: true },
  })
  if (running) {
    const elapsedMs = Date.now() - running.startedAt.getTime()
    console.log(
      `\n=== RUNNING sync: id=${running.id} triggered=${running.triggeredBy} startedAt=${running.startedAt.toISOString()} elapsed=${fmtDur(elapsedMs)} ===`,
    )
    // Board progress so far for the running sync
    const progressCount = await db.syncBoardResult.count({
      where: { syncLogId: running.id },
    })
    const progressFail = await db.syncBoardResult.count({
      where: { syncLogId: running.id, status: "FAILURE" },
    })
    console.log(`Boards processed so far: ${progressCount} (failures: ${progressFail})`)
    // Top-N slowest boards in this run
    const slowest = await db.syncBoardResult.findMany({
      where: { syncLogId: running.id },
      orderBy: { durationMs: "desc" },
      take: 10,
      select: { boardName: true, boardToken: true, durationMs: true, status: true },
    })
    console.log(`Slowest 10 boards so far:`)
    for (const s of slowest) {
      console.log(`  - ${s.boardName} (${s.boardToken}) ${s.status} dur=${fmtDur(s.durationMs)}`)
    }
  } else {
    console.log("\nNo RUNNING sync at the moment.")
  }

  // Duration distribution of recently-completed runs
  console.log("\n=== Last 20 completed (non-RUNNING) durations ===")
  const completed = await db.syncLog.findMany({
    where: { status: { in: ["SUCCESS", "PARTIAL_FAILURE", "FAILURE"] } },
    orderBy: { startedAt: "desc" },
    take: 20,
    select: { startedAt: true, status: true, durationMs: true, totalBoards: true, boardsSynced: true },
  })
  for (const c of completed) {
    console.log(
      `${c.startedAt.toISOString()} | ${c.status.padEnd(16)} | ${fmtDur(c.durationMs).padStart(8)} | ${c.boardsSynced}/${c.totalBoards}`,
    )
  }
}

main()
  .catch((err) => {
    console.error("FATAL:", err)
    process.exit(1)
  })
  .finally(() => db.$disconnect())
