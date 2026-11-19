"""Scan this export only. Print file/line/category, never matched secret content."""
import ast
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".venv", "__pycache__", ".pytest_cache", "node_modules", "dist", ".local-checks"}
FORBIDDEN_PARTS = {".venv", "__pycache__", ".pytest_cache", ".local-checks", ".git", "node_modules", "dist", "test-results", "playwright-report", "client_context", "restoration", "exports", "artifacts", "fixtures", "backups"}
FORBIDDEN_NAMES = {".env", "authorized_keys", "known_hosts"}
PATTERNS = {
    "operational_hostname": r"\bv[0-9]{8,}\b",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "access_token": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9]{24,})",
    "map_token": r"(?<![A-Za-z0-9_$])[ps]k[.][A-Za-z0-9_-]{8,}",
    "credential_url": r"[a-z][a-z0-9+.-]*://[^\s/:<>]+:[^\s@<>]+@",
    "private_home_path": r"/(?:ho[m]e|ro[o]t)/[A-Za-z0-9_.-]+/",
    "operational_domain": r"(?:megasrv[.]de|carbonmaps[.]net)",
    "credential_pair": r"admi[n]\s*[/ :]\s*admi[n]",
    "private_numeric_assessment": r"(?:verification_factor|risk_threshold|anomaly_threshold|watch_threshold|soil_coefficient|root_coefficient)\s*[=:]\s*[-+]?\d",
}
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def source_paths():
    """Scan every tracked file, even if its path matches an ignore rule."""
    git_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        top = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True, env=git_env)
    except (FileNotFoundError, subprocess.CalledProcessError):
        top = None
    if top is not None and Path(top.stdout.strip()).resolve() == ROOT.resolve():
        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=True, env=git_env,
        )
        names = set(listed.stdout.decode("utf-8").split("\0")) - {""}
        return [ROOT / name for name in sorted(names)], "tracked files and nonignored untracked files"

    # An exported source archive has no Git metadata. Exclude local runtime
    # trees only in this mode; symlinks and nested Git metadata still fail.
    paths = []
    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            if path.is_symlink():
                paths.append(path)
                dirs.remove(name)
            elif name in IGNORED or (parent == ROOT and name == ".git"):
                dirs.remove(name)
        paths.extend(parent / name for name in names)
    return sorted(paths), "source archive excluding local runtime/build trees"


def scan():
    findings = []
    files = []
    paths, mode = source_paths()
    for path in paths:
        rel = path.relative_to(ROOT)
        if ".." in rel.parts or not path.resolve().is_relative_to(ROOT.resolve()):
            findings.append({"file": str(rel), "category": "path_outside_source"})
            continue
        if path.is_symlink():
            findings.append({"file": str(rel), "category": "symlink"})
            continue
        if any(p in FORBIDDEN_PARTS for p in rel.parts) or path.name in FORBIDDEN_NAMES:
            findings.append({"file": str(rel), "category": "forbidden_artifact"})
        if not path.is_file():
            continue
        files.append(str(rel))
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            findings.append({"file": str(rel), "category": "non_text_or_unreadable"})
            continue
        if "\0" in content:
            findings.append({"file": str(rel), "category": "binary_content"})
        if path.name in {"carbon_methodology.json", "carbon_baseline_ipcc_v0_1.json"}:
            try:
                if json.loads(content).get("available") is not False:
                    findings.append({"file": str(rel), "category": "private_configuration_not_redacted"})
            except (ValueError, AttributeError):
                findings.append({"file": str(rel), "category": "private_configuration_not_redacted"})
        if path.name in {"carbon.py", "stage5_carbon.py"}:
            private_tree = ast.parse(content)
            for node in private_tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name not in {"stage5_scope_params", "stage5_annual_increment_query_params"} and not isinstance(node.body[-1], ast.Raise):
                        findings.append({"file": str(rel), "category": "private_calculation_body_present"})
        if path.suffix == ".py":
            try:
                ast.parse(content)
            except SyntaxError as e:
                findings.append({"file": str(rel), "line": e.lineno, "category": "python_syntax"})
        for category, pattern in PATTERNS.items():
            for match in re.finditer(pattern, content, re.I):
                findings.append({"file": str(rel), "line": content[:match.start()].count("\n") + 1, "category": category})
        for match in IPV4.finditer(content):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if not address.is_loopback and not address.is_unspecified:
                findings.append({"file": str(rel), "line": content[:match.start()].count("\n") + 1, "category": "non_local_ip_address"})
        if path.name == ".env.example":
            for number, line in enumerate(content.splitlines(), 1):
                if not line or line.startswith("#"):
                    continue
                _, separator, value = line.partition("=")
                if not separator or not re.fullmatch(r"<[A-Z0-9_]+>", value):
                    findings.append({"file": str(rel), "line": number, "category": "environment_value_not_placeholder"})
    projection_path = ROOT / "docs" / "scope-projection.json"
    if projection_path.exists():
        try:
            projection = json.loads(projection_path.read_text())
            for entry in projection["file_decisions"]:
                relative = Path(entry["path"])
                target = ROOT / relative
                if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(ROOT.resolve()):
                    findings.append({"file": str(relative), "category": "projection_path_outside_source"})
                elif target.resolve() == projection_path.resolve():
                    findings.append({"file": str(relative), "category": "projection_self_hash"})
                elif target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                    findings.append({"file": str(relative), "category": "projection_hash_mismatch"})
        except (OSError, ValueError, KeyError, TypeError):
            findings.append({"file": "docs/scope-projection.json", "category": "invalid_projection"})

    return {"status": "pass" if not findings else "fail", "files_scanned": len(files), "files": files, "findings": findings, "scope": mode + "; tracked paths are never exempted by ignore patterns.", "ipv4_allowance": "Loopback and unspecified local bind addresses only; no remote host addresses.", "limitations": ["Pattern scan is not proof of no intellectual-property leakage.", "Retained public route names, field names and schema require a manual disclosure review.", "Retained frontend presentation logic must remain separate from private calculation implementations."]}


if __name__ == "__main__":
    report = scan()
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["status"] == "pass" else 1)
