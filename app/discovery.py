import fnmatch
import os
import re
from pathlib import Path, PurePosixPath

import yaml


class BackupError(Exception):
    """A safe, user-facing error. Never contains provider credentials or responses."""


def image_repository(image):
    image = image.split("@")[0]
    if ":" in image.rsplit("/", 1)[-1]:
        image = image.rsplit(":", 1)[0]
    image = image.removeprefix("docker.io/")
    return "library/" + image if "/" not in image else image


def match_rule(image, rules):
    candidates = [r for r in rules if fnmatch.fnmatchcase(image_repository(image), r["pattern"])]
    return max(candidates, key=lambda r: (len(re.sub(r"[*?\[\]]", "", r["pattern"])), r["pattern"]), default=None)


def validate_rule(pattern, paths, excludes):
    if not pattern or len(pattern) > 240 or any(c.isspace() for c in pattern):
        raise BackupError("Enter a valid image repository pattern, without a tag.")
    if len(paths) > 30 or len(excludes) > 100:
        raise BackupError("Too many paths or exclusions.")
    for path in paths:
        p = PurePosixPath(path)
        if (not path.startswith("/") or ".." in p.parts or str(p) != path or
                path in {"/", "/etc", "/var", "/home", "/app", "/usr", "/opt", "/root"} or
                p.parts[1] in {"proc", "sys", "dev", "bin", "sbin", "lib", "lib64", "usr"}):
            raise BackupError("Use specific absolute config paths, such as /config or /etc/app. System roots are not allowed.")
    for item in excludes:
        if item.startswith("/") or ".." in PurePosixPath(item).parts:
            raise BackupError("Exclusions must be relative to each config path.")


