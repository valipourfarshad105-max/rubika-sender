# -*- coding: utf-8 -*-
"""
login.py
--------
اسکریپت ورود به حساب شخصی روبیکا (فقط یک‌بار، روی کامپیوتر خودت اجرا کن).

این اسکریپت شبیه login.js در پروژهٔ تلگرام است: با شماره‌موبایل و کد پیامکی
وارد حساب روبیکای خودت می‌شوی و در پایان یک «رشتهٔ نشست» (SESSION) به تو
می‌دهد که باید آن را داخل فایل .env قرار بدهی.

اجرا:
    python login.py
"""

import asyncio
import sys

from rubpy import Client
from rubpy.crypto import Crypto
from rubpy import exceptions
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from session_utils import FullStringSession


def convert_farsi_digits(text: str) -> str:
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def normalize_phone(phone: str) -> str:
    phone = convert_farsi_digits(phone).strip()
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("00"):
        phone = phone[2:]
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "98" + phone[1:]
    if not phone.startswith("98"):
        # اگر کاربر کد کشور را وارد نکرده و شماره با صفر هم شروع نشده،
        # فرض می‌کنیم قبلاً به فرمت درست وارد کرده.
        pass
    return phone


async def main():
    print("=" * 60)
    print("ورود به حساب شخصی روبیکا")
    print("=" * 60)

    phone_raw = input("شماره موبایل روبیکا (مثلاً 09121234567 یا 989121234567): ").strip()
    phone_number = normalize_phone(phone_raw)
    print(f"شماره نرمال‌شده: {phone_number}")

    session = FullStringSession()
    client = Client(name=session, timeout=20)

    await client.connect()

    # اگر session از قبل auth/private_key داشته باشد (ورود تکراری با همان فایل)،
    # باید decode_auth و import_key هم از روی آن‌ها ساخته شود، وگرنه درخواست‌ها ناموفق می‌مانند.
    if client.auth is not None:
        client.decode_auth = Crypto.decode_auth(client.auth)
    if client.private_key is not None:
        client.import_key = pkcs1_15.new(RSA.import_key(client.private_key.encode()))
        if client.auth is not None:
            client.key = Crypto.passphrase(client.auth)

    try:
        result = await client.get_me()
        print("\nاین session از قبل وارد شده است. نیازی به ورود دوباره نیست.")
        me = result.user
        print(f"نام حساب: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}")
    except exceptions.NotRegistered:
        # مرحلهٔ ارسال کد پیامکی
        result = await client.send_code(phone_number=phone_number, send_type="SMS")

        if getattr(result, "status", None) == "SendPassKey":
            while True:
                pass_key = input(f"رمز دو مرحله‌ای [{result.hint_pass_key}]: ")
                result = await client.send_code(phone_number=phone_number, pass_key=pass_key)
                if result.status == "OK":
                    break

        public_key, private_key = Crypto.create_keys()
        client.private_key = private_key

        while True:
            phone_code = input("کد پیامکی دریافتی: ").strip()
            result = await client.sign_in(
                phone_code=phone_code,
                phone_number=phone_number,
                phone_code_hash=result.phone_code_hash,
                public_key=public_key,
            )

            if result.status == "OK":
                auth = Crypto.decrypt_RSA_OAEP(private_key, result.auth)
                client.auth = auth
                client.decode_auth = Crypto.decode_auth(auth)
                client.key = Crypto.passphrase(auth)
                # این خط جا افتاده بود: بدون آن، درخواست‌های بعدی (مثل ثبت دستگاه)
                # نمی‌توانند امضا شوند و خطای "'NoneType' object has no attribute 'sign'" می‌دهند.
                client.import_key = pkcs1_15.new(RSA.import_key(private_key.encode()))

                client.session.insert(
                    phone_number=result.user.phone,
                    auth=auth,
                    guid=result.user.user_guid,
                    user_agent=client.user_agent,
                    private_key=private_key,
                )

                await client.register_device(device_model="RubikaSenderPanel")
                print("\nورود با موفقیت انجام شد!\n")
                break
            else:
                print("کد اشتباه بود یا منقضی شده، دوباره تلاش کن.")

    # ساخت رشتهٔ SESSION نهایی برای .env
    session_string = FullStringSession.dump(client.session.information())

    print("=" * 60)
    print("SESSION STRING:")
    print(session_string)
    print("=" * 60)
    print("این مقدار را داخل فایل .env مقابل RUBIKA_SESSION= قرار بده.")
    print("⚠️  این مقدار را هرگز با کسی به اشتراک نگذار (حتی داخل چت با هوش مصنوعی)؛")
    print("    هر کسی این رشته را داشته باشد می‌تواند به‌جای تو وارد اکانت روبیکایت شود.")

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nلغو شد.")
        sys.exit(1)
