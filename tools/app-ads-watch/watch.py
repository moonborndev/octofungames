#!/usr/bin/env python3
"""app-ads.txt watch: compare our app-ads.txt with what each ad network requires.

Commands:
  report (default)  Build a Markdown report (and optional JSON summary). Never edits files.
  apply             Add the missing lines of the given networks to app-ads.txt. Never removes,
                    edits or re-sorts existing lines and never commits.

Python 3 standard library only. See README.md.
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "app-ads-watch/1.0"
FETCH_TIMEOUT_SECONDS = 30

# Canonical form used in our file: "domain, id, TYPE" or "domain, id, TYPE, certId".
CANONICAL_RE = re.compile(r"^[a-z0-9.-]+, [^,\s]+, (DIRECT|RESELLER)(, [^,\s]+)?$")
# app-ads.txt variable lines such as OWNERDOMAIN=octofungames.com.
VARIABLE_RE = re.compile(r"^[A-Za-z]+=\S")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
PLACEHOLDER_CHARS = "[]<>{}"


class Record:
    """One app-ads.txt line. Compared on domain + id (case-insensitive) + type."""

    def __init__(self, domain, pub_id, rel_type, cert, raw):
        self.domain = domain
        self.pub_id = pub_id
        self.rel_type = rel_type
        self.cert = cert
        self.raw = raw

    @property
    def key(self):
        return (self.domain, self.pub_id.lower(), self.rel_type)

    @property
    def pair(self):
        return (self.domain, self.pub_id.lower())

    @property
    def cert_key(self):
        return (self.cert or "").lower()

    def canonical(self):
        parts = [self.domain, self.pub_id, self.rel_type]
        if self.cert:
            parts.append(self.cert)
        return ", ".join(parts)


def parse_loose(line):
    """Parse a line from a network list. Returns (Record, None), (None, error) or (None, None) to skip."""
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("//"):
        return None, None
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    if VARIABLE_RE.match(text) and "," not in text:
        return None, None
    if any(ch in text for ch in PLACEHOLDER_CHARS):
        return None, None
    fields = [f.strip() for f in text.split(",")]
    if len(fields) < 3 or not fields[0] or not fields[1]:
        return None, "not enough fields: " + line.strip()
    rel_type = fields[2].upper()
    if rel_type not in ("DIRECT", "RESELLER"):
        return None, "unknown relationship type: " + line.strip()
    cert = fields[3] if len(fields) > 3 and fields[3] else None
    return Record(fields[0].lower(), fields[1], rel_type, cert, line.rstrip("\n")), None


def normalize_direct(record, own_domains):
    """Only the network's own domain can be DIRECT for us; every other DIRECT line becomes RESELLER."""
    if record.rel_type == "DIRECT" and record.domain not in own_domains:
        return Record(record.domain, record.pub_id, "RESELLER", record.cert, record.raw), True
    return record, False


def dedupe(records):
    """Keep one record per key, preferring the one that carries a cert id."""
    by_key = {}
    order = []
    for rec in records:
        if rec.key not in by_key:
            by_key[rec.key] = rec
            order.append(rec.key)
        elif rec.cert and not by_key[rec.key].cert:
            by_key[rec.key] = rec
    return [by_key[k] for k in order]


# ---------- sources ----------

def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_markdown_code_blocks(text):
    """Return the lines inside ``` fenced blocks (used for the Mintegral GitBook page)."""
    lines = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


def parse_plain(text):
    return text.splitlines()


PARSERS = {
    "markdown_code_blocks": parse_markdown_code_blocks,
    "plain": parse_plain,
}


def read_snapshot(path, today, stale_days):
    """Return (lines, read_date or None, status). Status is fresh, stale or missing."""
    if not os.path.isfile(path):
        return [], None, "missing"
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    read_date = None
    for line in lines:
        if not line.strip().startswith("#"):
            continue
        match = DATE_RE.search(line)
        if match:
            try:
                read_date = datetime.date.fromisoformat(match.group(1))
            except ValueError:
                read_date = None
            break
    if read_date is None or (today - read_date).days > stale_days:
        return lines, read_date, "stale"
    return lines, read_date, "fresh"


