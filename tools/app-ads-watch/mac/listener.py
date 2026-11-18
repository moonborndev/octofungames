#!/usr/bin/env python3
"""Slack Socket Mode listener for app-ads-watch.

Answers only app_mention events in the alert channel. Two commands:
  "tekrar dene" / "retry"             start run_panels.py for the login-needed networks, replying in the thread
  "onayla" / "approve" [networks]     add the missing lines (one commit per network), push, wait for the live site
Anything else gets a fixed help reply. The mention text is only parsed; it never reaches Claude or a shell.

Runs from the venv in ~/.config/app-ads-watch/venv (slack_sdk). Never logs token values.
"""

import datetime
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_DIR = os.path.normpath(os.path.join(TOOL_DIR, "..", ".."))
SOURCES_PATH = os.path.join(TOOL_DIR, "sources.json")
RUN_PANELS_PATH = os.path.join(HERE, "run_panels.py")

STATE_DIR = os.path.expanduser("~/.config/app-ads-watch")
LOG_DIR = os.path.join(STATE_DIR, "logs")
LOCK_PATH = os.path.join(STATE_DIR, "run.lock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
SLACK_ENV_PATH = os.path.join(STATE_DIR, "slack.env")

def _channel_id_from_env(path):
    """The Slack channel id lives in slack.env (SLACK_CHANNEL_ID), not in this public repo."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "SLACK_CHANNEL_ID":
                    return value.strip()
    except OSError:
        pass
    return ""


CHANNEL_ID = _channel_id_from_env(SLACK_ENV_PATH)
LIVE_URL = "https://octofungames.com/app-ads.txt"
LIVE_POLL_SECONDS = 10 * 60
LIVE_POLL_INTERVAL_SECONDS = 30
SYSTEM_PYTHON = "/usr/bin/python3"
CO_AUTHOR = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
HELP_TEXT = "Yalnızca iki komut anlıyorum: tekrar dene, onayla [ağ]."
GENERIC_NAME_WORDS = {"ads", "network", "audience", "exchange", "monetize", "max", "google"}

log = logging.getLogger("app-ads-listener")


# ---------- parsing ----------

def normalize(text):
    text = re.sub(r"<[^>]*>", " ", text or "")  # mentions, links, channel refs
    text = text.replace("İ", "i").replace("I", "i").lower()
    text = re.sub(r"[^a-z0-9çğıöşü\s-]", " ", text)
    return text.split()


def parse_command(text):
    """Return ("retry", []), ("approve", [names...]) or ("help", [])."""
    words = normalize(text)
    if words[:2] == ["tekrar", "dene"] or words[:1] == ["retry"]:
        return "retry", []
    if words[:1] in (["onayla"], ["approve"]):
        return "approve", words[1:]
    return "help", []


def load_sources(path=SOURCES_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_networks(names, config):
    """Map user words to network keys. Returns (keys, unknown words)."""
    keys, unknown = [], []
    for name in names:
        match = None
        for net in config["networks"]:
            words = {w for w in re.split(r"[^a-z0-9]+", net["name"].lower()) if w} - GENERIC_NAME_WORDS
            if name == net["key"] or name in words:
                match = net["key"]
                break
        if match is None:
            unknown.append(name)
        elif match not in keys:
            keys.append(match)
    return keys, unknown


# ---------- lock ----------

def try_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def release_lock(handle):
    fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()


def child_env():
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    for key in list(env):
        if key.startswith("SLACK_"):
            env.pop(key)
    if "/opt/homebrew/bin" not in env.get("PATH", "").split(":"):
        env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "/usr/bin:/bin")
    return env


# ---------- commands ----------

def handle_retry(event, reply, start_process=None):
    thread_ts = event.get("thread_ts") or event["ts"]
    config = load_sources()
    snapshot_keys = [n["key"] for n in config["networks"] if n["type"] == "snapshot"]
    networks = snapshot_keys
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        login = [k for k in state.get("login_needed", []) if k in snapshot_keys]
        if login:
            networks = login
    except (OSError, ValueError) as exc:
        log.warning("retry: no usable state.json (%s); reading every snapshot network", type(exc).__name__)
    lock = try_lock()
    if lock is None:
        log.info("retry: lock held, not starting")
        reply(thread_ts, "Zaten çalışıyor, bitince sonucu yazacak.")
        return
    release_lock(lock)
    cmd = [SYSTEM_PYTHON, "-I", RUN_PANELS_PATH, "--networks", ",".join(networks), "--thread-ts", thread_ts]
    log.info("retry: starting run_panels for %s in thread %s (asked by %s)", ",".join(networks), thread_ts,
             event.get("user"))
    if start_process is None:
        out = open(os.path.join(LOG_DIR, "run-panels-from-listener.log"), "a")
        subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=child_env(),
                         cwd=HERE, start_new_session=True)
        out.close()
    else:
        start_process(cmd)
    reply(thread_ts, "Başladım, okunacak ağlar: {}. Bitince bu thread'e yazacağım.".format(", ".join(networks)))


def git(repo, *args, input_text=None):
    result = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, input=input_text,
                            env=child_env(), timeout=180)
    log.info("git %s -> exit %s", " ".join(args[:2]), result.returncode)
    return result


def watch_path(repo):
    return os.path.join(repo, "tools", "app-ads-watch", "watch.py")


def current_to_add(repo, app_ads_path):
    """Run watch.py report --no-live --json and return {key: [lines]} plus the network names."""
    with tempfile.TemporaryDirectory() as tmp:
        out_json = os.path.join(tmp, "report.json")
        cmd = [sys.executable, "-I", watch_path(repo), "report", "--no-live", "--out", os.path.join(tmp, "report.md"),
               "--json", out_json, "--file", app_ads_path]
        result = subprocess.run(cmd, capture_output=True, text=True, env=child_env(), timeout=300)
        if result.returncode != 0:
            raise RuntimeError("watch.py report exit {}: {}".format(result.returncode, result.stderr.strip()[-300:]))
        with open(out_json, encoding="utf-8") as f:
            data = json.load(f)
    return ({k: n["to_add"] for k, n in data["networks"].items() if n["to_add"]},
            {k: n["name"] for k, n in data["networks"].items()})


def added_lines(repo):
    """Lines added to app-ads.txt in the working tree. Raises if anything was removed."""
    diff = git(repo, "diff", "-U0", "--", "app-ads.txt").stdout
    added, removed = [], []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    if removed:
        raise RuntimeError("apply removed or changed {} line(s)".format(len(removed)))
    return added


def handle_approve(event, names, reply, repo=REPO_DIR, push=True, poll=True):
    """Add the approved lines. Returns {"commits": [(key, hash, lines)], "pushed": bool} for tests."""
    thread_ts = event.get("thread_ts") or event["ts"]
    user = event.get("user", "unknown")
    outcome = {"commits": [], "pushed": False}
    config = load_sources()
    keys, unknown = resolve_networks(names, config)
    if unknown:
        reply(thread_ts, "Tanımadığım ağ: {}. Hiçbir şey eklemedim.".format(", ".join(unknown)))
        return outcome
    lock = try_lock()
    if lock is None:
        reply(thread_ts, "Şu an bir okuma çalışıyor, bitince tekrar onayla.")
        return outcome
    try:
        app_ads = os.path.join(repo, "app-ads.txt")
        pull = git(repo, "pull", "--ff-only")
        if pull.returncode != 0:
            reply(thread_ts, "git pull başarısız, hiçbir şey eklemedim: ```{}```".format(
                (pull.stderr or pull.stdout).strip()[-500:]))
            return outcome
        if git(repo, "status", "--porcelain", "--", "app-ads.txt").stdout.strip():
            reply(thread_ts, "app-ads.txt'de commit edilmemiş değişiklik var, hiçbir şey eklemedim.")
            return outcome
        to_add, net_names = current_to_add(repo, app_ads)
        selected = [k for k in (keys or list(to_add)) if k in to_add]
        if not selected:
            reply(thread_ts, "Eklenecek satır yok{}.".format(" ({})".format(", ".join(keys)) if keys else ""))
            return outcome
        log.info("approve: user %s, ts %s, networks %s", user, event.get("ts"), ",".join(selected))
        for key in selected:
            result = subprocess.run([sys.executable, "-I", watch_path(repo), "apply", "--networks", key, "--file", app_ads],
                                    capture_output=True, text=True, env=child_env(), timeout=300)
            if result.returncode != 0:
                raise RuntimeError("watch.py apply {} exit {}: {}".format(key, result.returncode,
                                                                          result.stderr.strip()[-300:]))
            try:
                lines = added_lines(repo)
            except RuntimeError:
                git(repo, "checkout", "--", "app-ads.txt")
                raise
            if not lines:
                log.info("approve: %s had nothing new after earlier commits", key)
                continue
            message = "Add {} lines to app-ads.txt\n\nApproved in Slack by {} (ts {}).\n\n{}\n".format(
                net_names.get(key, key), user, event.get("ts"), CO_AUTHOR)
            commit = git(repo, "commit", "-q", "-F", "-", "--", "app-ads.txt", input_text=message)
            if commit.returncode != 0:
                git(repo, "checkout", "--", "app-ads.txt")
                raise RuntimeError("git commit failed: {}".format(commit.stderr.strip()[-300:]))
            sha = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
            outcome["commits"].append((key, sha, lines))
            log.info("approve: committed %s (%s lines) as %s", key, len(lines), sha)
        if not outcome["commits"]:
            reply(thread_ts, "Eklenecek satır yok.")
            return outcome
        summary = "\n".join("• {} `{}`: {} satır\n```{}```".format(net_names.get(k, k), sha, len(lines),
                                                                   "\n".join(lines))
                            for k, sha, lines in outcome["commits"])
        if push:
            pushed = git(repo, "push")
            if pushed.returncode != 0:
                reply(thread_ts, "Commit'ler hazır ama push başarısız, yerelde bıraktım:\n{}\n```{}```".format(
                    summary, (pushed.stderr or pushed.stdout).strip()[-500:]))
                return outcome
            outcome["pushed"] = True
        reply(thread_ts, "Ekledim{}:\n{}".format(" ve push ettim" if outcome["pushed"] else "", summary))
        if poll and outcome["pushed"]:
            all_lines = [l for _, _, lines in outcome["commits"] for l in lines]
            threading.Thread(target=poll_live, args=(all_lines, thread_ts, reply), daemon=True).start()
        return outcome
    except Exception as exc:
        log.error("approve failed: %s", exc)
        reply(thread_ts, "Onay işlenemedi: {}".format(str(exc)[:500]))
        return outcome
    finally:
        release_lock(lock)


def poll_live(lines, thread_ts, reply):
    wanted = {l.strip().lower() for l in lines}
    deadline = time.time() + LIVE_POLL_SECONDS
    while time.time() < deadline:
        time.sleep(LIVE_POLL_INTERVAL_SECONDS)
        url = "{}?t={}".format(LIVE_URL, int(time.time()))
        try:
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "app-ads-watch/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                live = {l.strip().lower() for l in resp.read().decode("utf-8", "replace").splitlines()}
        except Exception as exc:
            log.warning("poll: fetch failed: %s", exc)
            continue
        if wanted <= live:
            log.info("poll: %s new lines are live", len(wanted))
            reply(thread_ts, "Canlıda: {} yeni satır {} adresinde görünüyor.".format(len(wanted), LIVE_URL))
            return
    log.warning("poll: lines not live after %s s", LIVE_POLL_SECONDS)
    reply(thread_ts, "{} dakika geçti, yeni satırlar {} adresinde hâlâ görünmüyor.".format(
        LIVE_POLL_SECONDS // 60, LIVE_URL))


# ---------- events ----------

def handle_event(event, reply, start_process=None, repo=REPO_DIR, push=True, poll=True):
    """Dispatch one Events API event. Returns the command name or None when ignored."""
    if event.get("type") != "app_mention":
        return None
    if event.get("channel") != CHANNEL_ID:
        log.info("ignored mention in channel %s", event.get("channel"))
        return None
    if event.get("bot_id") or event.get("subtype") or event.get("edited"):
        return None
    command, names = parse_command(event.get("text", ""))
    log.info("mention from %s ts %s -> %s %s", event.get("user"), event.get("ts"), command, " ".join(names))
    if command == "retry":
        handle_retry(event, reply, start_process=start_process)
    elif command == "approve":
        handle_approve(event, names, reply, repo=repo, push=push, poll=poll)
    else:
        reply(event.get("thread_ts") or event["ts"], HELP_TEXT)
    return command


def read_slack_env():
    values = {}
    with open(SLACK_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main():
    from slack_sdk.socket_mode.builtin import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient

    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=os.path.join(LOG_DIR, "listener.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    secrets = read_slack_env()
    if not CHANNEL_ID:
        log.error("SLACK_CHANNEL_ID missing in %s; listener stops", SLACK_ENV_PATH)
        return 1
    web = WebClient(token=secrets["SLACK_BOT_TOKEN"])
    client = SocketModeClient(app_token=secrets["SLACK_APP_TOKEN"], web_client=web)
    seen = set()
    seen_lock = threading.Lock()

    def reply(thread_ts, text):
        try:
            web.chat_postMessage(channel=CHANNEL_ID, thread_ts=thread_ts, text=text, unfurl_links=False,
                                 unfurl_media=False)
        except Exception as exc:
            log.error("reply failed: %s", type(exc).__name__)

    def process(sm_client, req):
        sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event_id = req.payload.get("event_id")
        with seen_lock:
            if event_id in seen:
                return
            seen.add(event_id)
        event = req.payload.get("event") or {}
        threading.Thread(target=_safe_handle, args=(event, reply), daemon=True).start()

    client.socket_mode_request_listeners.append(process)
    log.info("listener starting at %s", datetime.datetime.now().isoformat())
    client.connect()
    threading.Event().wait()


def _safe_handle(event, reply):
    try:
        handle_event(event, reply)
    except Exception as exc:
        log.error("handler crashed: %s", exc)


if __name__ == "__main__":
    main()
