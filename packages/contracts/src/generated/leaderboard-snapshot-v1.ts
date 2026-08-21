/* Generated from JSON Schema. Do not edit. */

export type LeaderboardSnapshotId = string
export type LeaderboardSnapshotDigest = string

export interface HarborHFLeaderboardSnapshotV1 {
schema_version: "v1"
kind: "leaderboard.snapshot"
record_id: LeaderboardSnapshotId
created_at: string
actor: LeaderboardSnapshotActor
sqlite_key: string
sqlite_digest: LeaderboardSnapshotDigest
source_digest: LeaderboardSnapshotDigest
entry_count: number
}
export interface LeaderboardSnapshotActor {
subject: string
role: ("operator" | "reader" | "service" | "migration")
}