def load_network_list(net, config, today, errors, fetch=True):
    """Collect the raw lines a network requires. Returns a status dict and the raw lines."""
    info = {"status": "n/a", "snapshot_date": None, "parse_errors": []}
    raw_lines = []
    if net["type"] == "public":
        info["status"] = "ok"
        if not fetch:
            info["status"] = "skipped"
        for src in net.get("public_sources", []) if fetch else []:
            parser = PARSERS.get(src.get("parser", "plain"))
            if parser is None:
                msg = "{}: unknown parser {}".format(net["key"], src.get("parser"))
                errors.append(msg)
                info["status"] = "error"
                continue
            try:
                raw_lines.extend(parser(fetch_text(src["url"])))
            except Exception as exc:  # network errors must not stop the run
                msg = "{}: fetch failed for {}: {}".format(net["key"], src["url"], exc)
                errors.append(msg)
                info["status"] = "error"
    elif net["type"] == "snapshot":
        path = os.path.join(HERE, config.get("snapshot_dir", "snapshots"), net["key"] + ".txt")
        lines, read_date, status = read_snapshot(path, today, config.get("snapshot_stale_days", 8))
        raw_lines.extend(lines)
        info["status"] = status
        info["snapshot_date"] = read_date.isoformat() if read_date else None
    return info, raw_lines


# ---------- our file ----------

class OurFile:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.text = f.read()
        self.lines = self.text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.records = []
        self.format_errors = []
        for number, line in enumerate(self.lines, 1):
            if not line.strip() or VARIABLE_RE.match(line) or line.strip().startswith("#"):
                continue
            rec, err = parse_loose(line)
            if rec is None:
                self.format_errors.append((number, line, err or "unparsable line"))
                continue
            if not CANONICAL_RE.match(line):
                self.format_errors.append((number, line, "not in canonical form 'domain, id, TYPE[, certId]'"))
            rec.line_number = number
            self.records.append(rec)
        self.by_key = {}
        self.by_pair = {}
        for rec in self.records:
            self.by_key.setdefault(rec.key, []).append(rec)
            self.by_pair.setdefault(rec.pair, []).append(rec)

    def order_errors(self):
        """Record lines that break the file's sort rule (case-insensitive by whole line)."""
        bad = []
        for prev, cur in zip(self.records, self.records[1:]):
            if prev.raw.lower() > cur.raw.lower():
                bad.append((cur.line_number, prev.raw, cur.raw))
        return bad

    def duplicates(self):
        """Groups with the same domain + id + type. Kind is case-only or cert-variant."""
        groups = []
        for key, recs in self.by_key.items():
            if len(recs) < 2:
                continue
            lowered = {r.raw.lower() for r in recs}
            kind = "case-only" if len(lowered) == 1 else "cert-variant"
            if len(lowered) < len(recs) and len(lowered) > 1:
                kind = "case-only + cert-variant"
            groups.append({"kind": kind, "lines": [r.raw for r in recs]})
        groups.sort(key=lambda g: g["lines"][0].lower())
        return groups


# ---------- comparison ----------

def compare_network(net, raw_lines, ours, info):
    own = set(d.lower() for d in net.get("own_domains", []))
    required = []
    demoted = 0
    for line in list(net.get("direct_lines", [])) + raw_lines:
        rec, err = parse_loose(line)
        if err:
            info["parse_errors"].append(err)
        if rec is None:
            continue
        rec, changed = normalize_direct(rec, own)
        demoted += 1 if changed else 0
        required.append(rec)
    required = dedupe(required)
    to_add = []
    mismatches = []
    for rec in required:
        if rec.key in ours.by_key:
            our_recs = ours.by_key[rec.key]
            if rec.cert and rec.cert_key not in {r.cert_key for r in our_recs}:
                mismatches.append({"required": rec.canonical(), "ours": [r.raw for r in our_recs],
                                   "reason": "cert id differs"})
        elif rec.pair in ours.by_pair:
            mismatches.append({"required": rec.canonical(), "ours": [r.raw for r in ours.by_pair[rec.pair]],
                               "reason": "relationship type differs"})
        else:
            to_add.append(rec)
    return {"required_count": len(required), "demoted_direct": demoted,
            "to_add": to_add, "mismatches": mismatches}


