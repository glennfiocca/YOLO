// Polls the most recent SyncLog row and emits one line per check.
// Used as a Monitor source; exits when the latest run terminates.
//
// Usage: node sync_poll.mjs

import { PrismaClient } from "/Users/glennfiocca/launchpad/node_modules/@prisma/client/index.js"
import { readFileSync } from "node:fs"

const envText = readFileSync("/Users/glennfiocca/launchpad/.env", "utf8")
for (const line of envText.split("\n")) {
  const trimmed = line.trim()
  if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue
  const eq = trimmed.indexOf("=")
  const key = trimmed.slice(0, eq).trim()
  let val = trimmed.slice(eq + 1).trim()
  if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
    val = val.slice(1, -1)
  }
  if (!process.env[key]) process.env[key] = val
}

const db = new PrismaClient()

const POLL_INTERVAL_MS = 60_000
const TERMINAL = new Set(["SUCCESS", "FAILED"])

async function fetchLatest() {
  return db.syncLog.findFirst({ orderBy: { startedAt: "desc" } })
}

async function fetchActiveBoardsCount() {
  return db.companyBoard.count({
    where: { isActive: true, reviewStatus: { not: "REJECTED" } },
  })
}

async function fetchPublicJobsCount() {
  return db.job.count({ where: { isActive: true } })
}

let lastStatus = null
let lastBoardsSynced = -1

async function tick() {
  const log = await fetchLatest()
  if (!log) {
    console.log(`[${new Date().toISOString()}] no SyncLog row found yet`)
    return false
  }
  const now = new Date().toISOString()
  if (TERMINAL.has(log.status)) {
    const jobsActive = await fetchPublicJobsCount()
    console.log(
      `[${now}] DONE status=${log.status} ` +
      `boards=${log.boardsSynced}/${log.totalBoards} (failed=${log.boardsFailed}) ` +
      `added=${log.totalAdded} updated=${log.totalUpdated} deactivated=${log.totalDeactivated} ` +
      `durationMin=${(log.durationMs / 60000).toFixed(1)} ` +
      `totalActiveJobs=${jobsActive}`,
    )
    return true
  }
  if (log.status === "RUNNING") {
    // SyncLog counters are only written at finalization. Count finished boards
    // by querying SyncBoardResult — that's the live-progress source of truth.
    const [done, failed, target] = await Promise.all([
      db.syncBoardResult.count({ where: { syncLogId: log.id } }),
      db.syncBoardResult.count({ where: { syncLogId: log.id, status: "FAILURE" } }),
      fetchActiveBoardsCount(),
    ])
    if (done !== lastBoardsSynced) {
      const elapsedMin = (Date.now() - new Date(log.startedAt).getTime()) / 60000
      const rate = done / Math.max(elapsedMin, 0.1)
      const remain = (target - done) / Math.max(rate, 0.01)
      console.log(
        `[${now}] running boards=${done}/${target} ` +
        `(failed=${failed}) ` +
        `elapsedMin=${elapsedMin.toFixed(1)} rate=${rate.toFixed(2)}/min etaMin=${remain.toFixed(0)}`,
      )
      lastBoardsSynced = done
    }
  }
  return false
}

async function main() {
  while (true) {
    try {
      const done = await tick()
      if (done) break
    } catch (err) {
      // Survive transient DB blips (sync workers can saturate the pool briefly).
      // Emit a single short line and keep polling.
      const code = err?.errorCode ?? err?.code ?? "ERR"
      console.log(`[${new Date().toISOString()}] poll skipped (${code}); retry in ${POLL_INTERVAL_MS/1000}s`)
    }
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
  }
  await db.$disconnect().catch(() => undefined)
}

main().catch((err) => {
  console.error("[poll-fatal]", err?.message ?? err)
  process.exit(1)
})
