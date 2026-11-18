#!/usr/bin/env python3
"""Weekly (and retry) dashboard read for app-ads-watch, run on the Mac.

Steps: lock, git pull --ff-only, one Claude run that reads the dashboards in Chrome and prints each
snapshot between markers, write the snapshots, pick up the Liftoff download, run watch.py report,
post a short summary to Slack and save state.json for the Slack listener.

Python 3.9+ standard library only (runs with /usr/bin/python3). Never commits, pushes or edits
app-ads.txt. Never prints or logs token values.
"""

import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_DIR = os.path.normpath(os.path.join(TOOL_DIR, "..", ".."))
SNAPSHOT_DIR = os.path.join(TOOL_DIR, "snapshots")
SOURCES_PATH = os.path.join(TOOL_DIR, "sources.json")
PANEL_TASK_PATH = os.path.join(TOOL_DIR, "PANEL_TASK.md")
WATCH_PATH = os.path.join(TOOL_DIR, "watch.py")

STATE_DIR = os.path.expanduser("~/.config/app-ads-watch")
LOG_DIR = os.path.join(STATE_DIR, "logs")
WORK_DIR = os.path.join(STATE_DIR, "work")
LOCK_PATH = os.path.join(STATE_DIR, "run.lock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
SLACK_ENV_PATH = os.path.join(STATE_DIR, "slack.env")
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")

SDK_REGISTRY = os.environ.get("APP_ADS_SDK_REGISTRY", os.path.expanduser(
    "~/Development/Chessdoku/SDK/packages/com.octofun.sdk.core/Editor/SDKProviderRegistry.cs"))
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
CLAUDE_BIN = "claude"
CLAUDE_TIMEOUT_SECONDS = 25 * 60
MAX_LINES_PER_NETWORK = 20
EXIT_LOCKED = 3

# Claude may use only the Chrome tools. Built-in tools that could touch files, the shell or the web
# are denied explicitly as well, in case a settings file allows them.
CLAUDE_ARGS = [
    "-p", "--chrome",
    "--output-format", "text",
    "--permission-mode", "dontAsk",
    "--allowedTools", "mcp__claude-in-chrome__*",
    "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task",
]

SNAPSHOT_BEGIN_RE = re.compile(r"^=== SNAPSHOT ([a-z0-9_-]+) BEGIN ===\s*$")
SNAPSHOT_END_RE = re.compile(r"^=== SNAPSHOT ([a-z0-9_-]+) END ===\s*$")
STATUS_RE = re.compile(r"^=== STATUS ([a-z0-9_-]+) (ok|login_needed|error|download_clicked)(?: (.*))? ===\s*$")
CRAWL_RE = re.compile(r"^=== CRAWL ([a-z0-9_-]+) (.+) ===\s*$")
SOURCE_RE = re.compile(r"^=== SOURCE ([a-z0-9_-]+) (.+) ===\s*$")
# One app-ads record line: domain, id, TYPE[, certId]. Anything else is dropped.
RECORD_RE = re.compile(r"^\s*[A-Za-z0-9.-]+\s*,\s*[^,\s]+\s*,\s*(DIRECT|RESELLER)\s*(,\s*[^,\s]+\s*)?$",
                       re.IGNORECASE)
LIFTOFF_NAME_RE = re.compile(r"(vungle|liftoff|app-?ads)", re.IGNORECASE)


class Run:
    def __init__(self, log_path, no_slack):
        self.log_path = log_path
        self.no_slack = no_slack
        self.errors = []
        self.crawl = []
        self.claude_status = {}

    def log(self, message):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] {}".format(stamp, message)
        print(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------- helpers ----------

LOCAL_URLS_PATH = os.path.join(STATE_DIR, "dashboard_urls.json")


def apply_local_dashboard_urls(config):
    """Account-specific dashboard links (e.g. Unity's organization URL) live in ~/.config, not in this public repo."""
    try:
        with open(LOCAL_URLS_PATH, encoding="utf-8") as f:
            local = json.load(f)
    except FileNotFoundError:
        return config
    for net in config.get("networks", []):
        url = local.get(net.get("key"))
        if url:
            net["dashboard_url"] = url
    return config


def load_sources():
    with open(SOURCES_PATH, encoding="utf-8") as f:
        return json.load(f)


def snapshot_networks(config):
    return [n for n in config["networks"] if n["type"] == "snapshot"]


def clean_env():
    """Environment for child processes: no API key (Max subscription login), Homebrew on PATH."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    for key in list(env):
        if key.startswith("SLACK_"):
            env.pop(key)
    path = env.get("PATH", "")
    if "/opt/homebrew/bin" not in path.split(":"):
        env["PATH"] = "/opt/homebrew/bin:" + path if path else "/opt/homebrew/bin:/usr/bin:/bin"
    return env


def read_slack_env():
    values = {}
    with open(SLACK_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def acquire_lock():
    """Return the open lock file, or None when another run holds it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


# ---------- steps ----------

def git_pull(run, env):
    result = subprocess.run(["git", "-C", REPO_DIR, "pull", "--ff-only"], capture_output=True, text=True,
                            env=env, timeout=120)
    run.log("git pull --ff-only exit {}: {}".format(result.returncode, (result.stdout + result.stderr).strip()))
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or "exit {}".format(result.returncode)
    return None


def build_prompt(networks, today):
    with open(PANEL_TASK_PATH, encoding="utf-8") as f:
        task = f.read()
    lines = [task, "", "## This run", "", "Date: {}".format(today), "", "Networks to read (key, name, dashboard_url, "
             "page_wait_seconds, error_wait_seconds):", ""]
    for net in networks:
        lines.append("- {} | {} | {} | {} | {}".format(net["key"], net["name"], net.get("dashboard_url"),
                                                      net.get("page_wait_seconds", 60),
                                                      net.get("error_wait_seconds", 10)))
    return "\n".join(lines) + "\n"


def run_claude(run, networks, today, env):
    prompt = build_prompt(networks, today)
    os.makedirs(WORK_DIR, exist_ok=True)
    run.log("claude: starting for {} (timeout {} s)".format(",".join(n["key"] for n in networks),
                                                            CLAUDE_TIMEOUT_SECONDS))
    try:
        result = subprocess.run([CLAUDE_BIN] + CLAUDE_ARGS, input=prompt, capture_output=True, text=True,
                                cwd=WORK_DIR, env=env, timeout=CLAUDE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        run.log("claude: timed out after {} s; partial output follows\n{}".format(CLAUDE_TIMEOUT_SECONDS, out))
        run.errors.append("Claude {} dakikada bitmedi".format(CLAUDE_TIMEOUT_SECONDS // 60))
        return out
    except FileNotFoundError:
        run.log("claude: binary not found on PATH")
        run.errors.append("claude bulunamadı (PATH)")
        return ""
    run.log("claude: exit {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(result.returncode, result.stdout,
                                                                           result.stderr))
    if result.returncode != 0:
        run.errors.append("Claude exit {}".format(result.returncode))
    return result.stdout


def parse_claude_output(text, wanted_keys):
    """Return {key: {"lines": [...], "source": str}}, {key: (status, note)}, [(key, crawl text)]."""
    snapshots = {}
    statuses = {}
    crawl = []
    sources = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip("\r")
        begin = SNAPSHOT_BEGIN_RE.match(line.strip())
        end = SNAPSHOT_END_RE.match(line.strip())
        if current is None and begin and begin.group(1) in wanted_keys:
            current = begin.group(1)
            snapshots[current] = []
            continue
        if current is not None:
            if end and end.group(1) == current:
                current = None
            elif RECORD_RE.match(line):
                snapshots[current].append(line.strip())
            continue
        status = STATUS_RE.match(line.strip())
        if status and status.group(1) in wanted_keys:
            statuses[status.group(1)] = (status.group(2), (status.group(3) or "").strip()[:200])
            continue
        crawl_match = CRAWL_RE.match(line.strip())
        if crawl_match and crawl_match.group(1) in wanted_keys:
            crawl.append((crawl_match.group(1), crawl_match.group(2).strip()[:200]))
            continue
        source = SOURCE_RE.match(line.strip())
        if source and source.group(1) in wanted_keys:
            sources[source.group(1)] = source.group(2).strip()[:120]
    if current is not None:
        # Unterminated block: do not trust it.
        snapshots.pop(current, None)
        statuses.setdefault(current, ("error", "snapshot block not closed"))
    return {k: {"lines": v, "source": sources.get(k)} for k, v in snapshots.items()}, statuses, crawl


def write_snapshot(net, lines, source, today):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    page = source or net.get("dashboard_path", "dashboard")
    header = "# source: {} dashboard {}, read {}".format(net["name"], page.replace("\n", " "), today)
    path = os.path.join(SNAPSHOT_DIR, net["key"] + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n" + "\n".join(lines) + "\n")
    return path


def find_liftoff_download(started_epoch):
    """Newest Vungle/Liftoff app-ads.txt file in ~/Downloads modified after the run started."""
    best = None
    try:
        names = os.listdir(DOWNLOADS_DIR)
    except OSError:
        return None
    for name in names:
        if not LIFTOFF_NAME_RE.search(name) or not name.lower().endswith(".txt"):
            continue
        path = os.path.join(DOWNLOADS_DIR, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        created = getattr(stat, "st_birthtime", stat.st_mtime)
        if max(created, stat.st_mtime) < started_epoch:
            continue
        if best is None or stat.st_mtime > best[0]:
            best = (stat.st_mtime, path)
    return best[1] if best else None


def read_liftoff_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f.read().splitlines()]
    records = [l for l in lines if RECORD_RE.match(l)]
    if not any(l.lower().startswith("vungle.com,") for l in records):
        return None
    return records


def run_report(run, env):
    os.makedirs(STATE_DIR, exist_ok=True)
    out_md = os.path.join(STATE_DIR, "last-report.md")
    out_json = os.path.join(STATE_DIR, "last-report.json")
    cmd = [sys.executable, "-I", WATCH_PATH, "report", "--out", out_md, "--json", out_json,
           "--sdk-registry", SDK_REGISTRY]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    run.log("watch.py report exit {}: {}".format(result.returncode, (result.stdout + result.stderr).strip()))
    if result.returncode != 0:
        run.errors.append("watch.py report exit {}: {}".format(result.returncode,
                                                               (result.stderr or "").strip()[-300:]))
        return None
    with open(out_json, encoding="utf-8") as f:
        return json.load(f)


def add_this_run_login_needed(report, run, by_key):
    """A network Claude could not read in this run is login needed even if an older snapshot is still fresh."""
    listed = set(item["key"] for item in report["login_needed"])
    for key, (status, note) in sorted(run.claude_status.items()):
        if status != "login_needed" or key in listed or key not in by_key:
            continue
        net = by_key[key]
        report["login_needed"].append({"key": key, "name": net["name"], "dashboard_url": net.get("dashboard_url"),
                                       "reason": "bu turda giriş sayfası çıktı (eski snapshot kullanıldı)"})
        run.log("{}: login needed this run; older snapshot used for the report".format(key))


# ---------- Slack ----------

def build_message(report, run, today, is_retry):
    title = "app-ads.txt tekrar okuma" if is_retry else "app-ads.txt haftalık kontrol"
    out = ["*{}, {}*".format(title, today)]
    if report is None:
        out.append("Rapor üretilemedi.")
    else:
        adds = [(k, n) for k, n in report["networks"].items() if n["to_add"]]
        out.append("")
        if adds:
            out.append("*Eklenecek satırlar ({})*".format(report["to_add_total"]))
            for key, net in adds:
                out.append("{} (`{}`, {} satır)".format(slack_escape(net["name"]), key, len(net["to_add"])))
                shown = net["to_add"][:MAX_LINES_PER_NETWORK]
                block = "\n".join(slack_escape(l) for l in shown)
                if len(net["to_add"]) > MAX_LINES_PER_NETWORK:
                    block += "\n+{} more".format(len(net["to_add"]) - MAX_LINES_PER_NETWORK)
                out.append("```{}```".format(block))
        else:
            out.append("Eklenecek satır yok.")
        if report.get("mismatch_total"):
            out.append("Tip veya cert uyumsuzluğu: {} (yalnız raporda, otomatik eklenmez)".format(
                report["mismatch_total"]))

        if report["login_needed"]:
            out.append("")
            out.append("*Giriş gerekenler*")
            for item in report["login_needed"]:
                url = item.get("dashboard_url")
                name = slack_escape(item["name"])
                link = "<{}|{}>".format(url, name) if url else name
                out.append("• {}: {}".format(link, slack_escape(item["reason"])))

        registry = report.get("registry") or {}
        if registry.get("checked") and not registry.get("error"):
            new = list(registry.get("unmapped_ids", [])) + [
                "{} ({})".format(m["registry_id"], m["network"]) for m in registry.get("missing_networks", [])]
            removed = ["{} ({})".format(m["registry_id"], m["network"]) for m in registry.get("not_in_registry", [])]
            if new or removed:
                out.append("")
                out.append("*SDK registry*")
                if new:
                    out.append("• Yeni: {}".format(slack_escape(", ".join(new))))
                if removed:
                    out.append("• Kaldırılan: {}".format(slack_escape(", ".join(removed))))

        live = report.get("live") or {}
        if live.get("checked") and live.get("identical") is False:
            out.append("")
            out.append("Canlı site repodan farklı: {} satır yalnız repoda, {} yalnız canlıda.".format(
                len(live.get("only_in_repo", [])), len(live.get("only_in_live", []))))

    if run.crawl:
        out.append("")
        out.append("*Crawl uyarıları*")
        for key, text in run.crawl:
            out.append("• {}: {}".format(key, slack_escape(text)))

    errors = list(run.errors) + (list(report.get("errors", [])) if report else [])
    if errors:
        out.append("")
        out.append("*Hatalar*")
        for err in errors:
            out.append("• {}".format(slack_escape(err)[:300]))

    out.append("")
    out.append("Eklemek için @app-ads onayla (ya da onayla unity inmobi). Giriş gerekenleri yeniden okumak için "
               "@app-ads tekrar dene.")
    return "\n".join(out)


def post_slack(run, text, thread_ts):
    """Post to Slack and return the message ts. With --no-slack, print instead and return None."""
    if run.no_slack:
        print("----- Slack message (not posted) -----")
        print(text)
        print("----- end -----")
        return None
    try:
        secrets = read_slack_env()
    except OSError as exc:
        run.log("slack: cannot read {}: {}".format(SLACK_ENV_PATH, exc))
        return None
    payload = {"channel": CHANNEL_ID, "text": text, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    req = urllib.request.Request("https://slack.com/api/chat.postMessage",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": "Bearer " + secrets.get("SLACK_BOT_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        run.log("slack: post failed: {}".format(type(exc).__name__))
        return None
    if not body.get("ok"):
        run.log("slack: post failed: {}".format(body.get("error")))
        return None
    run.log("slack: posted ts {}".format(body.get("ts")))
    return body.get("ts")


def save_state(report, ts, thread_ts, today):
    state = {
        "ts": thread_ts or ts,
        "date": today,
        "channel": CHANNEL_ID,
        "to_add": {k: n["to_add"] for k, n in report["networks"].items() if n["to_add"]} if report else {},
        "login_needed": [i["key"] for i in report["login_needed"]] if report else [],
        "saved_epoch": int(time.time()),
    }
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


# ---------- main ----------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Read login-only ad network dashboards and post the report.")
    parser.add_argument("--networks", help="comma-separated snapshot network keys (default: all snapshot networks)")
    parser.add_argument("--thread-ts", help="reply in this Slack thread instead of a new message")
    parser.add_argument("--no-slack", action="store_true", help="print the message instead of posting it")
    parser.add_argument("--skip-claude", action="store_true", help="test: skip git pull and the Claude step")
    args = parser.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.datetime.now()
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    run = Run(os.path.join(LOG_DIR, now.strftime("%Y-%m-%d-%H%M%S") + ".log"), args.no_slack)

    lock = acquire_lock()
    if lock is None:
        print("app-ads-watch: another run is already in progress (lock {}).".format(LOCK_PATH), file=sys.stderr)
        return EXIT_LOCKED

    config = apply_local_dashboard_urls(load_sources())
    all_snapshot = snapshot_networks(config)
    if args.networks:
        wanted = [k.strip() for k in args.networks.split(",") if k.strip()]
        unknown = [k for k in wanted if k not in {n["key"] for n in all_snapshot}]
        if unknown:
            run.log("unknown or non-snapshot network keys: {}".format(", ".join(unknown)))
            run.errors.append("Bilinmeyen ağ: {}".format(", ".join(unknown)))
        networks = [n for n in all_snapshot if n["key"] in wanted]
    else:
        networks = all_snapshot
    run.log("run start: networks={} thread_ts={} no_slack={} skip_claude={}".format(
        ",".join(n["key"] for n in networks), args.thread_ts, args.no_slack, args.skip_claude))

    env = clean_env()
    started_epoch = time.time()

    if not args.skip_claude:
        pull_error = git_pull(run, env)
        if pull_error:
            text = "*app-ads.txt kontrolü durdu, {}*\ngit pull başarısız: ```{}```".format(
                today, slack_escape(pull_error[-500:]))
            post_slack(run, text, args.thread_ts)
            return 1

        if networks:
            output = run_claude(run, networks, today, env)
            keys = {n["key"] for n in networks}
            snapshots, statuses, crawl = parse_claude_output(output, keys)
            run.crawl = crawl
            by_key = {n["key"]: n for n in networks}
            for key, snap in snapshots.items():
                if key == "liftoff":
                    continue
                if not snap["lines"]:
                    run.log("{}: snapshot block had no record lines; not written".format(key))
                    run.errors.append("{}: liste boş geldi, snapshot yazılmadı".format(key))
                    continue
                path = write_snapshot(by_key[key], snap["lines"], snap["source"], today)
                run.log("{}: wrote {} lines to {}".format(key, len(snap["lines"]), path))
            run.claude_status = dict(statuses)
            for key, (status, note) in sorted(statuses.items()):
                run.log("{}: claude status {} {}".format(key, status, note))
                if status == "error":
                    run.errors.append("{}: {}".format(key, note or "okunamadı"))
            if "liftoff" in keys:
                download = find_liftoff_download(started_epoch)
                if download is None:
                    run.log("liftoff: no new download found in {}".format(DOWNLOADS_DIR))
                    if statuses.get("liftoff", ("",))[0] == "download_clicked":
                        run.errors.append("liftoff: indirilen dosya bulunamadı")
                else:
                    records = read_liftoff_file(download)
                    if records:
                        path = write_snapshot(by_key["liftoff"], records,
                                              "publisher.vungle.com/vungleAdsTxt (downloaded file)", today)
                        run.log("liftoff: wrote {} lines from {} to {}".format(len(records), download, path))
                    else:
                        run.log("liftoff: {} has no vungle.com line; ignored".format(download))
                        run.errors.append("liftoff: indirilen dosyada vungle.com satırı yok")

    report = run_report(run, env)
    if report is not None:
        add_this_run_login_needed(report, run, {n["key"]: n for n in networks})
        local_urls = {n["key"]: n.get("dashboard_url") for n in config.get("networks", [])}
        for item in report.get("login_needed", []):
            if local_urls.get(item.get("key")):
                item["dashboard_url"] = local_urls[item["key"]]
    text = build_message(report, run, today, is_retry=bool(args.thread_ts))
    ts = post_slack(run, text, args.thread_ts)
    if not args.no_slack:
        save_state(report, ts, args.thread_ts, today)
        run.log("state saved to {}".format(STATE_PATH))
    run.log("run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