class Discovery:
    def __init__(self, client, store, host_root="/host", stacks_root="/opt/stacks"):
        self.client, self.store = client, store
        self.host_root = Path(host_root).resolve()
        self.stacks_root = stacks_root

    def host_path(self, path):
        p = PurePosixPath(path)
        if not p.is_absolute() or ".." in p.parts or (len(p.parts) > 1 and p.parts[1] in {"proc", "sys", "dev"}):
            raise BackupError("Invalid host file path.")
        target = self.host_root.joinpath(*p.parts[1:]).resolve()
        if not target.is_relative_to(self.host_root):
            raise BackupError("Host file resolves outside the read-only host mount.")
        return target

    def stacks(self):
        try:
            containers = self.client.containers.list(all=True)
        except Exception as exc:
            raise BackupError("Cannot reach Docker. Check the Docker socket mount.") from exc
        rules, stacks = self.store.rules(), {}
        roots = {self.stacks_root}
        for c in containers:
            labels = c.labels or {}
            env = dict(x.split("=", 1) for x in c.attrs["Config"].get("Env", []) if "=" in x)
            dockge_root = env.get("DOCKGE_STACKS_DIR")
            if dockge_root:
                for mount in c.attrs.get("Mounts", []):
                    destination = mount["Destination"].rstrip("/")
                    if dockge_root == destination or dockge_root.startswith(destination + "/"):
                        roots.add(mount["Source"] + dockge_root[len(destination):])
            name = labels.get("com.docker.compose.project")
            if not name:
                # Standalone containers are also visible, but require a Compose
                # file to be supplied before a reproducible stack backup can run.
                name = "standalone:" + c.name
            stack = stacks.setdefault(name, {"name": name, "containers": [], "compose_files": [], "working_dir": "", "excluded": False})
            if labels.get("cloud-backup.exclude", "").lower() == "true":
                stack["excluded"] = True
            workdir = labels.get("com.docker.compose.project.working_dir", "")
            if workdir:
                stack["working_dir"] = workdir
            for file in labels.get("com.docker.compose.project.config_files", "").split(","):
                if not file:
                    continue
                if not file.startswith("/") and workdir:
                    file = str(PurePosixPath(workdir) / file)
                if file not in stack["compose_files"]:
                    stack["compose_files"].append(file)
            image = c.attrs["Config"]["Image"]
            rule = match_rule(image, rules)
            stack["containers"].append({
                "id": c.id, "name": c.name, "image": image,
                "service": labels.get("com.docker.compose.service", c.name),
                "state": c.status, "rule": rule,
                "mounts": [{"type": m["Type"], "destination": m["Destination"]} for m in c.attrs.get("Mounts", [])],
            })
        # Discover stacks saved in Dockge even when no containers exist.
        for root in sorted(roots):
            try:
                directory = self.host_path(root)
                if not directory.is_dir():
                    continue
                for folder in sorted(directory.iterdir()):
                    if not folder.is_dir():
                        continue
                    for filename in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
                        if not (folder / filename).is_file():
                            continue
                        file = str(PurePosixPath(root) / folder.name / filename)
                        known = next((s for s in stacks.values() if file in s["compose_files"]), None)
                        if known:
                            break
                        try:
                            doc = yaml.safe_load((folder / filename).read_text()) or {}
                            name = doc.get("name") or folder.name
                            if not isinstance(name, str) or "$" in name:
                                name = folder.name
                        except (ValueError, yaml.YAMLError, OSError):
                            name = folder.name
                        if name not in stacks:
                            stacks[name] = {"name": name, "containers": [], "compose_files": [file],
                                            "working_dir": str(PurePosixPath(root) / folder.name), "excluded": False}
                        break
            except (OSError, BackupError):
                continue
        schedules = {s["stack"]: s for s in self.store.rows("SELECT * FROM schedules")}
        for stack in stacks.values():
            stack["schedule"] = schedules.get(stack["name"], {"cron": "0 3 * * *", "timezone": os.getenv("TZ", "UTC"), "enabled": False, "next_run": None})
            stack["issues"] = []
            if stack["excluded"]:
                stack["issues"].append("Excluded: contains the backup app or an explicitly excluded service.")
            if not stack["compose_files"]:
                stack["issues"].append("No Compose file found. Recreate this container as a Compose stack first.")
            if not stack["containers"]:
                stack["issues"].append("Inactive stack: create its containers before backing up config.")
            for c in stack["containers"]:
                if c["rule"] is None:
                    stack["issues"].append(f"Add a config mapping for {c['image']}.")
            for file in stack["compose_files"]:
                try:
                    if not self.host_path(file).is_file():
                        stack["issues"].append(f"Compose file is not readable: {file}")
                except BackupError as exc:
                    stack["issues"].append(str(exc))
        return sorted(stacks.values(), key=lambda s: s["name"])

    def get(self, name):
        stack = next((s for s in self.stacks() if s["name"] == name), None)
        if stack is None:
            raise BackupError("Stack no longer exists.")
        return stack

    def compose_bundle(self, stack):
        """Raw Compose + .env + explicitly referenced env/config/secret files only."""
        files = set(stack["compose_files"])
        workdir = PurePosixPath(stack["working_dir"] or str(PurePosixPath(stack["compose_files"][0]).parent))
        environment = {}
        dotenv = str(workdir / ".env")
        if self.host_path(dotenv).is_file():
            files.add(dotenv)
            for line in self.host_path(dotenv).read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    environment[key.strip()] = value.strip().strip("\"'")
        dependencies = {}

        def add_reference(value, required=True):
            if not isinstance(value, str):
                raise BackupError("Unsupported Compose file reference.")
            def expand(match):
                key, default = match.group(1), match.group(2)
                if key in environment:
                    return environment[key]
                if default is not None:
                    return default
                raise BackupError(f"Compose file path uses unresolved variable {key}; define it in the stack .env.")
            value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-(.*?))?\}", expand, value)
            if "$" in value:
                raise BackupError("Unsupported variable in a Compose file reference.")
            full = str(workdir / value) if not value.startswith("/") else value
            # Normalize ../ references, which Compose supports, before host checks.
            import posixpath
            full = posixpath.normpath(full)
            if self.host_path(full).is_file():
                files.add(full)
            elif required:
                raise BackupError(f"Referenced Compose file is missing: {full}")

        for file in stack["compose_files"]:
            try:
                doc = yaml.safe_load(self.host_path(file).read_text()) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise BackupError("Cannot read or parse a Compose file.") from exc
            if doc.get("include"):
                raise BackupError("Compose include requires an explicit flattened Compose file for backup.")
            for service, config in (doc.get("services") or {}).items():
                if config.get("extends"):
                    raise BackupError("Compose extends requires an explicit flattened Compose file for backup.")
                dependencies[service] = list(config.get("depends_on") or [])
                refs = config.get("env_file") or []
                if isinstance(refs, (str, dict)):
                    refs = [refs]
                for ref in refs:
                    add_reference(ref["path"] if isinstance(ref, dict) else ref,
                                  ref.get("required", True) if isinstance(ref, dict) else True)
            for kind in ("configs", "secrets"):
                for config in (doc.get(kind) or {}).values():
                    if isinstance(config, dict) and "file" in config:
                        add_reference(config["file"])
        for file in files:
            if not self.host_path(file).is_file():
                raise BackupError(f"Compose file is missing: {file}")
        return sorted(files), dependencies


def start_order(containers, dependencies):
    result, visiting, visited = [], set(), set()
    by_service = {}
    for c in containers:
        by_service.setdefault(c["service"], []).append(c)

    def visit(service):
        if service in visiting:
            raise BackupError("Circular Compose service dependencies.")
        if service in visited:
            return
        visiting.add(service)
        for dependency in dependencies.get(service, []):
            visit(dependency)
        visiting.remove(service)
        visited.add(service)
        result.extend(by_service.get(service, []))
    for service in by_service:
        visit(service)
    return result
