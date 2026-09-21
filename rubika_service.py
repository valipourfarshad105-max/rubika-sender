# -*- coding: utf-8 -*-
"""
rubika_service.py
-------------------
لایهٔ واسط بین Flask (که sync است) و rubpy (که async است).

از آنجا که Flask به‌صورت پیش‌فرض سینک است ولی کتابخانهٔ rubpy کاملاً async
نوشته شده، یک event loop اختصاصی در یک ترد جداگانه اجرا می‌کنیم و هر
درخواستی که باید با روبیکا صحبت کند را از طریق آن event loop اجرا می‌کنیم.

این ماژول شامل:
  - راه‌اندازی و نگه‌داری یک Client واحد (چون فقط یک اکانت روبیکا داریم)
  - resolve_guid(phone): افزودن شماره به مخاطبین و گرفتن object_guid
  - send_smart(...): همان منطق «متن طولانی هرگز بریده نشود» که در تلگرام داشتیم
"""

import asyncio
import threading
import mimetypes
from pathlib import Path

from rubpy import Client
from rubpy import exceptions as rubpy_exceptions
from rubpy.crypto import Crypto
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from session_utils import FullStringSession

# محدودیت‌های متنی. مقدار کپشن عکس روبیکا به‌طور رسمی مستند نیست،
# پس محافظه‌کارانه همان سقف ۱۰۲۴ کاراکتریِ رایج پیام‌رسان‌ها را در نظر می‌گیریم:
# اگر متن از این مقدار بیشتر باشد، عکس بدون کپشن فرستاده می‌شود و کل متن کامل
# به‌صورت پیام جداگانه به دنبالش می‌رود (هیچ‌وقت هیچ بخشی از متن گم/بریده نمی‌شود).
TEXT_LIMIT = 4000        # کمی پایین‌تر از سقف داخلی ۴۲۰۰ کاراکتریِ خود rubpy، برای اطمینان
CAPTION_LIMIT = 1024


class RubikaServiceError(Exception):
    pass


class NotOnRubika(RubikaServiceError):
    pass


