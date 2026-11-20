#!/usr/bin/python3 -I
"""Root-operated, approval-bound OM upgrade for an existing Linux installation."""
import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ACCOUNT = "liuxie"
LIVE = Path("/home") / ACCOUNT / "apps/options-monitor"
RELEASES = LIVE.parent / "releases"
RUNTIME = Path("/var/lib/options-monitor")
PROFILE = RUNTIME / "service.profile.json"
UNITS = Path("/etc/systemd/system")
SYSTEMCTL = "/usr/bin/systemctl"
OPEN_D = {f"options-monitor-opend-{account}.service" for account in ("lx", "sy")}
UNIT = re.compile(r"options-monitor-[a-z0-9-]+\.(?:service|timer)")
VERSION = re.compile(r"\d+\.\d+\.\d+")
SHA = re.compile(r"[a-f0-9]{64}")
LIMIT = 1_000_000
SHIM = '''#!/usr/bin/python3 -I
import json,os,socket,sys
args=sys.argv[1:]
data=sys.stdin.read(1000001) if args[:5]==["-n","sh","-c",'cat > "$1"',"sh"] else ""
packet=json.dumps([args,data]).encode()+b"\\n"
if len(packet)>1000000:
    sys.exit("systemd request too large")
with socket.socket(socket.AF_UNIX) as sock:
    sock.settimeout(70)
    sock.connect(os.environ["OM_ROOT_UPGRADE_SOCKET"])
    sock.sendall(packet)
    result=json.loads(sock.makefile("rb").readline(4096))
if not result:
    sys.exit("operation outside approved OM systemd plan")
'''

