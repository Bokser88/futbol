import requests
print("Проверка подключения к Telegram...")
try:
    r = requests.get("https://api.telegram.org", timeout=10)
    print("✅ Успех! Статус:", r.status_code)
except Exception as e:
    print("❌ Ошибка:", e)