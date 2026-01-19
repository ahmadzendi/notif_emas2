import asyncio
import aiohttp
import re
import time
from datetime import datetime
import oandapyV20
import oandapyV20.endpoints.pricing as pricing
from concurrent.futures import ThreadPoolExecutor
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OANDA_ACCESS_TOKEN = os.getenv("OANDA_ACCESS_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENVIRONMENT = "practice"

last_treasury_buy = None
last_treasury_update = None
custom_message = ""
last_update_id = 0

nominals = [
    (20_000_000, 19_314_000),
    (30_000_000, 28_980_000),
    (40_000_000, 38_652_000),
    (50_000_000, 48_325_000),
]

oanda_executor = ThreadPoolExecutor(max_workers=1)

HARI_INDONESIA = {
    'Monday': 'Senin',
    'Tuesday': 'Selasa',
    'Wednesday': 'Rabu',
    'Thursday': 'Kamis',
    'Friday': 'Jumat',
    'Saturday': 'Sabtu',
    'Sunday': 'Minggu'
}


def format_tanggal_indo(updated_at_str):
    try:
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
        clean_str = updated_at_str.split('+')[0].split('Z')[0]
        dt = None
        for fmt in formats:
            try:
                dt = datetime.strptime(clean_str, fmt)
                break
            except:
                continue
        if dt is None:
            return updated_at_str
        hari_eng = dt.strftime('%A')
        hari_indo = HARI_INDONESIA.get(hari_eng, hari_eng)
        waktu = dt.strftime('%H:%M:%S')
        return f"{hari_indo} {waktu}"
    except:
        return updated_at_str


def format_id_number(number, decimal_places=0):
    if number is None:
        return "N/A"
    num_float = float(number)
    num_str_with_default_decimal = f"{num_float:.{decimal_places}f}"
    parts = num_str_with_default_decimal.split('.')
    integer_part_str = parts[0]
    fractional_part_str = parts[1] if len(parts) > 1 else ''
    formatted_integer_part = "{:,.0f}".format(int(integer_part_str)).replace(',', '.')
    if decimal_places > 0:
        return f"{formatted_integer_part},{fractional_part_str}"
    else:
        return formatted_integer_part


def get_status(new, old):
    if old is None:
        return "Baru"
    if new > old:
        return f"Naik 🚀 +{format_id_number(new - old, 0)} rupiah"
    elif new < old:
        return f"Turun 🔻 -{format_id_number(old - new, 0)} rupiah"
    else:
        return "➖ Tetap"


def calc_profit(nominal, modal, buy, sell):
    gram = nominal / buy
    hasil = gram * sell
    selisih = hasil - modal
    warna = "🟢" if selisih >= 0 else "🔴"
    sign = "+" if selisih >= 0 else "-"
    return gram, abs(selisih), warna, sign


async def send_telegram_message_async(session: aiohttp.ClientSession, message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except:
        return False


async def get_telegram_updates(session: aiohttp.ClientSession):
    global last_update_id, custom_message
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 0}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        last_update_id = update["update_id"]
                        message = update.get("message", {})
                        text = message.get("text", "")
                        if text.startswith("/atur "):
                            custom_message = text[6:].strip()
                            chat_id = message.get("chat", {}).get("id")
                            if chat_id:
                                await send_reply(session, chat_id, f"Pesan diatur: {custom_message}")
    except:
        pass


async def send_reply(session: aiohttp.ClientSession, chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            pass
    except:
        pass


async def fetch_treasury_price_async(session: aiohttp.ClientSession):
    url = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://treasury.id"
    }
    try:
        async with session.post(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except:
        return None


async def fetch_usd_idr_async(session: aiohttp.ClientSession):
    url = "https://www.google.com/finance/quote/USD-IDR"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                text = await resp.text()
                match = re.search(r'data-last-price="([0-9.,]+)"', text)
                if match:
                    kurs = match.group(1).replace(",", "")
                    return float(kurs)
            return None
    except:
        return None


def fetch_oanda_xauusd_sync():
    try:
        if not OANDA_ACCESS_TOKEN:
            return None
        api = oandapyV20.API(access_token=OANDA_ACCESS_TOKEN, environment=OANDA_ENVIRONMENT)
        params = {"instruments": "XAU_USD"}
        r = pricing.PricingInfo(accountID=OANDA_ACCOUNT_ID, params=params)
        api.request(r)
        response_data = r.response
        if response_data and 'prices' in response_data and len(response_data['prices']) > 0:
            price_info = response_data['prices'][0]
            bid = float(price_info['bids'][0]['price'])
            ask = float(price_info['asks'][0]['price'])
            return (bid + ask) / 2
        return None
    except:
        return None


async def fetch_oanda_xauusd_async():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(oanda_executor, fetch_oanda_xauusd_sync)


async def main_async():
    global last_treasury_buy, last_treasury_update
    polling_interval = 0.01
    print("Bot berjalan... Menunggu perubahan harga Treasury.")
    connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, keepalive_timeout=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            start = time.perf_counter()
            treasury_data, current_usd_idr, current_xau_usd, _ = await asyncio.gather(
                fetch_treasury_price_async(session),
                fetch_usd_idr_async(session),
                fetch_oanda_xauusd_async(),
                get_telegram_updates(session)
            )
            if treasury_data:
                data = treasury_data.get("data")
                if data and "buying_rate" in data and "selling_rate" in data and "updated_at" in data:
                    try:
                        new_buy = int(data["buying_rate"])
                        new_sell = int(data["selling_rate"])
                        new_update = data["updated_at"]
                        if last_treasury_update is None or new_update > last_treasury_update:
                            status_msg = get_status(new_buy, last_treasury_buy)
                            tanggal_indo = format_tanggal_indo(new_update)
                            msg = (
                                f"<b>Info Harga Treasury</b>\n"
                                f"{status_msg}\n"
                                f"<b>{tanggal_indo}</b>\n\n"
                                f"Harga Beli: <b>Rp {format_id_number(new_buy, 0)}</b> "
                                f"Jual: <b>Rp {format_id_number(new_sell, 0)}</b>\n\n"
                            )
                            for nominal, modal in nominals:
                                gram, selisih_abs, warna, sign = calc_profit(nominal, modal, new_buy, new_sell)
                                jt = nominal // 1_000_000
                                selisih_str = format_id_number(selisih_abs, 0)
                                msg += f"🥇 {jt} JT ➺ {gram:.4f}gr {warna} {sign}<b>Rp {selisih_str}</b>\n"
                            msg += "\n"
                            xau_str = format_id_number(current_xau_usd, 3)
                            usd_str = format_id_number(current_usd_idr, 4)
                            msg += f"Harga XAU : <b>{xau_str}</b> | USD : <b>{usd_str}</b>"
                            if custom_message:
                                msg += f"\n\n<b>{custom_message}</b>"
                            print(f"Mengirim update... {new_update}")
                            asyncio.create_task(send_telegram_message_async(session, msg))
                            last_treasury_buy = new_buy
                            last_treasury_update = new_update
                    except:
                        pass
            elapsed = time.perf_counter() - start
            sleep_time = max(0, polling_interval - elapsed)
            await asyncio.sleep(sleep_time)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nBot dihentikan.")
    finally:
        oanda_executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
