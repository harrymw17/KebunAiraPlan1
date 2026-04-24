"""
setup.py — Setup wizard untuk KebunAiraBot (local / Railway)
"""
import json, os, sys

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def main():
    os.system("clear" if os.name != "nt" else "cls")
    print("=" * 56)
    print("   🌿  KEBUN AIRA BOT — SETUP WIZARD  🌿")
    print("=" * 56)
    print()
    print("Setup ini untuk menjalankan bot di KOMPUTER LOKAL.")
    print("Untuk Railway (cloud), cukup set env vars di dashboard.")
    print()

    # Token
    print("─" * 56)
    print("STEP 1: TELEGRAM BOT TOKEN")
    print("─" * 56)
    print("1. Buka Telegram → cari @BotFather")
    print("2. Kirim: /newbot  → ikuti instruksi")
    print("3. Copy token yang diberikan (format: 123456:ABC-xxx)")
    print()
    while True:
        token = input("Token: ").strip()
        if ":" in token and len(token) > 20:
            break
        print("❌ Token tidak valid, coba lagi.")

    # API Key
    print()
    print("─" * 56)
    print("STEP 2: ANTHROPIC API KEY (untuk fitur AI)")
    print("─" * 56)
    print("Dapat dari: https://console.anthropic.com → API Keys")
    print("Format: sk-ant-api03-xxx...")
    print("(Kosongkan jika tidak punya — fitur AI tetap jalan terbatas)")
    print()
    api_key = input("Anthropic API Key: ").strip()

    config = {"telegram_token": token, "anthropic_api_key": api_key}
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    print()
    print("─" * 56)
    print("✅ SETUP SELESAI!")
    print("─" * 56)
    print()
    print("Langkah selanjutnya:")
    print("1. Double-click 'Jalankan Bot.command' (Mac)")
    print("   atau 'Jalankan Bot.bat' (Windows)")
    print("2. Buka bot di Telegram → kirim /start")
    print("3. Bot aktif! Pengingat otomatis Jumat 18:00 WIB")
    print()

if __name__ == "__main__":
    main()
