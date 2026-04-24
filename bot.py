"""
bot.py — Kebun Aira Bot (combined)

Semua fitur dalam satu bot:

TASK MANAGEMENT (dari bot lama):
  /task @nama deskripsi  — assign task ke karyawan
  /tasks                 — lihat semua task pending + update terakhir
  /done [id]             — tandai task selesai (atau via inline button)
  /update [id] pesan     — kirim progress update
  /taskdetail [id]       — riwayat lengkap task

JADWAL KEBUN (dari Excel):
  /rencana               — rencana minggu depan dari Jadwal Excel
  /selesai [n]           — tandai tugas rencana ke-n selesai
  /lewati [n]            — skip tugas rencana ke-n
  /cek                   — progress rencana minggu ini

BUDGET & SITUASI:
  /budget [jumlah]       — set budget minggu ini
  /situasi [keterangan]  — update kondisi lapangan + AI menyesuaikan rencana

KEUANGAN (otomatis dari pesan):
  /finance               — rekap keuangan 30 hari terakhir
  Pesan bebas            — angka & kata kunci keuangan dicatat otomatis

REKAP:
  /recap                 — rekap AI on-demand (task + rencana + keuangan)

LAINNYA:
  /start                 — daftarkan chat + welcome
  /bantuan               — daftar semua perintah
  /reset                 — reset state rencana minggu ini
"""

import json
import logging
import os
import re
from datetime import datetime, date

import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from database import Database
from excel_reader import (
    format_date_range, get_next_monday, get_weekly_plan,
    MONTH_NAMES_ID
)
from scheduler import setup_scheduler

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database()

# ─── Config ────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok
    # Fallback: config.json (local dev)
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            return json.load(f).get("telegram_token", "")
    raise ValueError("TELEGRAM_BOT_TOKEN not set. Run setup.py or set env var.")

def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            return json.load(f).get("anthropic_api_key", "")
    return ""

# ─── Finance keywords ──────────────────────────────────────────────────────────

FINANCE_KEYWORDS = [
    "rp", "rupiah", "bayar", "beli", "receipt", "struk", "bon",
    "tagihan", "transfer", "terima", "jual", "harga", "biaya",
    "ongkos", "kwitansi", "invoice", "nota", "pembayaran", "sewa",
    "gaji", "upah", "modal", "untung", "rugi", "hutang", "piutang",
]

SITUASI_KEYWORDS = [
    "sakit", "tidak bisa", "absen", "izin", "libur", "tunda",
    "hujan", "banjir", "becek", "kering",
    "layu", "mati", "uret", "hama", "rusak", "jebol",
    "darurat", "mati lampu", "pompa rusak",
]

BEBAN_EMOJI = {
    "Tinggi": "🔴", "Sangat Tinggi": "🔴🔴",
    "Sedang-Tinggi": "🟠", "Sedang": "🟡", "Rendah": "🟢",
}
EMOJI_STATUS = {"selesai": "✅", "lewati": "⏭️"}


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _parse_budget_from_text(text: str) -> int | None:
    """Try to parse an integer Rupiah amount from free text."""
    raw = text.replace(".", "").replace(",", "")
    raw = re.sub(r"(?i)(rb|ribu)$", "000", raw)
    raw = re.sub(r"(?i)(jt|juta)$", "000000", raw)
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits and int(digits) > 0 else None


def _format_rp(amount: int) -> str:
    return f"Rp {amount:,}".replace(",", ".")


