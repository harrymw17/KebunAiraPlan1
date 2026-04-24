"""
scheduler.py — All scheduled jobs for Kebun Aira Bot

Jobs:
  1. Task reminders     → every 6 hours (ping overdue task assignees)
  2. Weekly plan        → Friday 18:00 WIB (next week's farm plan from Excel)
  3. Weekly recap       → Sunday 08:00 WIB (AI summary: finance + tasks + plan)
"""

import json
import logging
import os
from datetime import datetime, date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import anthropic

from database import Database
from excel_reader import get_weekly_plan, format_date_range, get_next_monday

logger = logging.getLogger(__name__)
db = Database()

EMOJI_STATUS = {"selesai": "✅", "lewati": "⏭️"}
BEBAN_EMOJI  = {
    "Tinggi": "🔴", "Sangat Tinggi": "🔴🔴",
    "Sedang-Tinggi": "🟠", "Sedang": "🟡", "Rendah": "🟢",
}


def _api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def _get_primary_chat_id() -> int | None:
    val = db.get_config("primary_chat_id")
    return int(val) if val else None


# ─── Job 1: Task Reminders ─────────────────────────────────────────────────────

async def send_task_reminders(app):
    tasks = db.get_tasks_needing_reminder()
    if not tasks:
        return
    for task in tasks:
        try:
            days_old = (datetime.now() - datetime.fromisoformat(task["created_at"])).days
            await app.bot.send_message(
                chat_id=task["chat_id"],
                text=(
                    f"🔔 *Reminder Task \\#{task['id']}*\n\n"
                    f"Hey @{task['assignee']}\\!\n"
                    f"📝 Tugas: {task['description']}\n"
                    f"📅 Diberikan {days_old} hari lalu oleh {task['assigned_by']}\n\n"
                    f"Ketik /done {task['id']} jika sudah selesai ✅"
                ),
                parse_mode="Markdown",
            )
            db.update_last_reminded(task["id"])
        except Exception as e:
            logger.error(f"Reminder gagal untuk task #{task['id']}: {e}")


# ─── Job 2: Weekly Plan Reminder (Jumat 18:00) ─────────────────────────────────

def _format_weekly_plan_msg(plan: dict, task_statuses: dict, adjusted: bool = False) -> str:
    if not plan.get("in_schedule"):
        return "📅 Minggu depan di luar periode jadwal Mei 2026–April 2027."
    if plan.get("error"):
        return f"❌ Gagal baca Excel: {plan['error']}"

    tasks   = plan.get("tasks", [])
    beban   = plan.get("beban_kerja", "")
    week_lbl = format_date_range(plan["week_start"], plan["week_end"])
    b_icon  = BEBAN_EMOJI.get(beban, "⚪")

    lines = [
        "🌿 *KEBUN AIRA — Rencana Minggu Depan*",
        f"{'🔄 _(Disesuaikan)_' if adjusted else ''}",
        f"📅 {week_lbl}  |  🌤️ {plan.get('musim', '')}  |  Beban {b_icon} {beban}",
        f"\n🎯 *Fokus:* {plan.get('fokus', '')}",
        "\n*📋 Tugas:*",
    ]
    for i, t in enumerate(tasks, 1):
        icon = EMOJI_STATUS.get(task_statuses.get(i, ""), "⬜")
        lines.append(f"{icon} *{i}.* {t[:120]}")

    warnings = [w for w in plan.get("warnings", []) if w]
    if warnings:
        lines.append("\n*⚠️ Perhatian:*")
        lines.extend(f"• {w}" for w in warnings[:3])

    state = db.get_weekly_state(0)  # will be overridden per chat
    budget  = state.get("budget")
    situasi = state.get("situasi", "")
    lines.append(f"\n💰 Budget: {'Rp {:,.0f}'.format(budget).replace(',', '.') if budget else 'belum di-set'}")
    if situasi:
        lines.append(f"📝 Situasi: {situasi}")

    lines += [
        "\n━━━━━━━━━━━━━━━",
        "`/selesai 1` ✅  `/lewati 2` ⏭️  `/situasi [info]` 🔄",
        "`/budget 500000`  `/cek`  `/bantuan`",
    ]
    return "\n".join(l for l in lines if l)


async def send_weekly_plan(app):
    chat_id = _get_primary_chat_id()
    if not chat_id:
        logger.warning("primary_chat_id belum di-set — kirim /start ke bot dulu!")
        return

    next_monday = get_next_monday()
    plan = get_weekly_plan(next_monday)

    state = db.get_weekly_state(chat_id)
    adj_raw = state.get("adjusted_tasks")
    adjusted_tasks = json.loads(adj_raw) if adj_raw else []
    task_statuses  = db.get_plan_task_statuses(chat_id)

    if adjusted_tasks:
        plan["tasks"] = adjusted_tasks

    msg = "📬 *Pengingat Otomatis — Jumat Sore*\n\n" + _format_weekly_plan_msg(
        plan, task_statuses, adjusted=bool(adjusted_tasks)
    )
    # Patch budget/situasi per chat
    budget  = state.get("budget")
    situasi = state.get("situasi", "")
    if budget:
        msg = msg.replace("belum di-set", f"Rp {budget:,.0f}".replace(",", "."))
    if situasi:
        msg += f"\n📝 Situasi: {situasi}"

    try:
        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        logger.info(f"Weekly plan sent to {chat_id}")
    except Exception as e:
        logger.error(f"Gagal kirim weekly plan: {e}")


