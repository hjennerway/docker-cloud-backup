import base64
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import stat
import time
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
        self._size_cache = {}

    def check_path(self, container_id, name, path):
        """Check existence through Docker HEAD without downloading live data."""
        try:
            api = self.client.api
            response = api.head(api._url('/containers/{0}/archive', container_id), params={"path": path}, timeout=30)
            try:
                api._raise_for_status(response)
                header = getattr(response, "headers", {}).get("X-Docker-Container-Path-Stat")
                try:
                    metadata = json.loads(base64.b64decode(header)) if header else {}
                except (ValueError, TypeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
            finally:
                response.close()
        except Exception:
            raise BackupError(f"Config path {path} is missing or not readable in {name}. Update its backup selection or image mapping.") from None
        return metadata

    def validate_mapping(self, rule):
        rules = [r for r in self.store.rules() if r["pattern"] != rule["pattern"]] + [rule]
        try:
            containers = self.client.containers.list(all=True)
        except Exception:
            raise BackupError("Cannot validate config paths while Docker is unavailable.") from None
        checked = 0
        for container in containers:
            if match_rule(container.attrs["Config"]["Image"], rules) != rule:
                continue
            for path in rule["paths"]:
                self.check_path(container.id, container.name, path)
            checked += 1
        return checked

    def volume_size(self, source, excludes=()):
        """Logical file bytes; never follow symlinks or download container data.

        Bound scans and cache them so dashboard polling cannot repeatedly walk
        large media mounts. An incomplete/unreadable scan is unknown, never zero.
        """
        if not source:
            return None
        key = (source, tuple(excludes))
        now = time.monotonic()
        cached = self._size_cache.get(key)
        if cached and now - cached[0] < 60:
            return cached[1]
        size, count = 0, 0
        try:
            root = self.host_path(source)
            pending = [root]
            while pending:
                path = pending.pop()
                count += 1
                if count > 100000 or time.monotonic() - now > 1:
                    raise OSError("Size scan limit reached")
                relative = path.relative_to(root).as_posix() if path != root else ""
                if self.excluded(relative, excludes):
                    continue
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    size += info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as entries:
                        for entry in entries:
                            if len(pending) + count > 100000 or time.monotonic() - now > 1:
                                raise OSError("Size scan limit reached")
                            pending.append(Path(entry.path))
        except (OSError, BackupError):
            size = None
        if len(self._size_cache) > 1000:
            self._size_cache.clear()
        self._size_cache[key] = (time.monotonic(), size)
        return size

    @staticmethod
    def excluded(relative, excludes):
        return any(fnmatch.fnmatchcase(relative, pattern) or
                   (pattern.endswith("/**") and relative == pattern[:-3]) for pattern in excludes)

    def container_size(self, container, path, excludes=(), metadata=None):
        """Estimate only the selected files, including config outside mounts.

        Prefer the read-only host mount. For running containers, a bounded,
        shell-free metadata scan is a fallback; never download live file data
        or start a stopped container just to display a size.
        """
        covering = sorted((m for m in container["mounts"] if
                           path == m["destination"] or path.startswith(m["destination"].rstrip("/") + "/")),
                          key=lambda m: len(m["destination"]), reverse=True)
        # A nested mount has its own source, rather than the parent's host files.
        nested = [m for m in container["mounts"] if m["destination"].startswith(path.rstrip("/") + "/")]
        included_nested = [m for m in nested if not self.excluded(m["destination"][len(path.rstrip("/")) + 1:], excludes)]
        if covering and not included_nested and covering[0]["source"]:
            mount = covering[0]
            suffix = path[len(mount["destination"].rstrip("/")):].lstrip("/")
            size = self.volume_size(str(PurePosixPath(mount["source"]) / suffix), excludes)
            if size is not None:
                return size

        # Docker uses Go FileMode bits, rather than POSIX stat type bits.
        file_types = sum(1 << bit for bit in (31, 27, 26, 25, 24, 21, 19))
        if metadata and isinstance(metadata.get("mode"), int) and not metadata["mode"] & file_types:
            size = metadata.get("size")
            if isinstance(size, int) and size >= 0:
                return size

        if container["state"] != "running":
            return None
        key = ("container", container["id"], path, tuple(excludes))
        now = time.monotonic()
        cached = self._size_cache.get(key)
        if cached and now - cached[0] < 60:
            return cached[1]
        size = None
        try:
            result = self.client.containers.get(container["id"]).exec_run(
                ["timeout", "-s", "KILL", "2", "find", path, "-type", "f", "-exec",
                 "stat", "-c", "%s %n", "--", "{}", "+"], stderr=False)
            if result.exit_code != 0:
                raise ValueError("Metadata scan unavailable")
            lines = result.output.decode("utf-8").splitlines()
            if len(lines) > 100000:
                raise ValueError("Size scan limit reached")
            size = 0
            for line in lines:
                value, filename = line.split(" ", 1)
                if filename != path and not filename.startswith(path.rstrip("/") + "/"):
                    raise ValueError("Invalid metadata path")
                relative = filename[len(path.rstrip("/")) + 1:] if filename != path else ""
                amount = int(value)
                if amount < 0:
                    raise ValueError("Invalid metadata size")
                if not self.excluded(relative, excludes):
                    size += amount
        except Exception:
            size = None
        if len(self._size_cache) > 1000:
            self._size_cache.clear()
        self._size_cache[key] = (time.monotonic(), size)
        return size

    def backup_size(self, stack):
        """Approximate uncompressed selected contents plus Compose inputs."""
        if not stack["compose_files"]:
            return None
        sizes = [item.get("size_bytes") for c in stack["containers"] for item in c["backup_paths"]]
        try:
            files, _ = self.compose_bundle(stack)
            sizes.extend(self.volume_size(file) for file in files)
        except (BackupError, OSError, ValueError, TypeError, AttributeError):
            return None
        return None if any(size is None for size in sizes) else sum(sizes)

    def stack_volumes(self, stack, include_sizes=True):
        volumes = {}
        for container in stack["containers"]:
            for mount in container["mounts"]:
                if mount["type"] not in {"volume", "bind"}:
                    continue
                source = mount.get("source", "")
                identity = [mount["type"], mount.get("name", ""), source]
                if not source and not mount.get("name"):
                    identity += [container["service"], mount["destination"]]
                key = hashlib.sha256(repr(identity).encode()).hexdigest()
                volume = volumes.setdefault(key, {
                    "id": key, "name": mount.get("name") or source or mount["destination"],
                    "type": mount["type"], "source": source, "mounts": [],
                })
                volume["mounts"].append({"container_id": container["id"], "container": container["name"],
                                         "service": container["service"], "destination": mount["destination"]})
        for volume in volumes.values():
            volume["size_bytes"] = self.volume_size(volume["source"]) if include_sizes else None
        return sorted(volumes.values(), key=lambda v: (v["name"], v["id"]))

    def configure_stack(self, stack, selection, include_sizes=False):
        """Resolve persisted choices against current containers, failing closed."""
        stack["backup_selection"] = selection
        issues = []
        containers = {c["id"]: c for c in stack["containers"]}
        for container in containers.values():
            container["backup_paths"] = []
        mode = selection["mode"]
        if mode == "auto":
            for container in containers.values():
                rule = container["rule"]
                if rule is None:
                    issues.append(f"Choose volumes, config files or an image mapping for {container['image']}.")
                else:
                    container["backup_paths"] = [{"path": path, "excludes": rule["excludes"], "kind": "config"}
                                                 for path in rule["paths"]]
        elif mode == "manual":
            services = {c["service"] for c in containers.values()}
            for service in selection["paths"]:
                if selection["paths"][service] and service not in services:
                    issues.append(f"Selected service {service} no longer exists. Update the backup selection.")
            if not any(selection["paths"].values()):
                issues.append("Enter at least one config file or folder to back up.")
            for container in containers.values():
                container["backup_paths"] = [{"path": path, "excludes": [], "kind": "config"}
                                             for path in dict.fromkeys(selection["paths"].get(container["service"], []))]
        elif mode == "volumes":
            available = {v["id"]: v for v in stack["volumes"]}
            if not selection["volumes"]:
                issues.append("Select at least one volume to back up.")
            for key in dict.fromkeys(selection["volumes"]):
                if key not in available:
                    issues.append("A selected volume is no longer mounted. Update the backup selection.")
                    continue
                mount = available[key]["mounts"][0]
                path = mount["destination"]
                if path == "/" or PurePosixPath(path).parts[1] in {"proc", "sys", "dev"}:
                    issues.append(f"System mount {path} cannot be backed up as a volume.")
                    continue
                containers[mount["container_id"]]["backup_paths"].append({
                    "path": path, "kind": "volume", "volume_id": key,
                    "excludes": [m["destination"][len(path) + 1:] + "/**"
                                 for m in containers[mount["container_id"]]["mounts"]
                                 if m["destination"].startswith(path + "/")],
                })
        else:
            issues.append("Unknown backup selection mode.")
        for container in containers.values():
            for item in container["backup_paths"]:
                try:
                    metadata = self.check_path(container["id"], container["name"], item["path"])
                    if include_sizes:
                        item["size_bytes"] = self.container_size(container, item["path"], item["excludes"], metadata)
                except BackupError as exc:
                    issues.append(str(exc))
        return issues

    def host_path(self, path):
        p = PurePosixPath(path)
        if not p.is_absolute() or ".." in p.parts or (len(p.parts) > 1 and p.parts[1] in {"proc", "sys", "dev"}):
            raise BackupError("Invalid host file path.")
        target = self.host_root.joinpath(*p.parts[1:]).resolve()
        if not target.is_relative_to(self.host_root):
            raise BackupError("Host file resolves outside the read-only host mount.")
        return target

    def stacks(self, include_sizes=True, stack_name=None):
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
                "mounts": [{"type": m["Type"], "destination": m["Destination"],
                            "source": m.get("Source", ""), "name": m.get("Name", "")}
                           for m in c.attrs.get("Mounts", [])],
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
        if stack_name is not None:
            stacks = {name: stack for name, stack in stacks.items() if name == stack_name}
        schedules = {s["stack"]: s for s in self.store.rows("SELECT * FROM schedules")}
        manual_stacks = self.store.manual_backup_stacks()
        for stack in stacks.values():
            stack["schedule"] = schedules.get(stack["name"], {"cron": "0 3 * * *", "timezone": os.getenv("TZ", "UTC"), "enabled": False, "next_run": None})
            stack["include_in_manual_backup"] = stack["name"] in manual_stacks and not stack["excluded"]
            stack["issues"] = []
            if stack["excluded"]:
                stack["issues"].append("Excluded: contains the backup app or an explicitly excluded service.")
            if not stack["compose_files"]:
                stack["issues"].append("No Compose file found. Recreate this container as a Compose stack first.")
            if not stack["containers"]:
                stack["issues"].append("Inactive stack: create its containers before backing up config.")
            stack["volumes"] = self.stack_volumes(stack, include_sizes)
            selection_issues = self.configure_stack(stack, self.store.backup_selection(stack["name"]), include_sizes)
            stack["issues"].extend(selection_issues)
            stack["backup_size_bytes"] = self.backup_size(stack) if include_sizes and not selection_issues else None
            for file in stack["compose_files"]:
                try:
                    if not self.host_path(file).is_file():
                        stack["issues"].append(f"Compose file is not readable: {file}")
                except BackupError as exc:
                    stack["issues"].append(str(exc))
        return sorted(stacks.values(), key=lambda s: s["name"])

    def get(self, name):
        stack = next(iter(self.stacks(include_sizes=False, stack_name=name)), None)
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