async def _adjust_plan_with_claude(tasks: list[str], situasi: str,
                                   budget: int | None, fokus: str, musim: str) -> dict:
    """Call Claude to intelligently adjust the farm plan given situation."""
    api_key = _get_api_key()
    if not api_key:
        return _keyword_fallback(tasks, situasi, budget)

    budget_text = _format_rp(budget) if budget else "tidak di-set"
    task_text   = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"Kamu asisten manajer kebun alpukat Kebun Aira, Turi, Sleman, DIY.\n\n"
                    f"RENCANA SAAT INI:\n{task_text}\n\n"
                    f"FOKUS BULAN: {fokus}\nMUSIM: {musim}\nBUDGET: {budget_text}\n\n"
                    f"SITUASI LAPANGAN:\n{situasi}\n\n"
                    f"Sesuaikan rencana: prioritaskan yang penting, tunda yang bisa ditunda, "
                    f"hapus yang tidak feasible, tambah tugas darurat jika perlu.\n\n"
                    f"Balas HANYA dengan JSON ini:\n"
                    f'{{ "adjusted_tasks": ["tugas 1", ...], "penjelasan": "...", "prioritas_utama": "..." }}\n'
                    f"Max 8 tugas. Bahasa Indonesia."
                ),
            }],
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        return {
            "success": True,
            "adjusted_tasks": result.get("adjusted_tasks", tasks),
            "penjelasan": result.get("penjelasan", ""),
            "prioritas_utama": result.get("prioritas_utama", ""),
        }
    except Exception as e:
        logger.warning(f"Claude plan adjustment gagal: {e}")
        return _keyword_fallback(tasks, situasi, budget)


def _keyword_fallback(tasks: list[str], situasi: str, budget: int | None) -> dict:
    """Simple keyword-based plan adjustment without Claude."""
    sl = situasi.lower()
    adjusted, notes = list(tasks), []

    if any(k in sl for k in ["sakit", "tidak bisa", "absen"]):
        notes.append("⚠️ Bayu tidak tersedia — tunda tugas fisik berat")
        adjusted = [t for t in adjusted if not any(k in t.lower() for k in ["cangkul", "gali", "angkat"])]

    if any(k in sl for k in ["hujan", "banjir", "becek"]):
        notes.append("🌧️ Cuaca hujan — tunda semprot & tanam")
        adjusted = [t for t in adjusted if not any(k in t.lower() for k in ["semprot", "pruning", "tanam"])]

    if budget and budget < 200_000:
        notes.append(f"💰 Budget terbatas {_format_rp(budget)} — skip pembelian material")
        adjusted = [t for t in adjusted if not any(k in t.lower() for k in ["beli", "pupuk", "furadan", "dolomit"])]

    if any(k in sl for k in ["layu", "mati", "uret", "hama"]):
        adjusted.insert(0, "🚨 DARURAT: Cek pohon layu — gali 50cm, cari uret, beri Furadan")

    penjelasan = " | ".join(notes) if notes else "Rencana disesuaikan berdasarkan situasi."
    return {"success": True, "adjusted_tasks": adjusted[:8] or tasks[:3], "penjelasan": penjelasan, "prioritas_utama": adjusted[0] if adjusted else ""}


def _build_plan_message(plan: dict, task_statuses: dict, adjusted: bool = False,
                        budget: int = None, situasi: str = "") -> str:
    if not plan.get("in_schedule"):
        return "📅 Minggu ini di luar periode jadwal Mei 2026–April 2027."
    if plan.get("error"):
        return f"❌ Error baca Excel: {plan['error']}"

    tasks  = plan.get("tasks", [])
    beban  = plan.get("beban_kerja", "")
    b_icon = BEBAN_EMOJI.get(beban, "⚪")
    wk_lbl = format_date_range(plan["week_start"], plan["week_end"])

    lines  = [
        "🌿 *KEBUN AIRA — Rencana Minggu Depan*",
    ]
    if adjusted:
        lines.append("🔄 _(Disesuaikan AI)_")
    lines += [
        f"📅 {wk_lbl}",
        f"🌤️ Musim: {plan.get('musim','')}   |   Beban: {b_icon} {beban}",
        f"\n🎯 *Fokus:* {plan.get('fokus','')}",
        "\n*📋 Tugas:*",
    ]
    for i, t in enumerate(tasks, 1):
        st   = task_statuses.get(i, "")
        icon = EMOJI_STATUS.get(st, "⬜")
        lines.append(f"{icon} *{i}.* {t[:120]}")

    warnings = [w for w in plan.get("warnings", []) if w]
    if warnings:
        lines.append("\n*⚠️ Perhatian:*")
        lines.extend(f"• {w}" for w in warnings[:3])

    people = plan.get("people_notes", [])
    if people:
        lines.append("\n*👥 Info Tenaga:*")
        lines.extend(f"• {p}" for p in people[:2])

    lines.append(f"\n💰 Budget: {_format_rp(budget) if budget else 'belum di-set'}")
    if situasi:
        lines.append(f"📝 Situasi: {situasi}")

    lines += [
        "\n━━━━━━━━━━━━━━━",
        "`/selesai 1`  `/lewati 2`  `/situasi [info]`",
        "`/budget 500000`  `/cek`  `/bantuan`",
    ]
    return "\n".join(lines)


