"""账单库：SQLite 读写（6.6）。

- 记录粒度为费用行，一行 = 一个 BillRecord；
- 提单号、箱号是列表，拆到子表 bill_bl / bill_container，匹配规则 2、4 才能走索引；
- 费用名另存一列 charge_key（转小写、标点换空格、合并空格），匹配按它比较：
  标准化表里没有的费用名照抄原文，`DETENTION CHARGE` 与 `Detention Charge` 应是同一个费用；
- 金额存规范化后的十进制文本（"65.00" 与 "65" 存成同一个值），不用 REAL，避免浮点误差；
- 每次 UPDATE 先把旧记录快照写进 bill_history，再写新值，version + 1；
  更新带 expected_version，版本对不上就报错（乐观锁）。

这里只负责按匹配键把候选记录查出来；匹配优先级、冲突判断、交易双方别名归一化在 matcher 里做。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from pydantic_core import to_jsonable_python

from bill_agent.models import BillRecord
from bill_agent.normalize.charge import charge_key

SCHEMA_VERSION = 2

# 主表的标量列，与 BillRecord 字段一一对应（bl_nos / container_nos 在子表）
_SCALAR = (
    "record_id",
    "document_type", "document_type_raw", "reference_no", "document_date", "document_date_raw",
    "issuer", "recipient", "payee", "payer", "shipper", "consignee", "carrier",
    "booking_no",
    "line_no", "charge_item", "original_charge_item", "quantity", "unit_price", "amount",
    "currency", "currency_raw", "payment_term", "payment_term_raw",
    "accounting_entity", "posting_type",
    "version", "updated_at",
)
_JSON = ("posting_reasoning", "source_ids", "evidence", "review_reasons")
_LIST_TABLES = {"bl_nos": ("bill_bl", "bl_no"), "container_nos": ("bill_container", "container_no")}
_COLUMNS = _SCALAR + _JSON
_META = frozenset({"record_id", "version", "updated_at"})

_DDL = f"""
CREATE TABLE bill_records (
  record_id            TEXT PRIMARY KEY,
  {", ".join(f"{c} TEXT" for c in _SCALAR if c not in ("record_id", "line_no", "version", "updated_at",
                                                         "original_charge_item"))},
  original_charge_item TEXT NOT NULL,
  line_no              INTEGER,
  {", ".join(f"{c} TEXT" for c in _JSON)},
  version              INTEGER NOT NULL DEFAULT 1,
  updated_at           TEXT NOT NULL
);
CREATE TABLE bill_bl (
  record_id TEXT NOT NULL REFERENCES bill_records(record_id) ON DELETE CASCADE,
  bl_no     TEXT NOT NULL,
  PRIMARY KEY (record_id, bl_no)
);
CREATE TABLE bill_container (
  record_id    TEXT NOT NULL REFERENCES bill_records(record_id) ON DELETE CASCADE,
  container_no TEXT NOT NULL,
  PRIMARY KEY (record_id, container_no)
);
CREATE TABLE bill_history (
  record_id      TEXT NOT NULL,
  version        INTEGER NOT NULL,   -- 快照对应的旧版本号
  snapshot       TEXT NOT NULL,      -- 旧记录完整 JSON
  changed_fields TEXT NOT NULL,      -- [{"field", "old_value", "new_value"}]
  changed_at     TEXT NOT NULL,
  source_id      TEXT,
  PRIMARY KEY (record_id, version)
);
CREATE INDEX idx_ref_charge     ON bill_records(reference_no, charge_item);
CREATE INDEX idx_booking        ON bill_records(booking_no, charge_item, currency);
CREATE INDEX idx_bl             ON bill_bl(bl_no);
CREATE INDEX idx_container      ON bill_container(container_no);
"""

# 结构版本 n -> n+1 的迁移语句
_MIGRATIONS = {
    1: """