def require(condition, message):
    if not condition:
        raise ValueError(message)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def safe_root_path(path, kind):
    metadata = path.lstat()
    require(kind(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o022,
            f"unsafe root path: {path}")
    return metadata

def allowed_name(name):
    return isinstance(name, str) and UNIT.fullmatch(name) and "opend-" not in name

def unit_path(relative):
    path = Path(relative)
    require(path.as_posix() == relative, f"noncanonical unit path: {relative}")
    parts = path.parts
    valid_dropin = len(parts) == 2 and parts[0].endswith(".service.d") and parts[1] == "zzzz-secret-credentials.conf"
    name = parts[0][:-2] if valid_dropin else relative
    require((valid_dropin or len(parts) == 1) and allowed_name(name), f"out of scope unit path: {relative}")
    return UNITS / relative

def release_path(version):
    require(isinstance(version, str) and VERSION.fullmatch(version), "invalid release version")
    path = RELEASES / version
    require(path.resolve() == path and (path / "VERSION").read_text().strip() == version,
            f"invalid release tree: {path}")
    return path

def deployment_profile():
    content = PROFILE.read_bytes()
    profile = json.loads(content)
    require(profile.get("deploy_user") == ACCOUNT and profile.get("service_provider") == "systemd",
            "unsupported deployment profile")
    require(set(profile.get("external_opend_units", [])) == OPEN_D, "both OpenD services must be external")
    require(profile.get("restart", {}).get("command_prefix") == ["sudo", "-n", "systemctl"],
            "unsupported restart command prefix")
    require(LIVE.is_symlink(), "current must be the stable symlink")
    return profile, digest(content)

def child_options(account, path=None, endpoint=None):
    env = {"HOME": account.pw_dir, "PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    if endpoint:
        env["PATH"] = str(path) + ":" + env["PATH"]
        env["OM_ROOT_UPGRADE_SOCKET"] = str(endpoint)
        env.update(OM_UPGRADE_INSTALLER="pip", PIP_NO_INDEX="1", PIP_CONFIG_FILE="/dev/null",
                   PIP_FIND_LINKS=str(LIVE.parent / "_cache/wheels"))
    return {"user": account.pw_uid, "group": account.pw_gid,
            "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid), "env": env, "umask": 0o077}

def restricted(args):
    return ["/usr/bin/setpriv", "--no-new-privs", "--", *args]

def read_preview(release, account):
    cmd = [str(release / "om"), "service", "drift", "--repo-root", str(LIVE),
           "--runtime-root", str(RUNTIME), "--preserve-activation-state"]
    out = subprocess.run(restricted(cmd), cwd=release, **child_options(account),
                         capture_output=True, timeout=120, check=False)
    data = json.loads(out.stdout)["data"]
    require(data["checked"] and data.get("confirmed") is False
            and set(data.get("external_opend_units", [])) == OPEN_D, "drift preview did not protect both OpenD units")
    for key in ("extra_managed_units", "extra_managed_files", "missing_installed_units",
                "missing_managed_files", "mode_mismatched_managed_files", "activation_drift_units",
                "execution_drift_units"):
        require(not data[key], f"unsupported service drift: {key}")
    files = set(data["mismatched_units"])
    files.update(str(Path(path).relative_to(UNITS)) for path in data["mismatched_managed_files"])
    for path in files:
        unit_path(path)
    timers = {name for name in data["mismatched_units"] if name.endswith(".timer")}
    return files, timers - set(data["preserved_activation_units"])

def read_plan(path, account):
    for folder in [*reversed(path.parents)]:
        safe_root_path(folder, stat.S_ISDIR)
    metadata = safe_root_path(path, stat.S_ISREG)
    require(not metadata.st_mode & 0o077 and metadata.st_size < LIMIT, "plan must be private to root")
    plan = json.loads(path.read_bytes())
    require(set(plan) == {"from", "to", "profile_sha256", "files"}, "invalid plan fields")
    actual, target = release_path(plan["from"]), release_path(plan["to"])
    require(actual == LIVE.resolve() and tuple(map(int, plan["from"].split("."))) >= (4, 6, 49)
            and tuple(map(int, plan["to"].split("."))) > tuple(map(int, plan["from"].split("."))),
            "plan source or target does not match a supported forward upgrade")
    profile, profile_hash = deployment_profile()
    require(plan["profile_sha256"] == profile_hash, "current profile differs from approved plan")
    restart = profile["restart"]["services"]
    require(isinstance(restart, list) and all(allowed_name(name) and name.endswith(".service") for name in restart),
            "unsupported service restart")
    require(isinstance(plan["files"], dict), "invalid approved files")
    for relative, entry in plan["files"].items():
        path = unit_path(relative)
        safe_root_path(path.parent, stat.S_ISDIR)
        safe_root_path(path, stat.S_ISREG)
        require(set(entry) == {"before", "after"} and all(SHA.fullmatch(x) for x in entry.values()),
                f"invalid approved digest: {relative}")
        require(digest(path.read_bytes()) == entry["before"], f"systemd file changed: {relative}")
    rollback, _ = read_preview(actual, account)
    desired, timers = read_preview(target, account)
    require(not rollback, "active release must reconcile cleanly before upgrade")
    require(desired == set(plan["files"]), "target preview differs from approved file set")
    for unit in set(restart) | timers:
        require(allowed_name(unit), f"out of scope restart: {unit}")
        query = subprocess.check_output([SYSTEMCTL, "show", "-p", "Id,Names,Conflicts,PropagatesStopTo", unit],
                                        text=True, timeout=10)
        row = dict(line.split("=", 1) for line in query.splitlines())
        require(row.get("Id") == unit and not any(name in query for name in OPEN_D), f"unsafe systemd identity: {unit}")
    return actual, target, plan, set(restart) | timers

def upgrade_command(target, plan):
    return [str(target / "om"), "update", "apply", "--repo-root", str(LIVE), "--runtime-root", str(RUNTIME),
            "--target-version", plan["to"], "--preserve-activation-state"]

def check_upgrade(target, plan, account):
    preview = subprocess.run(restricted(upgrade_command(target, plan)), cwd=target, **child_options(account),
                             capture_output=True, timeout=140, check=False)
    result = json.loads(preview.stdout)["data"]
    require(preview.returncode == 0 and result.get("status") == "dry_run"
            and result.get("changed") is False and result.get("target_version") == plan["to"],
            "OM upgrade preview did not accept this release")

def replace_unit(path, content, allowed):
    owner = safe_root_path(path, stat.S_ISREG)
    require(owner.st_nlink == 1 and digest(path.read_bytes()) in allowed and digest(content) in allowed,
            "unexpected unit content")
    require(not any(unit.encode() in content for unit in OPEN_D), "unit refers to external OpenD")
    fd, temporary = tempfile.mkstemp(prefix=".om-root-upgrade-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchown(output.fileno(), owner.st_uid, owner.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(owner.st_mode))
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        current = path.lstat()
        require(
            (current.st_dev, current.st_ino, current.st_ctime_ns) == (owner.st_dev, owner.st_ino, owner.st_ctime_ns),
            f"unit changed during operation: {path}",
        )
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def handle(args, content, files, restarts):
    require(isinstance(args, list) and all(isinstance(s, str) for s in args), "invalid command")
    require(isinstance(content, str) and not any(name in token for name in OPEN_D for token in args),
            "external OpenD operation denied")
    if args[:3] == ["-n", "install", "-d"] and len(args) == 4:
        parent = Path(args[3])
        require(parent in {unit_path(name).parent for name in files}, "directory outside plan")
        safe_root_path(parent, stat.S_ISDIR)
    elif args[:5] == ["-n", "sh", "-c", 'cat > "$1"', "sh"] and len(args) == 6:
        relative = str(Path(args[5]).relative_to(UNITS))
        path = unit_path(relative)
        allowed = set(files[relative].values())
        safe_root_path(path.parent, stat.S_ISDIR)
        replace_unit(path, content.encode(), allowed)
    elif args == ["-n", "systemctl", "daemon-reload"] and files:
        subprocess.run([SYSTEMCTL, "daemon-reload"], check=True, timeout=60)
    else:
        require(len(args) == 4 and args[:3] == ["-n", "systemctl", "restart"] and args[3] in restarts,
                "command outside plan")
        subprocess.run([SYSTEMCTL, "restart", args[3]], check=True, timeout=60)

def apply(target, plan, restarts, account):
    with (
        tempfile.TemporaryDirectory(prefix="om-root-upgrade-", dir="/run") as socket_directory,
        tempfile.TemporaryDirectory(prefix="om-root-upgrade-", dir="/var/tmp") as shim_directory,
    ):
        socket_root, shim_root = Path(socket_directory), Path(shim_directory)
        socket_root.chmod(0o711)
        shim_root.chmod(0o711)
        shim = shim_root / "sudo"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        endpoint = socket_root / "broker"
        with socket.socket(socket.AF_UNIX) as broker:
            broker.bind(str(endpoint))
            os.chown(endpoint, account.pw_uid, account.pw_gid)
            endpoint.chmod(0o600)
            broker.listen(4)
            broker.settimeout(0.25)
            argv = restricted([*upgrade_command(target, plan), "--confirm"])
            with subprocess.Popen(argv, cwd=target, **child_options(account, shim_root, endpoint)) as process:
                try:
                    while process.poll() is None:
                        try:
                            client, _ = broker.accept()
                        except TimeoutError:
                            continue
                        with client:
                            client.settimeout(5)
                            ok = False
                            try:
                                _, uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                                require(uid == account.pw_uid, "unexpected peer")
                                with client.makefile("rb") as wire:
                                    args, content = json.loads(wire.readline(LIMIT))
                                handle(args, content, plan["files"], restarts)
                                ok = True
                            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                                print(f"Privilege request denied or failed: {exc}", file=sys.stderr, flush=True)
                            try:
                                client.sendall(json.dumps(ok).encode() + b"\n")
                            except OSError:
                                pass
                finally:
                    if process.poll() is None:
                        process.terminate()
                return process.returncode

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, nargs="?", help="root-owned private JSON policy")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    require(os.geteuid() == 0 and hasattr(socket, "SO_PEERCRED"), "requires root on Linux")
    account = pwd.getpwnam(ACCOUNT)
    require(account.pw_uid != 0, "deploy user cannot be root")
    if args.plan is None:
        require(not args.confirm, "confirmed upgrade requires a plan")
        _, profile_hash = deployment_profile()
        print(json.dumps({"status": "installed", "current": (LIVE / "VERSION").read_text().strip(),
                          "deploy_uid": account.pw_uid, "profile_sha256": profile_hash,
                          "external_opend_units": sorted(OPEN_D)}))
        return 0
    fd = os.open("/run/om-root-upgrade.lock", os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual, target, plan, restarts = read_plan(Path(os.path.abspath(args.plan)), account)
        check_upgrade(target, plan, account)
        print(json.dumps({"status": "confirmed" if args.confirm else "preview", "from": actual.name,
                          "to": target.name, "files": sorted(plan["files"]), "restarts": sorted(restarts)}), flush=True)
        return apply(target, plan, restarts, account) if args.confirm else 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"OM root upgrade stopped: {exc}") from None