def live_check(ours, url, errors):
    result = {"checked": True, "url": url, "identical": None, "only_in_repo": [], "only_in_live": [], "error": None}
    try:
        live = fetch_text(url)
    except Exception as exc:
        result["error"] = str(exc)
        errors.append("live: fetch failed for {}: {}".format(url, exc))
        return result
    if live == ours.text:
        result["identical"] = True
        return result
    result["identical"] = False
    live_lines = live.split("\n")
    repo_set, live_set = set(ours.lines), set(live_lines)
    result["only_in_repo"] = [l for l in ours.lines if l not in live_set]
    result["only_in_live"] = [l for l in live_lines if l not in repo_set and l != ""]
    return result


def registry_check(path, config, errors):
    result = {"checked": True, "path": path, "registry_ids": [], "unmapped_ids": [],
              "missing_networks": [], "not_in_registry": [], "error": None}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception as exc:
        result["error"] = str(exc)
        errors.append("registry: cannot read {}: {}".format(path, exc))
        return result
    ids = sorted(set(re.findall(r'"([A-Za-z0-9_.-]+-mediation)"', text)))
    result["registry_ids"] = ids
    mapping = config.get("sdk_registry_mediation_map", {})
    network_keys = {n["key"] for n in config["networks"]}
    for rid in ids:
        if rid not in mapping:
            result["unmapped_ids"].append(rid)
        elif mapping[rid] not in network_keys:
            result["missing_networks"].append({"registry_id": rid, "network": mapping[rid]})
    for rid, key in sorted(mapping.items()):
        if rid.endswith("-mediation") and rid not in ids:
            result["not_in_registry"].append({"registry_id": rid, "network": key})
    return result


# ---------- report ----------

def run_checks(config, ours, today, fetch=True):
    errors = []
    results = []
    for net in config["networks"]:
        info, raw_lines = load_network_list(net, config, today, errors, fetch=fetch)
        cmp = compare_network(net, raw_lines, ours, info)
        results.append((net, info, cmp))
    return results, errors


