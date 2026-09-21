# -*- coding: utf-8 -*-
"""
session_utils.py
-----------------
نسخهٔ اصلاح‌شدهٔ StringSession کتابخانهٔ rubpy.

⚠️ نکتهٔ فنی مهم (دلیل وجود این فایل):
کلاس StringSession خودِ کتابخانهٔ rubpy فقط این چهار مقدار را ذخیره می‌کند:
    [phone_number, auth, guid, user_agent]
و «کلید خصوصی RSA» (private_key) را ذخیره نمی‌کند.

اما هنگام اتصال مجدد (connect) به حساب، کتابخانه به همان private_key نیاز دارد
تا بتواند درخواست‌ها را امضا/رمزگشایی کند. اگر private_key ذخیره نشود، بعد از
هر ری‌استارت سرور، اتصال قطع کامل می‌شود و باید دوباره لاگین شوی.

راه‌حل: این فایل یک نسخهٔ اصلاح‌شده به نام FullStringSession می‌سازد که
private_key را هم داخل همان رشتهٔ session ذخیره می‌کند. رشتهٔ خروجی همچنان
یک متن Base64 ساده است که داخل .env با نام SESSION نگه‌داری می‌شود —
دقیقاً مثل همان الگویی که در پنل تلگرام استفاده کردیم.
"""

from rubpy.sessions import StringSession


class FullStringSession(StringSession):
    """
    نسخهٔ کامل StringSession که پنج مقدار را ذخیره می‌کند:
    [phone_number, auth, guid, user_agent, private_key]
    """

    def insert(self, phone_number, auth, guid, user_agent, private_key=None, *args, **kwargs):
        self.session = [phone_number, auth, guid, user_agent, private_key]

    def information(self):
        # اگر یک StringSession قدیمی (بدون private_key) داده شده باشد،
        # با None برای private_key پر می‌کنیم تا خطا ندهد.
        info = self.session
        if info is not None and len(info) == 4:
            info = list(info) + [None]
        return info
