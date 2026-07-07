"""
GWAS v2.0 — Performance Engine
Generates weekly reports comparing alert-driven trades vs independent trading.
"""

import logging
import json
from datetime import datetime, timedelta
from typing import Optional

from .db import get_db

logger = logging.getLogger(__name__)


def compute_weekly_report(
    week_start: Optional[datetime] = None,
    alert_sender=None,
) -> dict:
    """
    Generate a weekly performance report.
    
    Metrics:
      - Alerts sent / executed / execution rate
      - Executed PnL vs independent trading PnL
      - Profit factor, win rate
      - Best & worst wallets
      - Dead wallet count
    
    Returns report dict.
    """
    db = get_db()

    if week_start is None:
        # Default to last completed week (Monday 00:00 UTC)
        now = datetime.utcnow()
        days_since_monday = now.weekday()  # Monday=0
        week_start = (now - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # If today is Monday, use last week
        if days_since_monday == 0:
            week_start -= timedelta(days=7)

    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end = week_start + timedelta(days=7)

    # ── Basic counts ─────────────────────────────────────────────────────
    with db._connection() as conn:
        alerts_total = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE alert_timestamp >= ? AND alert_timestamp < ?",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()["cnt"]

        alerts_exec = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE alert_timestamp >= ? AND alert_timestamp < ? AND executed = TRUE",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()["cnt"]

        # Executed trade PnL (trades correlated to alerts)
        exec_pnl_rows = conn.execute(
            """SELECT COALESCE(SUM(pnl_sol), 0) as total_pnl,
                      COUNT(*) as trade_count,
                      COALESCE(SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END), 0) as wins,
                      COALESCE(SUM(CASE WHEN pnl_sol <= 0 THEN 1 ELSE 0 END), 0) as losses
               FROM trades WHERE timestamp >= ? AND timestamp < ? AND correlated_alert_id IS NOT NULL""",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()

        # All trades (independent)
        all_pnl_rows = conn.execute(
            """SELECT COALESCE(SUM(pnl_sol), 0) as total_pnl,
                      COUNT(*) as trade_count,
                      COALESCE(SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END), 0) as wins,
                      COALESCE(SUM(CASE WHEN pnl_sol <= 0 THEN 1 ELSE 0 END), 0) as losses
               FROM trades WHERE timestamp >= ? AND timestamp < ?""",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()

        # Best and worst wallets by executed PnL
        best_worst_rows = conn.execute(
            """SELECT wallet_address, SUM(pnl_sol) as total_pnl
               FROM trades WHERE timestamp >= ? AND timestamp < ? AND correlated_alert_id IS NOT NULL
               GROUP BY wallet_address ORDER BY total_pnl DESC""",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchall()

        # Dead wallets
        dead = db.get_dead_wallets()
        dead_count = len(dead)

    # ── Compute metrics ──────────────────────────────────────────────────
    total_exec_trades = exec_pnl_rows["trade_count"] or 0
    total_wins = exec_pnl_rows["wins"] or 0
    total_losses = exec_pnl_rows["losses"] or 0
    executed_pnl = round(exec_pnl_rows["total_pnl"], 4)
    independent_pnl = round(all_pnl_rows["total_pnl"], 4)

    execute_rate = round(alerts_exec / alerts_total * 100, 1) if alerts_total > 0 else 0

    # Win rate on executed
    wr = round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) > 0 else 0

    # Profit factor: gross profit / gross loss (absolute)
    gross_profit = sum(
        1 for _ in range(total_wins)
    )  # placeholder, we have total pnl but need gross
    # Better PF: if we have individual trade data
    if total_losses > 0 and executed_pnl > 0:
        # Approximate: if total PnL positive but some losses, PF = (gain + |loss|) / |loss|
        pass

    profit_factor = 0.0
    with db._connection() as conn:
        gross_profit_row = conn.execute(
            """SELECT COALESCE(SUM(pnl_sol), 0) as gross FROM trades 
               WHERE timestamp >= ? AND timestamp < ? AND correlated_alert_id IS NOT NULL AND pnl_sol > 0""",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()
        gross_loss_row = conn.execute(
            """SELECT COALESCE(SUM(pnl_sol), 0) as gross FROM trades 
               WHERE timestamp >= ? AND timestamp < ? AND correlated_alert_id IS NOT NULL AND pnl_sol < 0""",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()

        gross_profit_amount = gross_profit_row["gross"]
        gross_loss_amount = abs(gross_loss_row["gross"])
        if gross_loss_amount > 0:
            profit_factor = round(gross_profit_amount / gross_loss_amount, 2)
        elif gross_profit_amount > 0:
            profit_factor = float("inf")

    best_wallet = best_worst_rows[0]["wallet_address"] if best_worst_rows else None
    worst_wallet = best_worst_rows[-1]["wallet_address"] if best_worst_rows else None

    report = {
        "week_start": week_start_str,
        "alerts_sent": alerts_total,
        "alerts_executed": alerts_exec,
        "execute_rate": execute_rate,
        "executed_pnl_sol": executed_pnl,
        "independent_pnl_sol": independent_pnl,
        "profit_factor": profit_factor,
        "win_rate": wr,
        "best_wallet": best_wallet,
        "worst_wallet": worst_wallet,
        "dead_wallets_count": dead_count,
        "total_executed_trades": total_exec_trades,
        "generated_at": datetime.utcnow().isoformat(),
    }

    # Persist to DB
    db.insert_weekly_report(report)

    return report


def format_report_telegram(report: dict) -> str:
    """
    Format a weekly report for Telegram.
    """
    pf_str = f"{report['profit_factor']:.2f}" if report['profit_factor'] != float('inf') else "∞"
    alert_beat = (
        "✅ Beating independent" 
        if report["executed_pnl_sol"] > report["independent_pnl_sol"]
        else "⚠️ Below independent"
    )

    lines = [
        f"📊 GWAS WEEKLY REPORT — Week of {report['week_start']}",
        "",
        f"*Alerts*: {report['alerts_sent']} sent / {report['alerts_executed']} executed ({report['execute_rate']}% rate)",
        f"*Executed PnL*: {report['executed_pnl_sol']:.4f} SOL",
        f"*Independent PnL*: {report['independent_pnl_sol']:.4f} SOL",
        f"*Alert vs Independent*: {alert_beat}",
        f"*Win Rate* (executed): {report['win_rate']}%",
        f"*Profit Factor*: {pf_str}",
        f"*Best Wallet*: `{report.get('best_wallet', 'N/A')}`",
        f"*Worst Wallet*: `{report.get('worst_wallet', 'N/A')}`",
        f"🔒 Dead Wallets: {report.get('dead_wallets_count', 0)}",
        "",
        f"_Generated: {report['generated_at'][:19]}_",
    ]
    return "\n".join(lines)


def send_weekly_report(alert_sender, week_start: Optional[datetime] = None) -> bool:
    """
    Generate and send weekly report to Telegram.
    """
    report = compute_weekly_report(week_start=week_start)
    text = format_report_telegram(report)
    if alert_sender:
        return alert_sender.send_report(text)
    logger.info(f"Weekly report generated: {report['week_start']}")
    return True