def build_report(config, ours, results, errors, live, registry, today):
    out = []
    out.append("# app-ads.txt report {}".format(today.isoformat()))
    out.append("")
    out.append("File: `{}` ({} lines, {} records)".format(os.path.relpath(ours.path, HERE), len(ours.lines),
                                                       len(ours.records)))
    out.append("")
    out.append("## Networks")
    out.append("")
    out.append("| Network | Source | Status | Required lines | To add | Mismatches |")
    out.append("|---|---|---|---|---|---|")
    for net, info, cmp in results:
        status = info["status"]
        if info.get("snapshot_date"):
            status += " ({})".format(info["snapshot_date"])
        if not net.get("required", True):
            status = "not required"
        out.append("| {} | {} | {} | {} | {} | {} |".format(net["name"], net["type"], status, cmp["required_count"],
                                                          len(cmp["to_add"]), len(cmp["mismatches"])))
    out.append("")

    login = login_needed(results)
    if login:
        out.append("## Login needed (snapshot missing or stale)")
        out.append("")
        for item in login:
            verified = "" if item["dashboard_url_verified"] else " (URL not verified)"
            out.append("- {}: {} - {}{}".format(item["name"], item["reason"], item["dashboard_url"], verified))
        out.append("")

    out.append("## To add")
    out.append("")
    any_add = False
    for net, info, cmp in results:
        if not cmp["to_add"]:
            continue
        any_add = True
        out.append("### {}".format(net["name"]))
        out.append("")
        out.append("```")
        out.extend(r.canonical() for r in cmp["to_add"])
        out.append("```")
        out.append("")
    if not any_add:
        out.append("Nothing to add from the sources that were available.")
        out.append("")

    out.append("## Type or cert mismatches (report only)")
    out.append("")
    any_mm = False
    for net, info, cmp in results:
        for mm in cmp["mismatches"]:
            any_mm = True
            out.append("- {}: `{}` ({}); ours: {}".format(net["name"], mm["required"], mm["reason"],
                                                         ", ".join("`{}`".format(o) for o in mm["ours"])))
    if not any_mm:
        out.append("None.")
    out.append("")

    dups = ours.duplicates()
    out.append("## Duplicates in our file ({} groups)".format(len(dups)))
    out.append("")
    if dups:
        out.append("Same domain + id + type. `case-only` lines differ only in letter case; `cert-variant` lines "
                   "differ in the cert id (or one has none).")
        out.append("")
        for group in dups:
            out.append("- {}: {}".format(group["kind"], " | ".join("`{}`".format(l) for l in group["lines"])))
    else:
        out.append("None.")
    out.append("")

    out.append("## Format")
    out.append("")
    order = ours.order_errors()
    if not ours.format_errors and not order:
        out.append("No format or sort-order errors.")
    for number, line, err in ours.format_errors:
        out.append("- line {}: `{}` - {}".format(number, line, err))
    for number, prev, cur in order:
        out.append("- line {}: out of order (`{}` after `{}`)".format(number, cur, prev))
    for net, info, cmp in results:
        for err in info["parse_errors"]:
            out.append("- {} list: {}".format(net["name"], err))
    out.append("")

    out.append("## Repo vs live site")
    out.append("")
    if not live["checked"]:
        out.append("Skipped (--no-live).")
    elif live["error"]:
        out.append("Could not fetch {}: {}".format(live["url"], live["error"]))
    elif live["identical"]:
        out.append("{} is identical to the repo file.".format(live["url"]))
    else:
        out.append("{} differs from the repo file: {} lines only in repo, {} only live.".format(
            live["url"], len(live["only_in_repo"]), len(live["only_in_live"])))
        for l in live["only_in_repo"][:50]:
            out.append("- repo only: `{}`".format(l))
        for l in live["only_in_live"][:50]:
            out.append("- live only: `{}`".format(l))
    out.append("")

    out.append("## SDK registry")
    out.append("")
    if not registry["checked"]:
        out.append("Skipped (no --sdk-registry).")
    elif registry["error"]:
        out.append("Could not read registry: {}".format(registry["error"]))
    else:
        out.append("Mediation ids in registry: {}".format(", ".join(registry["registry_ids"]) or "none"))
        out.append("")
        if not (registry["unmapped_ids"] or registry["missing_networks"] or registry["not_in_registry"]):
            out.append("Registry and sources.json agree.")
        for rid in registry["unmapped_ids"]:
            out.append("- `{}` is in the registry but not in sdk_registry_mediation_map".format(rid))
        for item in registry["missing_networks"]:
            out.append("- `{}` maps to `{}`, which has no entry in networks".format(item["registry_id"],
                                                                                 item["network"]))
        for item in registry["not_in_registry"]:
            out.append("- `{}` ({}) is in sources.json but no longer in the registry".format(item["registry_id"],
                                                                                           item["network"]))
    out.append("")

    out.append("## Errors")
    out.append("")
    out.extend("- {}".format(e) for e in errors) if errors else out.append("None.")
    out.append("")
    return "\n".join(out)


def login_needed(results):
    items = []
    for net, info, cmp in results:
        if net["type"] != "snapshot" or info["status"] == "fresh":
            continue
        reason = "snapshot missing" if info["status"] == "missing" else "snapshot stale (read {})".format(
            info.get("snapshot_date") or "unknown date")
        items.append({"key": net["key"], "name": net["name"], "reason": reason,
                      "dashboard_url": net.get("dashboard_url"),
                      "dashboard_url_verified": net.get("dashboard_url_verified", False),
                      "page_wait_seconds": net.get("page_wait_seconds", 60),
                      "error_wait_seconds": net.get("error_wait_seconds", 10)})
    return items


def build_json(ours, results, errors, live, registry, today):
    networks = {}
    for net, info, cmp in results:
        networks[net["key"]] = {
            "name": net["name"], "type": net["type"], "required": net.get("required", True),
            "status": info["status"], "snapshot_date": info.get("snapshot_date"),
            "required_count": cmp["required_count"], "to_add_count": len(cmp["to_add"]),
            "to_add": [r.canonical() for r in cmp["to_add"]], "mismatch_count": len(cmp["mismatches"]),
            "mismatches": cmp["mismatches"], "parse_errors": info["parse_errors"],
        }
    dups = ours.duplicates()
    return {
        "date": today.isoformat(),
        "file_lines": len(ours.lines),
        "file_records": len(ours.records),
        "to_add_total": sum(n["to_add_count"] for n in networks.values()),
        "mismatch_total": sum(n["mismatch_count"] for n in networks.values()),
        "networks": networks,
        "login_needed": login_needed(results),
        "duplicates": {"groups": len(dups),
                       "case_only": sum(1 for g in dups if "case-only" in g["kind"]),
                       "cert_variant": sum(1 for g in dups if g["kind"] == "cert-variant"),
                       "items": dups},
        "format_errors": [{"line": n, "text": t, "error": e} for n, t, e in ours.format_errors],
        "order_errors": [{"line": n, "previous": p, "text": c} for n, p, c in ours.order_errors()],
        "live": live,
        "registry": registry,
        "errors": errors,
    }


