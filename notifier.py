"""
Pluggable notifier for the memecoin monitor.

Channels (set in monitor_config.json -> notification.channel):
  - "log"     : print + append to data/monitor_log.txt  (default, no creds needed)
  - "telegram": Telegram bot. Needs telegram_bot_token + telegram_chat_id
  - "webhook" : generic POST JSON. Works for Discord / Feishu(Lark) / WeCom / Slack.
                webhook_format: "discord"  -> {"content": text}
                                "generic" -> {"text": text, "title": title}

Usage:
  from notifier import Notifier
  n = Notifier(config["notification"])
  n.send("标题", "正文...")
  n.test()   # send a test message to verify creds
"""
import os, sys, json, time
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.channel = (self.cfg.get("channel") or "log").lower()
        # env vars override config file (so Zeabur can inject secrets without committing them)
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or self.cfg.get("telegram_bot_token", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or self.cfg.get("telegram_chat_id", "")
        self.webhook_url = os.environ.get("WEBHOOK_URL") or self.cfg.get("webhook_url", "")
        self.webhook_format = self.cfg.get("webhook_format", "generic")
        self.log_path = os.path.join(DATA, "monitor_log.txt")
        os.makedirs(DATA, exist_ok=True)

    # ---- public API ----
    def send(self, title, message, level="info"):
        text = f"*{title}*\n\n{message}" if self.channel == "telegram" else f"{title}\n\n{message}"
        if self.channel == "telegram":
            return self._send_telegram(text)
        if self.channel == "webhook":
            return self._send_webhook(title, message)
        # default: log
        self._log(f"[{level.upper()}] {title}\n{message}\n{'-'*40}")
        return True

    def test(self):
        ok = self.send("✅ 监控器通知测试",
                       "如果你看到这条消息，说明通知渠道已配置正确。\n"
                       "妖币埋伏监控器将在此推送：进入 / 埋伏 / 见顶 / 崩盘 信号。")
        return ok

    # ---- implementations ----
    def _log(self, s):
        print(s)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + s + "\n")
        except Exception:
            pass

    def _send_telegram(self, text):
        if not self.bot_token or not self.chat_id:
            self._log("[WARN] telegram 未配置 token/chat_id，回退到 log")
            self._log(text)
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # split long messages (Telegram limit 4096)
        chunks = self._split(text, 3800)
        ok = True
        for ch in chunks:
            payload = json.dumps({"chat_id": self.chat_id, "text": ch,
                                  "parse_mode": "Markdown", "disable_web_page_preview": True}).encode()
            try:
                req = urllib.request.Request(url, data=payload,
                                            headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    if r.status != 200:
                        ok = False
            except Exception as e:
                ok = False
                self._log(f"[ERR] telegram send failed: {e}")
        return ok

    def _send_webhook(self, title, message):
        if not self.webhook_url:
            self._log("[WARN] webhook_url 未配置，回退到 log")
            self._log(f"{title}\n{message}")
            return False
        if self.webhook_format == "discord":
            body = {"content": f"**{title}**\n\n{message}"}
        else:
            body = {"title": title, "text": message}
        payload = json.dumps(body).encode()
        try:
            req = urllib.request.Request(self.webhook_url, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status in (200, 204)
        except Exception as e:
            self._log(f"[ERR] webhook send failed: {e}")
            return False

    @staticmethod
    def _split(s, n):
        if len(s) <= n:
            return [s]
        out, cur = [], ""
        for line in s.split("\n"):
            if len(cur) + len(line) + 1 > n:
                out.append(cur)
                cur = line
            else:
                cur = cur + "\n" + line if cur else line
        if cur:
            out.append(cur)
        return out


if __name__ == "__main__":
    # quick self-test with no creds -> falls back to log
    n = Notifier({"channel": "log"})
    n.test()
