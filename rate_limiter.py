# -*- coding: utf-8 -*-
"""
rate_limiter.py
----------------
سیستم محدودسازی نرخ ارسال (Rate Limiting) برای پنل روبیکا.

طبق درخواست شما، این پنل باید محتاط‌تر از پنل تلگرام عمل کند، چون:
  ۱) کتابخانهٔ روبیکا (rubpy) غیررسمی است و ریسک محدودشدن حساب را دارد.
  ۲) قبل از ارسال به هر شماره، ابتدا باید آن شماره به مخاطبین اضافه شود
     (add_address_book) که خودش یک عملیات حساس و قابل‌ردیابی توسط روبیکاست.

به همین دلیل، علاوه بر «حداقل فاصلهٔ زمانی بین ارسال‌ها»، سه سقفِ اضافیِ
پنجرهٔ لغزان (Sliding Window) هم اعمال می‌شود:
  - حداکثر پیام در هر دقیقه
  - حداکثر پیام در هر ساعت
  - حداکثر «مخاطب جدید اضافه‌شده» در هر روز

این محدودیت‌ها روی دیسک ذخیره می‌شوند تا حتی بعد از ری‌استارت سرور هم حفظ شوند.
"""

import json
import time
from collections import deque
from pathlib import Path


class RateLimiter:
    def __init__(
        self,
        state_file: str,
        max_messages_per_minute: int = 10,
        max_messages_per_hour: int = 40,
        max_new_contacts_per_day: int = 300,
    ):
        self.path = Path(state_file)
        self.max_messages_per_minute = max_messages_per_minute
        self.max_messages_per_hour = max_messages_per_hour
        self.max_new_contacts_per_day = max_new_contacts_per_day

        self.message_times = deque()
        self.contact_times = deque()  # (timestamp, phone)
        self._known_contacts = set()

        self._load()

    def _load(self):
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self.message_times.extend(float(t) for t in data.get("message_times", []))
            self.contact_times.extend(
                (float(t), phone) for t, phone in data.get("contact_times", [])
            )
            self._known_contacts = set(phone for _, phone in self.contact_times)
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            pass
        self._prune(time.time())

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "message_times": list(self.message_times),
                    "contact_times": [[t, p] for t, p in self.contact_times],
                },
                f,
                indent=2,
            )

    def _prune(self, now: float):
        while self.message_times and now - self.message_times[0] >= 3600:
            self.message_times.popleft()
        while self.contact_times and now - self.contact_times[0][0] >= 86400:
            self.contact_times.popleft()
        self._known_contacts = set(phone for _, phone in self.contact_times)

    def status(self):
        now = time.time()
        self._prune(now)
        minute_count = sum(1 for t in self.message_times if now - t < 60)
        return {
            "messages_last_minute": minute_count,
            "messages_last_hour": len(self.message_times),
            "max_messages_per_minute": self.max_messages_per_minute,
            "max_messages_per_hour": self.max_messages_per_hour,
            "new_contacts_last_24h": len(self.contact_times),
            "max_new_contacts_per_day": self.max_new_contacts_per_day,
        }

    def wait_seconds_for_message_slot(self) -> float:
        """چند ثانیه باید صبر کرد تا نوبت ارسال پیام بعدی برسد (۰ یعنی همین الان)."""
        now = time.time()
        self._prune(now)
        minute_count = sum(1 for t in self.message_times if now - t < 60)
        hour_count = len(self.message_times)

        waits = [0.0]
        if minute_count >= self.max_messages_per_minute:
            oldest_in_window = sorted([t for t in self.message_times if now - t < 60])[0]
            waits.append(60 - (now - oldest_in_window))
        if hour_count >= self.max_messages_per_hour:
            waits.append(3600 - (now - self.message_times[0]))
        return max(0.0, max(waits))

    def record_message_sent(self):
        now = time.time()
        self._prune(now)
        self.message_times.append(now)
        self._save()

    def can_add_new_contact(self, phone: str) -> bool:
        """آیا اضافه‌کردن این شماره به مخاطبین مجاز است؟ (اگر قبلاً اضافه شده، همیشه مجاز است)."""
        now = time.time()
        self._prune(now)
        if phone in self._known_contacts:
            return True
        return len(self.contact_times) < self.max_new_contacts_per_day

    def record_new_contact(self, phone: str):
        if phone in self._known_contacts:
            return
        now = time.time()
        self.contact_times.append((now, phone))
        self._known_contacts.add(phone)
        self._save()