ALTER TABLE bill_records ADD COLUMN charge_key TEXT;
DROP INDEX idx_ref_charge;
DROP INDEX idx_booking;
CREATE INDEX idx_ref_charge ON bill_records(reference_no, charge_key);
CREATE INDEX idx_booking    ON bill_records(booking_no, charge_key, currency);
""",
}


def _charge_key(rec) -> str:
    return charge_key(rec.charge_item or rec.original_charge_item)


class RepositoryError(Exception):
    pass


class VersionConflict(RepositoryError):
    """更新时记录版本已变化（被别的流程改过）或记录不存在。"""


def _decimal_text(d: Decimal | None) -> str | None:
    if d is None:
        return None
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "0") else s


def _to_db(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _JSON:
        if name == "evidence":
            return json.dumps({k: v.model_dump(mode="json") for k, v in value.items()}, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _snapshot(rec: BillRecord) -> str:
    return json.dumps(rec.model_dump(mode="json"), ensure_ascii=False)


def _json_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return _decimal_text(v)
    return to_jsonable_python(v)


@dataclass
class ImportReport:
    source_path: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BillRepository:
    def __init__(self, path: str | Path = "data/bills.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)   # 事务由 transaction() 显式控制
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "BillRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 结构与事务

    def _migrate(self) -> None:
        v = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if v > SCHEMA_VERSION:
            raise RepositoryError(f"数据库结构版本 {v} 比程序支持的 {SCHEMA_VERSION} 新")
        with self.transaction():
            if v == 0:                      # 新库：先建第 1 版结构，再逐级迁移到最新
                self._run(_DDL)
                v = 1
            while v < SCHEMA_VERSION:
                self._run(_MIGRATIONS[v])
                if v == 1:                  # 已有记录补 charge_key
                    rows = self.conn.execute("SELECT record_id, charge_item, original_charge_item "
                                             "FROM bill_records").fetchall()
                    self.conn.executemany("UPDATE bill_records SET charge_key = ? WHERE record_id = ?",
                                          [(charge_key(r[1] or r[2]), r[0]) for r in rows])
                v += 1
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _run(self, script: str) -> None:
        for stmt in filter(str.strip, script.split(";")):
            self.conn.execute(stmt)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """一个批次一个事务；可嵌套，只有最外层提交或回滚。"""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ 读

    def get(self, record_id: str) -> BillRecord | None:
        rows = self._select("WHERE r.record_id = ?", (record_id,))
        return rows[0] if rows else None

    def all(self) -> list[BillRecord]:
        return self._select("", ())

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM bill_records").fetchone()[0]

    def history(self, record_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM bill_history WHERE record_id = ? ORDER BY version", (record_id,)).fetchall()
        return [{"record_id": r["record_id"], "version": r["version"], "snapshot": json.loads(r["snapshot"]),
                 "changed_fields": json.loads(r["changed_fields"]), "changed_at": r["changed_at"],
                 "source_id": r["source_id"]} for r in rows]

    # 匹配候选（6.6 四级规则）：任一匹配键为 null 时返回空列表，表示跳过该级

    def find_by_ref_charge(self, reference_no: str | None, charge_item: str | None) -> list[BillRecord]:
        """规则 1：单据号 + 费用名称（按 charge_key 比较，下同）。同一单据可能有同名费用行，可能返回多条，由 matcher 用行号 / 金额消歧。"""
        if not reference_no or not charge_item:
            return []
        return self._select("WHERE r.reference_no = ? AND r.charge_key = ?",
                            (reference_no, charge_key(charge_item)))

    def find_by_bl(self, bl_no: str | None, charge_item: str | None, currency: str | None) -> list[BillRecord]:
        """规则 2：提单号 + 费用名称 + 币种。"""
        if not bl_no or not charge_item or not currency:
            return []
        return self._select(
            "WHERE r.record_id IN (SELECT record_id FROM bill_bl WHERE bl_no = ?) "
            "AND r.charge_key = ? AND r.currency = ?", (bl_no, charge_key(charge_item), currency))

    def find_by_booking(self, booking_no: str | None, charge_item: str | None,
                        currency: str | None) -> list[BillRecord]:
        """规则 3：订舱号 + 费用名称 + 币种。"""
        if not booking_no or not charge_item or not currency:
            return []
        return self._select("WHERE r.booking_no = ? AND r.charge_key = ? AND r.currency = ?",
                            (booking_no, charge_key(charge_item), currency))

    def find_by_container(self, container_no: str | None, document_date: date | None,
                          amount: Decimal | None) -> list[BillRecord]:
        """规则 4：箱号 + 日期 + 金额。交易双方要先过别名表归一化，由 matcher 在结果上再筛。"""
        if not container_no or document_date is None or amount is None:
            return []
        return self._select(
            "WHERE r.record_id IN (SELECT record_id FROM bill_container WHERE container_no = ?) "
            "AND r.document_date = ? AND r.amount = ?",
            (container_no, document_date.isoformat(), _decimal_text(amount)))

    def _select(self, where: str, params: tuple) -> list[BillRecord]:
        rows = self.conn.execute(
            f"SELECT * FROM bill_records r {where} ORDER BY r.reference_no, r.line_no, r.record_id",
            params).fetchall()
        if not rows:
            return []
        ids = [r["record_id"] for r in rows]
        lists = {name: self._list_values(table, col, ids) for name, (table, col) in _LIST_TABLES.items()}
        out = []
        for r in rows:
            data = {c: r[c] for c in _COLUMNS if r[c] is not None}
            for c in _JSON:
                if c in data:
                    data[c] = json.loads(data[c])
            for name in _LIST_TABLES:
                data[name] = lists[name].get(r["record_id"], [])
            out.append(BillRecord.model_validate(data))
        return out

    def _list_values(self, table: str, col: str, ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        marks = ",".join("?" * len(ids))
        for r in self.conn.execute(
                f"SELECT record_id, {col} FROM {table} WHERE record_id IN ({marks}) ORDER BY rowid", ids):
            out.setdefault(r[0], []).append(r[1])
        return out

    # ------------------------------------------------------------------ 写

    def insert(self, rec: BillRecord) -> BillRecord:
        with self.transaction():
            data = {c: _to_db(c, getattr(rec, c)) for c in _COLUMNS}
            data["charge_key"] = _charge_key(rec)
            self.conn.execute(
                f"INSERT INTO bill_records ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                tuple(data.values()))
            self._write_lists(rec.record_id, rec)
        return rec

    def update(self, record_id: str, changes: dict[str, Any], *, expected_version: int,
               source_id: str | None = None) -> BillRecord:
        """只写入 changes 中的字段（6.7：只改输入材料明确要求变更的字段）。

        changes 为空或新旧值相同时不写库，原样返回；否则旧记录快照进 bill_history，version + 1。
        """
        with self.transaction():
            old = self.get(record_id)
            if old is None:
                raise VersionConflict(f"记录 {record_id} 不存在")
            if old.version != expected_version:
                raise VersionConflict(f"记录 {record_id} 版本为 {old.version}，期望 {expected_version}")
            bad = set(changes) - set(BillRecord.model_fields) | (set(changes) & _META)
            if bad:
                raise RepositoryError(f"不能更新的字段: {sorted(bad)}")

            # 经过模型校验，保证写进去的值类型合法
            new = BillRecord.model_validate({**old.model_dump(), **changes})
            diffs = [f for f in changes if getattr(old, f) != getattr(new, f)]
            if not diffs:
                return old
            if source_id and source_id not in new.source_ids:
                new.source_ids = [*new.source_ids, source_id]
            new.version = old.version + 1
            new.updated_at = datetime.now()

            self.conn.execute(
                "INSERT INTO bill_history (record_id, version, snapshot, changed_fields, changed_at, source_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (record_id, old.version, _snapshot(old),
                 json.dumps([{"field": f, "old_value": _json_value(getattr(old, f)),
                              "new_value": _json_value(getattr(new, f))} for f in diffs if f not in _JSON],
                            ensure_ascii=False),
                 new.updated_at.isoformat(), source_id))

            cols = [c for c in _COLUMNS if c != "record_id"]
            cur = self.conn.execute(
                f"UPDATE bill_records SET {', '.join(f'{c} = ?' for c in cols)}, charge_key = ? "
                "WHERE record_id = ? AND version = ?",
                (*(_to_db(c, getattr(new, c)) for c in cols), _charge_key(new), record_id, old.version))
            if cur.rowcount != 1:
                raise VersionConflict(f"记录 {record_id} 更新时版本已变化")
            if set(diffs) & set(_LIST_TABLES):
                self._write_lists(record_id, new, replace=True)
            return new

    def _write_lists(self, record_id: str, rec: BillRecord, replace: bool = False) -> None:
        for name, (table, col) in _LIST_TABLES.items():
            if replace:
                self.conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
            values = list(dict.fromkeys(getattr(rec, name)))   # 去重、保序
            self.conn.executemany(f"INSERT INTO {table} (record_id, {col}) VALUES (?, ?)",
                                  [(record_id, v) for v in values])

    # ------------------------------------------------------------------ 导入已有账单 JSON

    def import_bills(self, bill_json) -> ImportReport:
        """把已有账单 JSON（parsers.BillJSON）导入账单库，按 record_id upsert。

        没有 record_id 的记录用"文件内容哈希 + 序号"生成固定 id，同一文件重复导入结果不变。
        整个文件一个事务；单条记录格式错误只记入 errors，不影响其他记录。
        """
        from .bill_json import bill_from_json

        report = ImportReport(bill_json.source_path)
        with self.transaction():
            for i, raw in enumerate(bill_json.records):
                try:
                    rec, warnings = bill_from_json(raw, bill_json.source_id, i)
                except ValueError as e:
                    report.errors.append(f"第 {i + 1} 条: {e}")
                    continue
                report.warnings += [f"第 {i + 1} 条: {w}" for w in warnings]
                old = self.get(rec.record_id)
                if old is None:
                    self.insert(rec)
                    report.created.append(rec.record_id)
                    continue
                changes = {k: v for k, v in rec.model_dump(exclude=_META).items()
                           if k not in ("source_ids",) and getattr(old, k) != getattr(rec, k)}
                if not changes:
                    report.unchanged.append(rec.record_id)
                    continue
                self.update(rec.record_id, changes, expected_version=old.version, source_id=bill_json.source_id)
                report.updated.append(rec.record_id)
        return report