# ---------- apply ----------

def insert_sorted(lines, new_line):
    """Insert before the first record line that sorts after new_line (case-insensitive by whole line).

    Blank lines, comments and variable lines (OWNERDOMAIN=...) are never moved; a line that sorts last
    goes right after the last record line, so the trailing variable block stays at the end.
    """
    key = new_line.lower()
    last_record = -1
    for index, line in enumerate(lines):
        if not line.strip() or line.strip().startswith("#") or VARIABLE_RE.match(line):
            continue
        if line.lower() > key:
            lines.insert(index, new_line)
            return
        last_record = index
    lines.insert(last_record + 1, new_line)


def cmd_apply(args, config, today):
    ours = OurFile(args.file)
    if args.all:
        selected = [n["key"] for n in config["networks"]]
    elif args.networks:
        selected = [k.strip() for k in args.networks.split(",") if k.strip()]
    else:
        print("apply needs --networks a,b or --all", file=sys.stderr)
        return 2
    known = {n["key"] for n in config["networks"]}
    unknown = [k for k in selected if k not in known]
    if unknown:
        print("unknown network keys: {}".format(", ".join(unknown)), file=sys.stderr)
        return 2
    results, errors = run_checks(config, ours, today)
    for err in errors:
        print("warning: " + err, file=sys.stderr)
    added = []
    added_keys = set()
    lines = list(ours.lines)
    for net, info, cmp in results:
        if net["key"] not in selected:
            continue
        if net["type"] == "snapshot" and info["status"] != "fresh":
            print("warning: {} snapshot is {}".format(net["key"], info["status"]), file=sys.stderr)
        for rec in cmp["to_add"]:
            if rec.key in added_keys:
                continue
            added_keys.add(rec.key)
            line = rec.canonical()
            insert_sorted(lines, line)
            added.append((net["key"], line))
    if not added:
        print("Nothing to add.")
        return 0
    with open(args.file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("Added {} line(s) to {}:".format(len(added), args.file))
    for key, line in added:
        print("  [{}] {}".format(key, line))
    return 0


def cmd_report(args, config, today):
    ours = OurFile(args.file)
    results, errors = run_checks(config, ours, today)
    if args.no_live:
        live = {"checked": False}
    else:
        live = live_check(ours, config["live_url"], errors)
    if args.sdk_registry:
        registry = registry_check(args.sdk_registry, config, errors)
    else:
        registry = {"checked": False}
    report = build_report(config, ours, results, errors, live, registry, today)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        print(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(build_json(ours, results, errors, live, registry, today), f, indent=2)
            f.write("\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare app-ads.txt with each ad network's required lines.")
    parser.add_argument("command", nargs="?", default="report", choices=["report", "apply"])
    parser.add_argument("--config", default=os.path.join(HERE, "sources.json"))
    parser.add_argument("--file", help="app-ads.txt to check or edit (default: app_ads_file in sources.json)")
    parser.add_argument("--out", help="report: write Markdown here instead of stdout")
    parser.add_argument("--json", help="report: write a machine-readable summary here")
    parser.add_argument("--no-live", action="store_true", help="report: skip the repo vs live site check")
    parser.add_argument("--sdk-registry", help="report: path to SDKProviderRegistry.cs")
    parser.add_argument("--networks", help="apply: comma-separated network keys")
    parser.add_argument("--all", action="store_true", help="apply: every network")
    args = parser.parse_args(argv)

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    if not args.file:
        args.file = os.path.normpath(os.path.join(HERE, config["app_ads_file"]))
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if args.command == "apply":
        return cmd_apply(args, config, today)
    return cmd_report(args, config, today)


if __name__ == "__main__":
    sys.exit(main())
