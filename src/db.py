"""
GWAS v2.0 — Database Layer
SQLite models, connection management, migrations, and backup.
"""

import sqlite3
import os
import json
import shutil
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager for GWAS."""

    def __init__(self, db_path: str = "/opt/gwas/data/gwas.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._ensure_schema()
        self._ensure_indexes()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self):
        schema = """
        CREATE TABLE IF NOT EXISTS wallets (
            address TEXT PRIMARY KEY,
            label TEXT,
            quality_score REAL,
            wr_7d REAL,
            pnl_7d REAL,
            trades_7d INTEGER,
            status TEXT DEFAULT 'ACTIVE',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            wallet_address TEXT,
            token_address TEXT,
            token_symbol TEXT,
            action TEXT,
            conviction_score REAL,
            gmgn_link TEXT,
            photon_link TEXT,
            flags TEXT,
            alert_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            executed BOOLEAN DEFAULT FALSE,
            execute_tx_hash TEXT,
            execute_timestamp TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS trades (
            tx_hash TEXT PRIMARY KEY,
            wallet_address TEXT,
            token_address TEXT,
            action TEXT,
            amount_sol REAL,
            price_usd REAL,
            pnl_sol REAL,
            fee_sol REAL,
            timestamp TIMESTAMP,
            correlated_alert_id TEXT
        );

        CREATE TABLE IF NOT EXISTS weekly_reports (
            week_start DATE PRIMARY KEY,
            alerts_sent INTEGER,
            alerts_executed INTEGER,
            execute_rate REAL,
            executed_pnl_sol REAL,
            independent_pnl_sol REAL,
            profit_factor REAL,
            win_rate REAL,
            best_wallet TEXT,
            worst_wallet TEXT,
            dead_wallets_count INTEGER,
            report_json TEXT
        );

        CREATE TABLE IF NOT EXISTS alert_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id TEXT,
            wallet_address TEXT,
            token_address TEXT,
            token_symbol TEXT,
            action TEXT,
            conviction_score REAL,
            gmgn_link TEXT,
            photon_link TEXT,
            flags TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Track rate limiting
        CREATE TABLE IF NOT EXISTS rate_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        with self._connection() as conn:
            conn.executescript(schema)

    def _ensure_indexes(self):
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_alerts_token_ts ON alerts(token_address, alert_timestamp);",
            "CREATE INDEX IF NOT EXISTS idx_alerts_id ON alerts(id);",
            "CREATE INDEX IF NOT EXISTS idx_trades_tx ON trades(tx_hash);",
            "CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON trades(wallet_address, timestamp);",
            "CREATE INDEX IF NOT EXISTS idx_trades_correlated ON trades(correlated_alert_id);",
            "CREATE INDEX IF NOT EXISTS idx_alerts_wallet_ts ON alerts(wallet_address, alert_timestamp);",
            "CREATE INDEX IF NOT EXISTS idx_wallets_status ON wallets(status);",
            "CREATE INDEX IF NOT EXISTS idx_rate_log_ts ON rate_log(timestamp);",
        ]
        with self._connection() as conn:
            for idx in indexes:
                conn.execute(idx)

    # ─── Wallet Operations ───────────────────────────────────────────────

    def upsert_wallet(self, address: str, data: dict):
        """Insert or update a wallet record."""
        with self._connection() as conn:
            existing = conn.execute(
                "SELECT address FROM wallets WHERE address = ?", (address,)
            ).fetchone()
            if existing:
                fields = ", ".join(f"{k} = ?" for k in data.keys())
                values = list(data.values()) + [address]
                conn.execute(
                    f"UPDATE wallets SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE address = ?",
                    values,
                )
            else:
                data["address"] = address
                columns = ", ".join(data.keys())
                placeholders = ", ".join("?" for _ in data)
                conn.execute(
                    f"INSERT INTO wallets ({columns}) VALUES ({placeholders})",
                    list(data.values()),
                )

    def get_wallet(self, address: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM wallets WHERE address = ?", (address,)
            ).fetchone()
            return dict(row) if row else None

    def get_active_wallets(self, min_wr: float = 30, min_trades: int = 10) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM wallets 
                   WHERE status = 'ACTIVE' 
                     AND wr_7d >= ? 
                     AND trades_7d >= ? 
                     AND pnl_7d >= 0""",
                (min_wr, min_trades),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_dead_wallets(self, threshold_alerts: int = 15, min_window_weeks: int = 2) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(weeks=min_window_weeks)
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT w.*, 
                   COUNT(a.id) as alert_count,
                   SUM(CASE WHEN a.executed THEN 1 ELSE 0 END) as executed_count
                   FROM wallets w
                   LEFT JOIN alerts a ON w.address = a.wallet_address 
                     AND a.alert_timestamp >= ?
                   WHERE w.status = 'ACTIVE'
                   GROUP BY w.address
                   HAVING alert_count >= ? AND executed_count = 0""",
                (cutoff.isoformat(), threshold_alerts),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_wallet_status(self, address: str, status: str):
        with self._connection() as conn:
            conn.execute(
                "UPDATE wallets SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE address = ?",
                (status, address),
            )

    # ─── Alert Operations ────────────────────────────────────────────────

    def insert_alert(self, alert: dict) -> str:
        """Insert an alert. Returns alert_id."""
        with self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO alerts 
                   (id, wallet_address, token_address, token_symbol, action, 
                    conviction_score, gmgn_link, photon_link, flags, alert_timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    alert["id"],
                    alert.get("wallet_address", ""),
                    alert.get("token_address", ""),
                    alert.get("token_symbol", ""),
                    alert.get("action", "BUY"),
                    alert.get("conviction_score", 0),
                    alert.get("gmgn_link", ""),
                    alert.get("photon_link", ""),
                    json.dumps(alert.get("flags", [])),
                    alert.get("alert_timestamp", datetime.utcnow().isoformat()),
                ),
            )
            return alert["id"]

    def mark_alert_executed(self, alert_id: str, tx_hash: str, timestamp: str = None):
        ts = timestamp or datetime.utcnow().isoformat()
        with self._connection() as conn:
            conn.execute(
                """UPDATE alerts SET executed = TRUE, execute_tx_hash = ?, 
                   execute_timestamp = ? WHERE id = ?""",
                (tx_hash, ts, alert_id),
            )

    def get_unexecuted_alerts_since(self, since: datetime) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE executed = FALSE AND alert_timestamp >= ? ORDER BY alert_timestamp DESC",
                (since.isoformat(),),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_alert_by_id(self, alert_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_alerts_for_token_wallet(self, token_address: str, wallet_address: str = None) -> list[dict]:
        with self._connection() as conn:
            if wallet_address:
                rows = conn.execute(
                    """SELECT * FROM alerts 
                       WHERE token_address = ? AND wallet_address = ? 
                       ORDER BY alert_timestamp DESC LIMIT 20""",
                    (token_address, wallet_address),
                ).fetchall()
            else:
                # Token-only query for user-wallet correlation (v2.2)
                rows = conn.execute(
                    """SELECT * FROM alerts 
                       WHERE token_address = ? 
                       ORDER BY alert_timestamp DESC LIMIT 20""",
                    (token_address,),
                ).fetchall()
            return [dict(r) for r in rows]

    def has_recent_alert(self, wallet_address: str, token_address: str, within_hours: int = 1) -> bool:
        cutoff = datetime.utcnow() - timedelta(hours=within_hours)
        with self._connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM alerts 
                   WHERE wallet_address = ? AND token_address = ? 
                   AND alert_timestamp >= ?""",
                (wallet_address, token_address, cutoff.isoformat()),
            ).fetchone()
            return row["cnt"] > 0

    # ─── Trade Operations ────────────────────────────────────────────────

    def insert_trade(self, trade: dict):
        with self._connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO trades 
                   (tx_hash, wallet_address, token_address, action, amount_sol, 
                    price_usd, pnl_sol, fee_sol, timestamp, correlated_alert_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade["tx_hash"],
                    trade.get("wallet_address", ""),
                    trade.get("token_address", ""),
                    trade.get("action", "BUY"),
                    trade.get("amount_sol", 0),
                    trade.get("price_usd", 0),
                    trade.get("pnl_sol", 0),
                    trade.get("fee_sol", 0),
                    trade.get("timestamp", datetime.utcnow().isoformat()),
                    trade.get("correlated_alert_id"),
                ),
            )

    def get_recent_trades(self, hours: int = 24) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp DESC",
                (cutoff.isoformat(),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── Rate Limit Operations ───────────────────────────────────────────

    def log_alert_sent(self):
        with self._connection() as conn:
            conn.execute("INSERT INTO rate_log (timestamp) VALUES (?)", (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),))

    def count_alerts_in_window(self, minutes: int = 10) -> int:
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_log WHERE timestamp >= ?",
                (cutoff.isoformat(),),
            ).fetchone()
            return row["cnt"]

    def count_alerts_today(self) -> int:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_log WHERE timestamp >= ?",
                (today_start.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchone()
            return row["cnt"]

    def count_alerts_this_hour(self) -> int:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_log WHERE timestamp >= ?",
                (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchone()
            return row["cnt"]

    def count_alerts_this_cycle(self, since: datetime) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_log WHERE timestamp >= ?",
                (since.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchone()
            return row["cnt"]

    def is_burst_cooldown(self, cooldown_minutes: int = 30, burst_threshold: int = 5, burst_window: int = 10) -> bool:
        """Check if we should be in burst cooldown."""
        cutoff = datetime.utcnow() - timedelta(minutes=burst_window)
        cooldown_cutoff = datetime.utcnow() - timedelta(minutes=cooldown_minutes)
        with self._connection() as conn:
            burst_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_log WHERE timestamp >= ?",
                (cutoff.isoformat(),),
            ).fetchone()["cnt"]
            if burst_count < burst_threshold:
                return False
            # Check if we've sent anything in cooldown window
            after_burst = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_log WHERE timestamp >= ?",
                (cooldown_cutoff.isoformat(),),
            ).fetchone()["cnt"]
            # If burst_count == after_burst, we haven't sent since the burst started
            if burst_count == after_burst:
                return True
            return False

    # ─── Report Operations ───────────────────────────────────────────────

    def get_weekly_stats(self, week_start: str) -> dict:
        week_start_dt = datetime.fromisoformat(week_start)
        week_end = week_start_dt + timedelta(days=7)
        with self._connection() as conn:
            alerts_total = conn.execute(
                "SELECT COUNT(*) as cnt FROM alerts WHERE alert_timestamp >= ? AND alert_timestamp < ?",
                (week_start, week_end.isoformat()),
            ).fetchone()["cnt"]
            alerts_exec = conn.execute(
                "SELECT COUNT(*) as cnt FROM alerts WHERE alert_timestamp >= ? AND alert_timestamp < ? AND executed = TRUE",
                (week_start, week_end.isoformat()),
            ).fetchone()["cnt"]
            pnl_exec = conn.execute(
                "SELECT COALESCE(SUM(pnl_sol), 0) as total FROM trades WHERE timestamp >= ? AND timestamp < ? AND correlated_alert_id IS NOT NULL",
                (week_start, week_end.isoformat()),
            ).fetchone()["total"]
            pnl_all = conn.execute(
                "SELECT COALESCE(SUM(pnl_sol), 0) as total FROM trades WHERE timestamp >= ? AND timestamp < ?",
                (week_start, week_end.isoformat()),
            ).fetchone()["total"]
            wins = conn.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE timestamp >= ? AND timestamp < ? AND pnl_sol > 0 AND correlated_alert_id IS NOT NULL",
                (week_start, week_end.isoformat()),
            ).fetchone()["cnt"]
            losses = conn.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE timestamp >= ? AND timestamp < ? AND pnl_sol <= 0 AND correlated_alert_id IS NOT NULL",
                (week_start, week_end.isoformat()),
            ).fetchone()["cnt"]
            wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
            exec_rate = (alerts_exec / alerts_total * 100) if alerts_total > 0 else 0

        return {
            "alerts_sent": alerts_total,
            "alerts_executed": alerts_exec,
            "execute_rate": round(exec_rate, 1),
            "executed_pnl_sol": round(pnl_exec, 4),
            "independent_pnl_sol": round(pnl_all, 4),
            "win_rate": round(wr, 1),
            "profit_factor": abs(pnl_exec / (pnl_exec - sum_of_wins)) if pnl_exec else 0,
        }

    def get_best_worst_wallets(self, week_start: str) -> tuple:
        week_end = (datetime.fromisoformat(week_start) + timedelta(days=7)).isoformat()
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT wallet_address, SUM(pnl_sol) as total_pnl
                   FROM trades WHERE timestamp >= ? AND timestamp < ? AND correlated_alert_id IS NOT NULL
                   GROUP BY wallet_address ORDER BY total_pnl DESC""",
                (week_start, week_end),
            ).fetchall()
            if not rows:
                return None, None
            return rows[0]["wallet_address"], rows[-1]["wallet_address"]

    def insert_weekly_report(self, report: dict):
        with self._connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO weekly_reports 
                   (week_start, alerts_sent, alerts_executed, execute_rate, executed_pnl_sol, 
                    independent_pnl_sol, profit_factor, win_rate, best_wallet, worst_wallet, 
                    dead_wallets_count, report_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report["week_start"],
                    report["alerts_sent"],
                    report["alerts_executed"],
                    report["execute_rate"],
                    report["executed_pnl_sol"],
                    report["independent_pnl_sol"],
                    report["profit_factor"],
                    report["win_rate"],
                    report.get("best_wallet"),
                    report.get("worst_wallet"),
                    report.get("dead_wallets_count", 0),
                    json.dumps(report),
                ),
            )

    def get_latest_report(self) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM weekly_reports ORDER BY week_start DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    # ─── Backup ──────────────────────────────────────────────────────────

    def backup(self, backup_dir: str = "/opt/gwas/data/backups"):
        os.makedirs(backup_dir, exist_ok=True)
        today = datetime.utcnow().strftime("%Y%m%d")
        backup_path = os.path.join(backup_dir, f"gwas_{today}.db")
        # Use SQLite backup API for safe copy
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        logger.info(f"Database backed up to {backup_path}")

        # Rotate old backups
        cutoff = datetime.utcnow() - timedelta(days=30)
        cutoff_str = cutoff.strftime("gwas_%Y%m%d.db")
        for f in Path(backup_dir).glob("gwas_*.db"):
            if f.name < cutoff_str:
                f.unlink()
                logger.info(f"Removed old backup: {f.name}")


# Global singleton
_db_instance: Database | None = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