# ─── Job 3: Weekly Recap (Minggu 08:00) ────────────────────────────────────────

async def send_weekly_recap(app):
    chat_ids = db.get_all_active_chat_ids()
    if not chat_ids:
        return

    api_key = _api_key()
    client  = anthropic.Anthropic(api_key=api_key) if api_key else None

    for chat_id in chat_ids:
        try:
            entries        = db.get_finance_entries(chat_id, days=7)
            tasks_done     = db.get_completed_tasks(chat_id, days=7)
            tasks_pending  = db.get_all_pending_tasks(chat_id)
            messages       = db.get_messages(chat_id, days=7)
            state          = db.get_weekly_state(chat_id)
            plan_statuses  = db.get_plan_task_statuses(chat_id)

            # Plan summary
            plan = get_weekly_plan(get_next_monday())
            plan_tasks = plan.get("tasks", [])
            adj_raw = state.get("adjusted_tasks")
            if adj_raw:
                plan_tasks = json.loads(adj_raw)
            plan_done    = sum(1 for v in plan_statuses.values() if v == "selesai")
            plan_skipped = sum(1 for v in plan_statuses.values() if v == "lewati")
            plan_total   = len(plan_tasks)

            total_expense = sum(e["amount"] for e in entries if e["type"] == "expense")
            total_income  = sum(e["amount"] for e in entries if e["type"] == "income")

            finance_lines  = "\n".join(
                f"- Rp {e['amount']:,.0f} ({e['type']}): {e['description']}" for e in entries
            ) or "Tidak ada transaksi."
            done_lines     = "\n".join(
                f"- #{t['id']} @{t['assignee']}: {t['description']}" for t in tasks_done
            ) or "Tidak ada."
            pending_lines  = "\n".join(
                f"- #{t['id']} @{t['assignee']}: {t['description']}" for t in tasks_pending
            ) or "Tidak ada."
            budget_text    = f"Rp {state['budget']:,.0f}".replace(",", ".") if state.get("budget") else "tidak di-set"

            if client:
                prompt = (
                    f"Buat rekap mingguan dalam Bahasa Indonesia untuk pemilik kebun alpukat Kebun Aira, Turi, Sleman.\n\n"
                    f"RENCANA KEBUN MINGGU INI:\n"
                    f"- Tugas terjadwal: {plan_total}\n"
                    f"- Selesai: {plan_done}, Dilewati: {plan_skipped}, Pending: {plan_total - plan_done - plan_skipped}\n"
                    f"- Budget: {budget_text}\n"
                    f"- Situasi: {state.get('situasi', 'tidak ada catatan')}\n\n"
                    f"TASK KARYAWAN SELESAI:\n{done_lines}\n\n"
                    f"TASK KARYAWAN PENDING:\n{pending_lines}\n\n"
                    f"KEUANGAN MINGGU INI:\n"
                    f"- Pemasukan: Rp {total_income:,.0f}\n"
                    f"- Pengeluaran: Rp {total_expense:,.0f}\n"
                    f"- Net: Rp {total_income - total_expense:,.0f}\n"
                    f"Detail:\n{finance_lines}\n\n"
                    f"TOTAL PESAN GRUP: {len(messages)}\n\n"
                    f"Format: pakai emoji, informatif, max 25 baris. "
                    f"Akhiri dengan 1-2 saran prioritas untuk minggu depan."
                )
                resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=800,
                    messages=[{"role": "user", "content": prompt}],
                )
                ai_text = resp.content[0].text
            else:
                net = total_income - total_expense
                net_sign = "+" if net >= 0 else ""
                ai_text = (
                    f"📋 Rencana kebun: {plan_done}/{plan_total} selesai, {plan_skipped} dilewati\n"
                    f"👷 Task karyawan: {len(tasks_done)} selesai, {len(tasks_pending)} pending\n"
                    f"💰 Keuangan: Rp {total_income:,.0f} masuk, Rp {total_expense:,.0f} keluar\n"
                    f"   Net: {net_sign}Rp {abs(net):,.0f}"
                ).replace(",", ".")

            now = datetime.now().strftime("%d %B %Y")
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"📊 *REKAP MINGGUAN — {now}*\n\n{ai_text}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Rekap gagal untuk chat {chat_id}: {e}")


# ─── Setup ─────────────────────────────────────────────────────────────────────

def setup_scheduler(app) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")

    # Every 6 hours — task reminders
    scheduler.add_job(
        send_task_reminders,
        IntervalTrigger(hours=6),
        args=[app],
        id="task_reminders",
        replace_existing=True,
    )

    # Friday 18:00 WIB — weekly farm plan
    scheduler.add_job(
        send_weekly_plan,
        CronTrigger(day_of_week="fri", hour=18, minute=0, timezone="Asia/Jakarta"),
        args=[app],
        id="weekly_plan",
        replace_existing=True,
    )

    # Sunday 08:00 WIB — weekly recap
    scheduler.add_job(
        send_weekly_recap,
        CronTrigger(day_of_week="sun", hour=8, minute=0, timezone="Asia/Jakarta"),
        args=[app],
        id="weekly_recap",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler aktif: reminders 6j, plan Jumat 18:00, recap Minggu 08:00")
    return scheduler