def _build_progress_message(plan: dict, task_statuses: dict) -> str:
    tasks = plan.get("tasks", [])
    if not tasks:
        return "📋 Tidak ada tugas terjadwal minggu ini."
    done    = sum(1 for v in task_statuses.values() if v == "selesai")
    skipped = sum(1 for v in task_statuses.values() if v == "lewati")
    total   = len(tasks)
    pct     = int(done / total * 100) if total else 0
    bar     = "█" * (pct // 10) + "░" * (10 - pct // 10)
    lines   = [
        f"📊 *Progress Rencana Minggu Ini*",
        f"`{bar}` {pct}%",
        f"✅ Selesai: {done}/{total}  ⏭️ Skip: {skipped}  ⬜ Pending: {total - done - skipped}",
        "\n*Detail:*",
    ]
    for i, t in enumerate(tasks, 1):
        icon = EMOJI_STATUS.get(task_statuses.get(i, ""), "⬜")
        lines.append(f"{icon} {i}. {t[:80]}")
    return "\n".join(lines)


# ─── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.set_config("primary_chat_id", str(chat_id))
    db.store_message(chat_id, "system", "/start", datetime.now())
    await update.message.reply_text(
        "👋 *Halo! Kebun Aira Bot siap\\!*\n\n"
        "Saya akan bantu kelola kebun kamu:\n"
        "🌿 Rencana mingguan otomatis dari Excel\n"
        "💼 Task karyawan + reminder otomatis\n"
        "💰 Catatan keuangan otomatis dari chat\n"
        "🤖 Penyesuaian rencana via AI\n\n"
        "Chat ID terdaftar ✅ Pengingat akan dikirim ke sini.\n\n"
        "Ketik /bantuan untuk semua perintah.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ── Task management ────────────────────────────────────────────────────────────

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/task @nama deskripsi`\nContoh: `/task @bayu siram tanaman sore ini`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text    = " ".join(context.args)
    assignee, desc_words = None, []
    for w in text.split():
        if w.startswith("@") and not assignee:
            assignee = w[1:]
        else:
            desc_words.append(w)

    if not assignee:
        await update.message.reply_text(
            "⚠️ Sebutkan @nama.\nContoh: `/task @bayu cek kebun`", parse_mode=ParseMode.MARKDOWN
        )
        return
    description = " ".join(desc_words)
    if not description:
        await update.message.reply_text("⚠️ Deskripsi task tidak boleh kosong.")
        return

    assigned_by = update.effective_user.username or update.effective_user.first_name
    task_id     = db.add_task(update.effective_chat.id, assignee, description, assigned_by)
    await update.message.reply_text(
        f"✅ *Task #{task_id} dibuat!*\n\n"
        f"👤 Untuk: @{assignee}\n"
        f"📝 Tugas: {description}\n"
        f"🔔 Reminder otomatis setiap 2 hari.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = db.get_all_pending_tasks(update.effective_chat.id)
    if not tasks:
        await update.message.reply_text("✨ Tidak ada task pending saat ini.")
        return
    text = "📋 *Task Aktif:*\n\n"
    for t in tasks:
        days_old = (datetime.now() - datetime.fromisoformat(t["created_at"])).days
        age      = f"{days_old} hari" if days_old else "hari ini"
        updates  = db.get_task_updates(t["id"])
        last     = updates[-1] if updates else None
        text += f"*#{t['id']}* @{t['assignee']}\n"
        text += f"   📝 {t['description']}\n"
        text += f"   ⏱ {age} lalu — oleh {t['assigned_by']}\n"
        if last:
            ts = datetime.fromisoformat(last["created_at"]).strftime("%d/%m %H:%M")
            text += f"   💬 [{ts}]: {last['message']}\n"
        else:
            text += "   💬 Belum ada update\n"
        text += "\n"
    text += "_/update [id] [pesan]  |  /done [id]  |  /taskdetail [id]_"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.first_name
    chat_id  = update.effective_chat.id
    if context.args:
        try:
            task_id = int(context.args[0])
            task    = db.complete_task(task_id)
            if task:
                await update.message.reply_text(
                    f"🎉 *Task #{task_id} selesai!*\nKerja bagus, @{task['assignee']}! 💪",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.message.reply_text("❌ Task tidak ditemukan.")
        except ValueError:
            await update.message.reply_text("Format: `/done [id]`", parse_mode=ParseMode.MARKDOWN)
        return

    # Interactive button list
    all_tasks = db.get_all_pending_tasks(chat_id)
    if not all_tasks:
        await update.message.reply_text("✨ Tidak ada task pending.")
        return
    keyboard = [
        [InlineKeyboardButton(
            f"#{t['id']} @{t['assignee']}: {t['description'][:40]}",
            callback_data=f"done_{t['id']}"
        )] for t in all_tasks
    ]
    await update.message.reply_text("Pilih task yang selesai:", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.first_name
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: `/update [id] pesan`\nContoh: `/update 3 sudah siram 50 pohon`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka.", parse_mode=ParseMode.MARKDOWN)
        return
    message = " ".join(context.args[1:])
    if not db.add_task_update(task_id, username, message):
        await update.message.reply_text(f"❌ Task #{task_id} tidak ditemukan / sudah selesai.")
        return
    task = db.get_task_by_id(task_id)
    await update.message.reply_text(
        f"📝 *Update Task #{task_id} diterima!*\n\n"
        f"👤 @{username}\n📋 {task['description']}\n💬 {message}\n\n"
        f"_/done {task_id} jika sudah selesai_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_taskdetail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: `/taskdetail [id]`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka.", parse_mode=ParseMode.MARKDOWN)
        return
    task = db.get_task_by_id(task_id)
    if not task:
        await update.message.reply_text(f"❌ Task #{task_id} tidak ditemukan.")
        return
    updates = db.get_task_updates(task_id)
    created = datetime.fromisoformat(task["created_at"]).strftime("%d/%m %H:%M")
    icon    = "✅" if task["status"] == "done" else "🔄"
    text    = (
        f"{icon} *Task #{task_id}*\n\n"
        f"📝 Tugas: {task['description']}\n"
        f"👤 @{task['assignee']} (dari {task['assigned_by']})\n"
        f"📅 Dibuat: {created}   🏷 {task['status'].upper()}\n"
    )
    if updates:
        text += f"\n*Riwayat ({len(updates)}):*\n"
        for u in updates:
            ts = datetime.fromisoformat(u["created_at"]).strftime("%d/%m %H:%M")
            text += f"  `[{ts}]` @{u['username']}: {u['message']}\n"
    else:
        text += "\n_Belum ada update._\n"
    if task["status"] == "pending":
        text += f"\n_/update {task_id} [pesan]  |  /done {task_id}_"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Farm plan ──────────────────────────────────────────────────────────────────

async def cmd_rencana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id       = update.effective_chat.id
    next_monday   = get_next_monday()
    plan          = get_weekly_plan(next_monday)
    state         = db.get_weekly_state(chat_id)
    task_statuses = db.get_plan_task_statuses(chat_id)

    adj_raw = state.get("adjusted_tasks")
    if adj_raw:
        plan["tasks"] = json.loads(adj_raw)

    msg = _build_plan_message(
        plan, task_statuses,
        adjusted=bool(adj_raw),
        budget=state.get("budget"),
        situasi=state.get("situasi", ""),
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_selesai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: `/selesai 3`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        n = int(context.args[0])
        db.set_plan_task_status(update.effective_chat.id, n, "selesai")
        await update.message.reply_text(f"✅ Tugas rencana *{n}* ditandai selesai!", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Format: `/selesai 3`", parse_mode=ParseMode.MARKDOWN)


async def cmd_lewati(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: `/lewati 2`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        n = int(context.args[0])
        db.set_plan_task_status(update.effective_chat.id, n, "lewati")
        await update.message.reply_text(f"⏭️ Tugas rencana *{n}* di-skip.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Format: `/lewati 2`", parse_mode=ParseMode.MARKDOWN)


async def cmd_cek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id       = update.effective_chat.id
    next_monday   = get_next_monday()
    plan          = get_weekly_plan(next_monday)
    state         = db.get_weekly_state(chat_id)
    adj_raw       = state.get("adjusted_tasks")
    if adj_raw:
        plan["tasks"] = json.loads(adj_raw)
    task_statuses = db.get_plan_task_statuses(chat_id)
    await update.message.reply_text(
        _build_progress_message(plan, task_statuses), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        state  = db.get_weekly_state(chat_id)
        budget = state.get("budget")
        if budget:
            await update.message.reply_text(
                f"💰 Budget minggu ini: *{_format_rp(budget)}*", parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "💰 Budget belum di-set.\nContoh: `/budget 500000` atau `/budget 500rb`",
                parse_mode=ParseMode.MARKDOWN,
            )
        return
    raw = "".join(context.args)
    amount = _parse_budget_from_text(raw)
    if not amount:
        await update.message.reply_text("❌ Format: `/budget 500000`", parse_mode=ParseMode.MARKDOWN)
        return
    db.set_weekly_budget(chat_id, amount)
    await update.message.reply_text(
        f"✅ Budget di-set: *{_format_rp(amount)}*\n\n"
        "Kirim `/situasi [kondisi]` untuk menyesuaikan rencana.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_situasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        state   = db.get_weekly_state(chat_id)
        situasi = state.get("situasi", "")
        if situasi:
            await update.message.reply_text(
                f"📝 Situasi saat ini:\n_{situasi}_\n\nUpdate: `/situasi [keterangan baru]`",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                "📝 Belum ada catatan situasi.\nContoh: `/situasi Bayu sakit, hujan terus, budget 200rb`",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    situasi_text = " ".join(context.args)
    db.set_weekly_situasi(chat_id, situasi_text)

    # Also update budget if mentioned in text
    m = re.search(r"budget\s+([\d\.,]+(?:rb|jt|ribu|juta)?)", situasi_text, re.IGNORECASE)
    if m:
        amt = _parse_budget_from_text(m.group(1))
        if amt:
            db.set_weekly_budget(chat_id, amt)

    await update.message.reply_text("⏳ Menyesuaikan rencana...", parse_mode=ParseMode.MARKDOWN)

    state  = db.get_weekly_state(chat_id)
    plan   = get_weekly_plan(get_next_monday())
    result = await _adjust_plan_with_claude(
        tasks=plan.get("tasks", []),
        situasi=situasi_text,
        budget=state.get("budget"),
        fokus=plan.get("fokus", ""),
        musim=plan.get("musim", ""),
    )

    if result.get("adjusted_tasks"):
        db.set_adjusted_tasks(chat_id, json.dumps(result["adjusted_tasks"]))
        plan["tasks"] = result["adjusted_tasks"]

    if result.get("penjelasan"):
        await update.message.reply_text(
            f"🤖 *Analisis:* {result['penjelasan']}", parse_mode=ParseMode.MARKDOWN
        )
    if result.get("prioritas_utama"):
        await update.message.reply_text(
            f"🚨 *Prioritas utama:* {result['prioritas_utama']}", parse_mode=ParseMode.MARKDOWN
        )

    msg = _build_plan_message(
        plan, db.get_plan_task_statuses(chat_id),
        adjusted=True, budget=state.get("budget"), situasi=situasi_text,
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[
        InlineKeyboardButton("✅ Ya, reset", callback_data="confirm_reset"),
        InlineKeyboardButton("❌ Batal",     callback_data="cancel_reset"),
    ]]
    await update.message.reply_text(
        "⚠️ Reset state rencana minggu ini (budget, situasi, status tugas, AI adjustment)?",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Finance ─────────────────────────────────────────────────────────────────────

async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = db.get_finance_entries(update.effective_chat.id, days=30)
    if not entries:
        await update.message.reply_text(
            "💸 Belum ada catatan keuangan.\n"
            "Kirim pesan dengan nominal (contoh: 'beli pupuk Rp 150.000') dan saya catat otomatis."
        )
        return
    total_exp = sum(e["amount"] for e in entries if e["type"] == "expense")
    total_inc = sum(e["amount"] for e in entries if e["type"] == "income")
    net       = total_inc - total_exp
    text  = "💰 *Keuangan 30 Hari Terakhir*\n\n"
    text += f"📈 Pemasukan  : {_format_rp(int(total_inc))}\n"
    text += f"📉 Pengeluaran: {_format_rp(int(total_exp))}\n"
    text += f"💵 Net        : {_format_rp(int(net))}\n\n"
    text += "*10 Transaksi Terakhir:*\n"
    for e in entries[-10:]:
        icon    = "📈" if e["type"] == "income" else "📉"
        ds      = datetime.fromisoformat(e["created_at"]).strftime("%d/%m")
        text   += f"{icon} [{ds}] {_format_rp(int(e['amount']))} — {e['description']}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Membuat rekap, mohon tunggu...")
    chat_id       = update.effective_chat.id
    entries       = db.get_finance_entries(chat_id, days=7)
    tasks_done    = db.get_completed_tasks(chat_id, days=7)
    tasks_pending = db.get_all_pending_tasks(chat_id)
    messages      = db.get_messages(chat_id, days=7)
    state         = db.get_weekly_state(chat_id)
    plan_statuses = db.get_plan_task_statuses(chat_id)
    plan          = get_weekly_plan(get_next_monday())
    adj_raw       = state.get("adjusted_tasks")
    if adj_raw:
        plan["tasks"] = json.loads(adj_raw)
    plan_tasks  = plan.get("tasks", [])
    plan_done   = sum(1 for v in plan_statuses.values() if v == "selesai")
    plan_skip   = sum(1 for v in plan_statuses.values() if v == "lewati")

    total_exp = sum(e["amount"] for e in entries if e["type"] == "expense")
    total_inc = sum(e["amount"] for e in entries if e["type"] == "income")

    api_key = _get_api_key()
    if api_key:
        finance_lines = "\n".join(f"- {_format_rp(int(e['amount']))} ({e['type']}): {e['description']}" for e in entries) or "Tidak ada."
        done_lines    = "\n".join(f"- #{t['id']} @{t['assignee']}: {t['description']}" for t in tasks_done) or "Tidak ada."
        pending_lines = "\n".join(f"- #{t['id']} @{t['assignee']}: {t['description']}" for t in tasks_pending) or "Tidak ada."
        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp   = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Rekap mingguan kebun alpukat Kebun Aira, Turi, Sleman.\n\n"
                        f"RENCANA KEBUN: {plan_done}/{len(plan_tasks)} selesai, {plan_skip} dilewati\n"
                        f"Budget: {_format_rp(state['budget']) if state.get('budget') else 'tidak di-set'}\n"
                        f"Situasi: {state.get('situasi','tidak ada catatan')}\n\n"
                        f"TASK KARYAWAN SELESAI:\n{done_lines}\n\nTASK PENDING:\n{pending_lines}\n\n"
                        f"KEUANGAN:\nPemasukan: {_format_rp(int(total_inc))}\nPengeluaran: {_format_rp(int(total_exp))}\n"
                        f"Net: {_format_rp(int(total_inc - total_exp))}\nDetail:\n{finance_lines}\n\n"
                        f"TOTAL PESAN GRUP: {len(messages)}\n\n"
                        f"Format: pakai emoji, max 25 baris, akhiri dengan saran prioritas minggu depan."
                    ),
                }],
            )
            ai_text = resp.content[0].text
        except Exception as e:
            ai_text = f"❌ AI gagal: {e}"
    else:
        net = total_inc - total_exp
        ai_text = (
            f"📋 Rencana kebun: {plan_done}/{len(plan_tasks)} selesai, {plan_skip} dilewati\n"
            f"👷 Task karyawan: {len(tasks_done)} selesai, {len(tasks_pending)} pending\n"
            f"💰 Keuangan: {_format_rp(int(total_inc))} masuk, {_format_rp(int(total_exp))} keluar\n"
            f"   Net: {_format_rp(int(abs(net)))} ({'untung' if net >= 0 else 'rugi'})"
        )

    now = datetime.now().strftime("%d %B %Y")
    await update.message.reply_text(
        f"📊 *REKAP MINGGUAN — {now}*\n\n{ai_text}", parse_mode=ParseMode.MARKDOWN
    )


# ── Help ───────────────────────────────────────────────────────────────────────

async def cmd_bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌿 *Kebun Aira Bot — Semua Perintah*\n\n"
        "*📋 Rencana Kebun (dari Excel):*\n"
        "`/rencana` — rencana minggu depan\n"
        "`/cek` — progress tugas minggu ini\n"
        "`/selesai [n]` — tandai tugas ke-n selesai\n"
        "`/lewati [n]` — skip tugas ke-n\n\n"
        "*💰 Budget & Situasi:*\n"
        "`/budget [jumlah]` — set budget minggu ini\n"
        "`/situasi [keterangan]` — update kondisi, bot sesuaikan rencana via AI\n"
        "  ✦ Contoh: `/situasi Bayu sakit, hujan terus, budget 200rb`\n\n"
        "*💼 Task Karyawan:*\n"
        "`/task @nama deskripsi` — assign task\n"
        "`/tasks` — lihat semua task pending\n"
        "`/done [id]` — tandai selesai\n"
        "`/update [id] pesan` — kirim progress\n"
        "`/taskdetail [id]` — riwayat lengkap\n\n"
        "*💸 Keuangan:*\n"
        "`/finance` — rekap keuangan 30 hari\n"
        "Kirim pesan bebas dengan nominal → dicatat otomatis\n\n"
        "*📊 Rekap:*\n"
        "`/recap` — rekap AI mingguan (on-demand)\n\n"
        "*🔄 Lainnya:*\n"
        "`/reset` — reset state rencana minggu ini\n"
        "`/bantuan` — halaman ini\n\n"
        "━━━━━━━━━━━━━━━\n"
        "🕕 *Otomatis:*\n"
        "• Jumat 18:00 WIB → rencana minggu depan\n"
        "• Minggu 08:00 WIB → rekap AI mingguan\n"
        "• Setiap 6 jam → reminder task overdue",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("done_"):
        task_id = int(query.data.split("_")[1])
        task    = db.complete_task(task_id)
        if task:
            await query.edit_message_text(f"🎉 Task #{task_id} selesai! Kerja bagus, @{task['assignee']}! 💪")
        else:
            await query.edit_message_text("❌ Task tidak ditemukan.")
    elif query.data == "confirm_reset":
        db.reset_weekly_state(query.message.chat_id)
        await query.edit_message_text("✅ State rencana minggu ini di-reset.")
    elif query.data == "cancel_reset":
        await query.edit_message_text("❌ Reset dibatalkan.")


# ── Free-text: finance + situasi ───────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text     = update.message.text.strip()
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name

    db.store_message(chat_id, username, text, update.message.date)

    tl = text.lower()

    # ── Finance auto-detection ──────────────────────────────────────────────
    if any(kw in tl for kw in FINANCE_KEYWORDS):
        api_key = _get_api_key()
        if api_key:
            try:
                client = anthropic.Anthropic(api_key=api_key)
                resp   = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=150,
                    messages=[{
                        "role": "user",
                        "content": (
                            f'Ekstrak info keuangan dari pesan ini. Balas HANYA JSON valid, tanpa teks lain.\n'
                            f'Pesan: "{text}"\n\n'
                            f'Format: {{"found": true, "amount": 150000, "type": "expense", "description": "label singkat"}}\n'
                            f'Jika tidak ada transaksi jelas: {{"found": false}}'
                        ),
                    }],
                )
                raw = resp.content[0].text.strip()
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                result = json.loads(raw.strip())
                if result.get("found") and result.get("amount", 0) > 0:
                    db.add_finance_entry(
                        chat_id, float(result["amount"]),
                        result.get("type", "expense"),
                        result.get("description", "Transaksi"),
                        username,
                    )
                    await update.message.reply_text(
                        f"💾 _Tercatat: {_format_rp(int(result['amount']))} ({result.get('type','expense')}) — {result.get('description','')}_ ",
                        parse_mode=ParseMode.MARKDOWN,
                    )
            except Exception as e:
                logger.warning(f"Finance extraction gagal: {e}")

    # ── Situasi detection → AI adjust plan ─────────────────────────────────
    if any(kw in tl for kw in SITUASI_KEYWORDS):
        db.set_weekly_situasi(chat_id, text)

        # Check for budget in text too
        bm = re.search(r"budget\s+([\d\.,]+(?:rb|jt|ribu|juta)?)", tl)
        if bm:
            amt = _parse_budget_from_text(bm.group(1))
            if amt:
                db.set_weekly_budget(chat_id, amt)

        state  = db.get_weekly_state(chat_id)
        plan   = get_weekly_plan(get_next_monday())
        result = await _adjust_plan_with_claude(
            tasks=plan.get("tasks", []),
            situasi=text,
            budget=state.get("budget"),
            fokus=plan.get("fokus", ""),
            musim=plan.get("musim", ""),
        )
        if result.get("adjusted_tasks"):
            db.set_adjusted_tasks(chat_id, json.dumps(result["adjusted_tasks"]))
            plan["tasks"] = result["adjusted_tasks"]

        if result.get("penjelasan"):
            await update.message.reply_text(
                f"🤖 _{result['penjelasan']}_", parse_mode=ParseMode.MARKDOWN
            )
        msg = _build_plan_message(
            plan, db.get_plan_task_statuses(chat_id),
            adjusted=True, budget=state.get("budget"), situasi=text,
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    token = _get_token()
    app   = Application.builder().token(token).build()

    # ── Register all handlers ──────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       cmd_start))
    # Task management
    app.add_handler(CommandHandler("task",        cmd_task))
    app.add_handler(CommandHandler("tasks",       cmd_tasks))
    app.add_handler(CommandHandler("done",        cmd_done))
    app.add_handler(CommandHandler("update",      cmd_update))
    app.add_handler(CommandHandler("taskdetail",  cmd_taskdetail))
    # Farm plan
    app.add_handler(CommandHandler("rencana",     cmd_rencana))
    app.add_handler(CommandHandler("selesai",     cmd_selesai))
    app.add_handler(CommandHandler("lewati",      cmd_lewati))
    app.add_handler(CommandHandler("cek",         cmd_cek))
    # Budget & situasi
    app.add_handler(CommandHandler("budget",      cmd_budget))
    app.add_handler(CommandHandler("situasi",     cmd_situasi))
    # Finance & recap
    app.add_handler(CommandHandler("finance",     cmd_finance))
    app.add_handler(CommandHandler("recap",       cmd_recap))
    # Utils
    app.add_handler(CommandHandler("reset",       cmd_reset))
    app.add_handler(CommandHandler("bantuan",     cmd_bantuan))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    setup_scheduler(app)

    logger.info("🌿 Kebun Aira Bot berjalan — semua fitur aktif!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