class RubikaService:
    def __init__(self, session_string: str):
        self._session_string = session_string
        self._client: Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._me = None

    # ---------------------------------------------------------------- setup

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=60)
        if not self._ready.is_set():
            raise RubikaServiceError("راه‌اندازی اتصال به روبیکا زمان‌بر شد (timeout).")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())
        self._ready.set()
        self._loop.run_forever()

    async def _connect(self):
        session = FullStringSession(self._session_string)
        self._client = Client(name=session, timeout=25, max_retries=5)
        await self._client.connect()

        # نکتهٔ فنی مهم: متد connect() خودِ کتابخانه فقط auth/guid/private_key را
        # از روی session می‌خواند، ولی «کلید امضا» (import_key) و «رمز رمزنگاری»
        # (key/decode_auth) را نمی‌سازد. بدون این‌ها، هر درخواستی (حتی get_me)
        # با خطای AttributeError روی pkcs1_15.sign متوقف می‌شود. این کار همان کاری
        # است که خودِ rubpy در ابتدای start() هم انجام می‌دهد.
        if self._client.auth is not None:
            self._client.decode_auth = Crypto.decode_auth(self._client.auth)
            self._client.key = Crypto.passphrase(self._client.auth)
        if self._client.private_key is not None:
            self._client.import_key = pkcs1_15.new(RSA.import_key(self._client.private_key.encode()))

        result = await self._client.get_me()
        self._me = result.user

    def account_info(self):
        if self._me is None:
            return {}
        first = getattr(self._me, "first_name", "") or ""
        last = getattr(self._me, "last_name", "") or ""
        return {"name": (first + " " + last).strip()}

    # ------------------------------------------------------------ run/await

    def _run(self, coro, timeout=60):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ---------------------------------------------------------------- guid

    async def _resolve_guid_async(self, phone: str):
        try:
            response = await self._client.add_address_book(
                phone=phone,
                first_name="مخاطب",
                last_name=phone[-4:] if len(phone) >= 4 else "",
            )
        except rubpy_exceptions.InvalidInput as e:
            raise NotOnRubika(f"شماره نامعتبر است یا روبیکا ندارد. (جزئیات فنی: {e})") from e
        except rubpy_exceptions.RequestError as e:
            # سایر خطاهای سمت سرور روبیکا (مثلاً محدودیت نرخ) را هم به‌جای کرش کردن،
            # با متن دقیق‌شان بالا می‌فرستیم تا در گزارش دیده شوند.
            raise RubikaServiceError(f"خطای سرور روبیکا هنگام افزودن مخاطب: {type(e).__name__}: {e}") from e

        user_guid = None
        try:
            chat_update = response.find_keys("chat_update")
            if chat_update is not None:
                user_guid = chat_update.find_keys("object_guid")
        except Exception:
            pass

        if not user_guid:
            try:
                user_guid = response.find_keys("user_guid")
            except Exception:
                pass

        if not user_guid:
            try:
                user_obj = response.find_keys("user")
                if user_obj is not None:
                    user_guid = user_obj.find_keys("user_guid")
            except Exception:
                pass

        if not user_guid:
            try:
                for key in ("guid", "object_guid"):
                    val = response.find_keys(key)
                    if val:
                        user_guid = val
                        break
            except Exception:
                pass

        if not user_guid:
            raise NotOnRubika(
                "این شماره پیدا نشد (احتمالاً روبیکا ندارد یا در تنظیمات حریم خصوصی، افزودن مخاطب را محدود کرده)."
            )

        return user_guid

    def resolve_guid(self, phone: str) -> str:
        return self._run(self._resolve_guid_async(phone))

    # ---------------------------------------------------------------- send

    @staticmethod
    def _split_text(text: str, limit: int):
        if not text:
            return []
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit * 0.5:
                cut = limit
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()
        return chunks

    async def _send_text_async(self, guid: str, text: str):
        for chunk in self._split_text(text, TEXT_LIMIT):
            await self._client.send_message(guid, chunk)

    async def _send_smart_async(self, guid: str, text: str, photo_path: str | None):
        clean_text = text or ""

        if not photo_path:
            await self._send_text_async(guid, clean_text)
            return

        photo_bytes = Path(photo_path).read_bytes()
        # نکتهٔ فنی مهم: وقتی عکس به‌صورت بایت (نه مسیر فایل) داده شود، خودِ کتابخانه
        # برای آپلود به یک "نام فایل" نیاز دارد، وگرنه با خطای
        # "file_name must be specified when uploading from bytes" متوقف می‌شود.
        file_name = Path(photo_path).name

        # نکتهٔ فنی مهم‌تر (باگ واقعی در خودِ کتابخانهٔ rubpy):
        # وقتی Pillow نصب باشد، تابع ساخت "تصویر بندانگشتی" (thumbnail) داخل rubpy
        # به‌اشتباه کل عکس را در همان اندازهٔ اصلی برمی‌گرداند (به‌جای کوچک‌کردنش).
        # این تصویر کامل به‌صورت متن base64 داخل بدنهٔ درخواست جاسازی می‌شود و باعث
        # می‌شود حجم درخواست از سقف مجاز سرورهای روبیکا رد شود (خطای 413 Request
        # Entity Too Large روی همهٔ سرورها، که در ظاهر شبیه «هنگ کردن» برنامه است).
        # چون این کتابخانه دیگر به‌روزرسانی نمی‌شود، این باگ را با غیرفعال‌کردن
        # ساخت thumbnail دور می‌زنیم. نکتهٔ ظریف: باید دقیقاً thumb=None بدهیم
        # نه thumb=False — چون با False، کدِ داخلی کتابخانه به‌اشتباه نوعِ پیام را
        # از «عکس» به «فایلِ ضمیمهٔ عمومی» تغییر می‌دهد و گیرنده دیگر آن را به‌صورت
        # عکس نمی‌بیند. با None، نوعِ پیام «عکس» درست باقی می‌ماند و فقط پیش‌نمایش
        # ریزِ خراب ساخته نمی‌شود؛ خودِ عکسِ اصلی و کامل بدون مشکل ارسال می‌شود.
        if len(clean_text) <= CAPTION_LIMIT:
            await self._client.send_photo(
                guid, photo_bytes, caption=clean_text or None, file_name=file_name, thumb=None
            )
            return

        # عکس بدون کپشن، بعد کل متن به‌صورت پیام(های) جداگانه — هیچ بریدگی‌ای رخ نمی‌دهد
        await self._client.send_photo(guid, photo_bytes, file_name=file_name, thumb=None)
        await self._send_text_async(guid, clean_text)

    def send_smart(self, guid: str, text: str, photo_path: str | None):
        self._run(self._send_smart_async(guid, text, photo_path), timeout=120)

    # ------------------------------------------------------------- shutdown

    async def _disconnect_async(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass

    def stop(self):
        if self._loop:
            try:
                self._run(self._disconnect_async(), timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
