"""Subtitle localization quality-control and delivery service."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"

# Broadcast frame rates are rational: 23.976 fps is exactly 24000/1001.
# (num, den, nominal_timecode_fps)
KNOWN_FPS: dict[str, tuple[int, int, int]] = {
    "23.976": (24000, 1001, 24),
    "23.98": (24000, 1001, 24),
    "24": (24, 1, 24),
    "25": (25, 1, 25),
    "29.97": (30000, 1001, 30),
    "30": (30, 1, 30),
    "59.94": (60000, 1001, 60),
    "60": (60, 1, 60),
}


def parse_fps(value: Any) -> tuple[int, int, int]:
    """Return (fps_num, fps_den, nominal_tc_fps) for common broadcast rates."""
    text = str(value).strip()
    if not text:
        raise DomainError("帧率不能为空")
    if text in KNOWN_FPS:
        return KNOWN_FPS[text]
    try:
        fps = float(text)
    except ValueError as exc:
        raise DomainError(f"不支持的帧率: {text}") from exc
    if not math.isfinite(fps) or fps <= 0 or fps > 240:
        raise DomainError(f"不支持的帧率: {text}")
    if abs(fps - round(fps)) < 1e-9:
        rate = int(round(fps))
        return (rate, 1, rate)
    # NTSC-style rates run 0.1% slow: nominal n is encoded as (1000n)/1001.
    nominal = int(round(fps * 1001 / 1000))
    candidate = nominal * 1000
    if abs(candidate / 1001 - fps) < 1e-6:
        return (candidate, 1001, nominal)
    raise DomainError(f"不支持的帧率: {text}（可用 24/25/23.976 等常用帧率）")


def frame_at(ms: int, fps: tuple[int, int, int]) -> int:
    num, den, _ = fps
    return int(ms * num / (1000 * den) + 0.5)


def frame_to_ms(frame: int, fps: tuple[int, int, int]) -> float:
    num, den, _ = fps
    return round(frame * 1000 * den / num, 3)


def format_timecode(frame: int, nominal_fps: int) -> str:
    total, ff = divmod(int(frame), nominal_fps)
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def convert_timeline(
    cues: list[dict[str, Any]],
    source_fps: tuple[int, int, int],
    target_fps: tuple[int, int, int],
    duration_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Frame-preserving respeed (24 -> 25 PAL, 24 -> 23.976 NTSC).

    Returns converted cue rows and validation issues. The media only contains
    whole frames, so its last valid frame is floor(duration * fps).
    """
    total_frames = int(duration_ms * source_fps[0] / (1000 * source_fps[1]))
    rows: list[dict[str, Any]] = []
    for cue in cues:
        osf = frame_at(int(cue["start_ms"]), source_fps)
        oef = frame_at(int(cue["end_ms"]), source_fps)
        # Frame count is preserved; wall-clock position shifts with the rate.
        nsf, nef = osf, oef
        rows.append({
            "cue_index": int(cue["cue_index"]),
            "orig_start_ms": int(cue["start_ms"]),
            "orig_end_ms": int(cue["end_ms"]),
            "orig_start_frame": osf,
            "orig_end_frame": oef,
            "new_start_frame": nsf,
            "new_end_frame": nef,
            "new_start_ms": frame_to_ms(nsf, target_fps),
            "new_end_ms": frame_to_ms(nef, target_fps),
            "text": cue["text"],
        })

    issues: list[dict[str, Any]] = []
    for row in rows:
        idx = row["cue_index"]
        if row["new_end_frame"] <= row["new_start_frame"]:
            issues.append({"cue_index": idx, "related_index": None, "kind": "empty_duration",
                           "message": f"第 {idx} 句换算后起止都落在第 {row['new_start_frame']} 帧，空时长"})
        if row["new_start_frame"] >= total_frames or row["new_end_frame"] > total_frames:
            issues.append({"cue_index": idx, "related_index": None, "kind": "out_of_bounds",
                           "message": f"第 {idx} 句换算后结束帧 {row['new_end_frame']} 越过成片末帧 {total_frames}，越界"})
    ordered = sorted(rows, key=lambda r: (r["new_start_frame"], r["cue_index"]))
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt["new_start_frame"] < prev["new_end_frame"]:
            a, b = prev["cue_index"], nxt["cue_index"]
            issues.append({"cue_index": a, "related_index": b, "kind": "overlap",
                           "message": f"第 {a} 句与第 {b} 句换算后时间轴重叠"})
            issues.append({"cue_index": b, "related_index": a, "kind": "overlap",
                           "message": f"第 {b} 句与第 {a} 句换算后时间轴重叠"})
    issues.sort(key=lambda i: (i["cue_index"], i["kind"]))
    return rows, issues


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    user TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
                    assigned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,user,role)
                );
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
                    user TEXT NOT NULL,
                    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS glossaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,source_term)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
                    supersedes_version_id INTEGER REFERENCES versions(id),
                    snapshot_hash TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    delivered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_specs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    platform_name TEXT NOT NULL,
                    source_fps TEXT NOT NULL,
                    target_fps TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','blocked','delivered')),
                    snapshot_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_by TEXT,
                    delivered_at TEXT,
                    UNIQUE(version_id,platform_name,target_fps)
                );
                CREATE TABLE IF NOT EXISTS platform_spec_cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spec_id INTEGER NOT NULL REFERENCES platform_specs(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    orig_start_ms INTEGER NOT NULL,
                    orig_end_ms INTEGER NOT NULL,
                    orig_start_frame INTEGER NOT NULL,
                    orig_end_frame INTEGER NOT NULL,
                    new_start_frame INTEGER NOT NULL,
                    new_end_frame INTEGER NOT NULL,
                    new_start_ms REAL NOT NULL,
                    new_end_ms REAL NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_spec_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spec_id INTEGER NOT NULL REFERENCES platform_specs(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    related_index INTEGER,
                    kind TEXT NOT NULL CHECK(kind IN ('overlap','out_of_bounds','empty_duration')),
                    message TEXT NOT NULL
                );
                """
            )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            source_term = str(payload.get("source_term", "")).strip()
            required = str(payload.get("required_translation", "")).strip()
            forbidden = payload.get("forbidden_terms", [])
            if not source_term or not required or not isinstance(forbidden, list):
                raise DomainError("术语、指定译法和禁用词格式不合法")
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), str(payload.get("notes", "")), utcnow()),
            )
            self._audit(conn, actor, "glossary.saved", "project", project_id, {"source_term": source_term})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required, "forbidden_terms": forbidden}

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            if parent_id is not None:
                parent = conn.execute("SELECT * FROM versions WHERE id=? AND project_id=?", (int(parent_id), project_id)).fetchone()
                if not parent or parent["language"] != language:
                    raise DomainError("父版本不存在或目标语言不一致", 409)
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?", (project_id, language)).fetchone()["value"])
            cur = conn.execute(
                "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, language, next_no, parent_id, actor, utcnow(), utcnow()),
            )
            self._audit(conn, actor, "version.created", "version", cur.lastrowid, {"language": language, "version_no": next_no})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.connect() as conn:
            version = conn.execute("SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)", (version_id, user, assignment_role, actor, utcnow()))
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')", (version["id"], actor)).fetchone())

    def _validate_glossary(self, conn: sqlite3.Connection, project_id: int, text: str) -> None:
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            forbidden = json.loads(row["forbidden_terms"])
            for term in forbidden:
                if term and term in text:
                    raise DomainError(f"字幕包含禁用译法: {term}")
            # The glossary is enforced only when the corresponding source term
            # appears in the localized cue. This keeps it useful without making
            # every cue repeat every glossary word.
            if row["source_term"] in text and row["required_translation"] not in text:
                raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")

    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            self._validate_glossary(conn, int(version["project_id"]), text)
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?", (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id, {"cue_id": saved_id, "revision": revision})
        return dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()) | {"version_revision": revision}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)", (version_id, cue_id, actor, time_ms, body, utcnow()))
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.submitted", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            assigned = conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'", (version_id, actor)).fetchone()
            if not assigned and actor != version["owner"]:
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)", (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.locked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有批准或锁定版本可以交付", 409)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            manifest = {"project_id": version["project_id"], "version_id": version_id, "language": version["language"], "version_no": version["version_no"], "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = conn.execute("SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1", (version["project_id"], version["language"], version_id)).fetchone()
            if previous:
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)", (utcnow(), previous["id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash, json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id, {"snapshot_hash": snapshot_hash})
        return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_spec(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        platform = str(payload.get("platform_name", "")).strip()
        if not platform:
            raise DomainError("平台名称不能为空")
        source_fps = parse_fps(payload.get("source_fps", 24))
        target_fps = parse_fps(payload.get("target_fps"))
        if source_fps == target_fps:
            raise DomainError("目标帧率与源帧率相同，无需建立交付规格")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以建立平台交付规格", 403)
            cues = [dict(r) for r in conn.execute(
                "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            if not cues:
                raise DomainError("版本还没有字幕，不能生成平台规格", 409)
            rows, issues = convert_timeline(cues, source_fps, target_fps, int(version["duration_ms"]))
            src_label = str(payload.get("source_fps", 24)).strip()
            tgt_label = str(payload.get("target_fps")).strip()
            snapshot = {
                "platform_name": platform,
                "source_fps": src_label,
                "target_fps": tgt_label,
                "source_revision": int(version["revision"]),
                "cues": rows,
                "issues": issues,
            }
            snapshot_hash = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            status = "blocked" if issues else "ready"
            try:
                cur = conn.execute(
                    """INSERT INTO platform_specs(version_id,platform_name,source_fps,target_fps,source_revision,
                       status,snapshot_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (version_id, platform, src_label, tgt_label, int(version["revision"]),
                     status, snapshot_hash, actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该平台规格已存在（同版本、平台与目标帧率唯一）", 409) from exc
            spec_id = int(cur.lastrowid)
            conn.executemany(
                """INSERT INTO platform_spec_cues(spec_id,cue_index,orig_start_ms,orig_end_ms,orig_start_frame,
                   orig_end_frame,new_start_frame,new_end_frame,new_start_ms,new_end_ms,text)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(spec_id, r["cue_index"], r["orig_start_ms"], r["orig_end_ms"], r["orig_start_frame"],
                  r["orig_end_frame"], r["new_start_frame"], r["new_end_frame"], r["new_start_ms"],
                  r["new_end_ms"], r["text"]) for r in rows],
            )
            conn.executemany(
                "INSERT INTO platform_spec_issues(spec_id,cue_index,related_index,kind,message) VALUES(?,?,?,?,?)",
                [(spec_id, i["cue_index"], i["related_index"], i["kind"], i["message"]) for i in issues],
            )
            self._audit(conn, actor, "platformspec.created", "platform_spec", spec_id,
                        {"platform": platform, "target_fps": tgt_label, "status": status, "issues": len(issues)})
        return self.get_spec(spec_id)

    def get_spec(self, spec_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            spec = conn.execute("SELECT * FROM platform_specs WHERE id=?", (spec_id,)).fetchone()
            if not spec:
                raise DomainError("平台交付规格不存在", 404)
            cues_raw = [dict(r) for r in conn.execute(
                "SELECT * FROM platform_spec_cues WHERE spec_id=? ORDER BY cue_index", (spec_id,))]
            issues = [dict(r) for r in conn.execute(
                "SELECT cue_index,related_index,kind,message FROM platform_spec_issues WHERE spec_id=? ORDER BY id", (spec_id,))]
        target_nominal = parse_fps(spec["target_fps"])[2]
        source_nominal = parse_fps(spec["source_fps"])[2]
        cues = []
        for row in cues_raw:
            row["orig_timecode"] = format_timecode(row["orig_start_frame"], source_nominal) + " --> " + format_timecode(row["orig_end_frame"], source_nominal)
            row["new_timecode"] = format_timecode(row["new_start_frame"], target_nominal) + " --> " + format_timecode(row["new_end_frame"], target_nominal)
            row["text"] = row["text"]
            cues.append(row)
        return dict(spec) | {"cues": cues, "issues": issues,
                             "issue_count": len(issues), "blocked": bool(issues)}

    def list_specs(self, version_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if version_id:
                rows = conn.execute(
                    """SELECT s.*,COUNT(i.id) issue_count FROM platform_specs s
                       LEFT JOIN platform_spec_issues i ON i.spec_id=s.id
                       WHERE s.version_id=? GROUP BY s.id ORDER BY s.id DESC""", (version_id,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT s.*,COUNT(i.id) issue_count FROM platform_specs s
                       LEFT JOIN platform_spec_issues i ON i.spec_id=s.id
                       GROUP BY s.id ORDER BY s.id DESC""").fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["blocked"] = d["status"] == "blocked"
                result.append(d)
            return result

    def deliver_spec(self, spec_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            spec = conn.execute(
                """SELECT s.*,v.status AS version_status,v.project_id,p.owner,p.duration_ms
                   FROM platform_specs s JOIN versions v ON v.id=s.version_id
                   JOIN projects p ON p.id=v.project_id WHERE s.id=?""", (spec_id,)).fetchone()
            if not spec:
                raise DomainError("平台交付规格不存在", 404)
            if actor != spec["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付平台规格", 403)
            if spec["status"] == "blocked":
                count = int(conn.execute("SELECT COUNT(*) c FROM platform_spec_issues WHERE spec_id=?", (spec_id,)).fetchone()["c"])
                raise DomainError(f"规格存在 {count} 个问题句（重叠/越界/空时长），已阻止交付，请修正时间轴后重新生成", 409)
            if spec["status"] == "delivered":
                raise DomainError("该平台规格已经交付，不能重复交付", 409)
            if spec["version_status"] not in {"approved", "locked", "delivered"}:
                raise DomainError("关联版本尚未通过复核，不能交付平台规格", 409)
            conn.execute("UPDATE platform_specs SET status='delivered',delivered_by=?,delivered_at=? WHERE id=?",
                         (actor, utcnow(), spec_id))
            self._audit(conn, actor, "platformspec.delivered", "platform_spec", spec_id,
                        {"platform": spec["platform_name"], "snapshot_hash": spec["snapshot_hash"]})
        return self.get_spec(spec_id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_projects():
        return {"project": int(db.list_projects()[0]["id"])}
    project = db.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4", "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    db.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    version = db.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "SubtitleQC/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.db.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.db.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.db.list_deliveries()})
            if parsed.path == "/api/platform-specs":
                qs = parse_qs(parsed.query)
                version_id = int(qs["version_id"][0]) if qs.get("version_id") else None
                return self._send({"specs": self.db.list_specs(version_id)})
            spec_parts = [p for p in parsed.path.split("/") if p]
            if len(spec_parts) == 3 and spec_parts[:2] == ["api", "platform-specs"]:
                return self._send(self.db.get_spec(int(spec_parts[2])))
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.db.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.db.list_comments(int(parts[2]))})
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "projects"]:
                return self._send(self.db.create_project(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return self._send(self.db.create_version(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.db.set_glossary(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                return self._send(self.db.assign(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send(self.db.save_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.db.add_comment(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "platform-specs":
                return self._send(self.db.create_spec(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "platform-specs"] and parts[3] == "deliver":
                return self._send(self.db.deliver_spec(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "lock", "deliver"}:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    return self._send(self.db.submit(version_id, actor, role))
                if parts[3] == "lock":
                    return self._send(self.db.lock(version_id, actor, role))
                return self._send(self.db.deliver(version_id, actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                return self._send(self.db.review(int(parts[2]), actor, body, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
