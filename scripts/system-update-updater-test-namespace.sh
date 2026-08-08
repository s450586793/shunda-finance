#!/usr/bin/bash
set -euo pipefail

if [ "${1:-}" = "--inside" ]; then
  shift
  rootfs="$1"
  fake_dir="$2"
  project_root="$3"
  target_uid="$4"
  target_gid="$5"
  test_batch="$6"
  mounted_paths=()

  cleanup_namespace() {
    local status=$?
    local cleanup_status=0
    local index
    trap - EXIT INT TERM HUP
    set +e
    for ((index=${#mounted_paths[@]} - 1; index >= 0; index--)); do
      /usr/bin/umount "${mounted_paths[index]}" || cleanup_status=1
    done
    if [ "$status" -ne 0 ]; then
      exit "$status"
    fi
    exit "$cleanup_status"
  }

  trap cleanup_namespace EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  /usr/bin/mount -t tmpfs -o mode=0755,nodev tmpfs "$rootfs"
  mounted_paths+=("$rootfs")

  /usr/bin/install -d -o root -g root -m 0755 \
    "$rootfs/.overlay/usr-upper" \
    "$rootfs/.overlay/usr-work" \
    "$rootfs/usr" \
    "$rootfs/etc" \
    "$rootfs/dev" \
    "$rootfs/proc" \
    "$rootfs/run" \
    "$rootfs/var" \
    "$rootfs/var/tmp" \
    "$rootfs/var/lib/sudo/lectured" \
    "$rootfs/var/log" \
    "$rootfs/tmp" \
    "$rootfs/home" \
    "$rootfs/volume4"
  /usr/bin/install -d -o root -g root -m 0700 "$rootfs/root"
  /usr/bin/install -d -o root -g root -m 0711 "$rootfs/run/sudo" "$rootfs/run/sudo/ts"
  /usr/bin/chmod 1777 "$rootfs/tmp" "$rootfs/var/tmp"
  /usr/bin/ln -s usr/bin "$rootfs/bin"
  /usr/bin/ln -s usr/sbin "$rootfs/sbin"
  /usr/bin/ln -s usr/lib "$rootfs/lib"
  /usr/bin/ln -s usr/lib64 "$rootfs/lib64"
  /usr/bin/ln -s /run "$rootfs/var/run"

  /usr/bin/mount -t overlay overlay \
    -o "lowerdir=/usr,upperdir=$rootfs/.overlay/usr-upper,workdir=$rootfs/.overlay/usr-work" \
    "$rootfs/usr"
  mounted_paths+=("$rootfs/usr")

  /usr/bin/mount --bind /etc "$rootfs/etc"
  mounted_paths+=("$rootfs/etc")
  /usr/bin/mount -o remount,bind,ro "$rootfs/etc"

  /usr/bin/mount -t tmpfs -o mode=0755,nosuid tmpfs "$rootfs/dev"
  mounted_paths+=("$rootfs/dev")
  for device_name in null zero random urandom tty; do
    : >"$rootfs/dev/$device_name"
    /usr/bin/mount --bind "/dev/$device_name" "$rootfs/dev/$device_name"
    mounted_paths+=("$rootfs/dev/$device_name")
  done
  /usr/bin/ln -s /proc/self/fd "$rootfs/dev/fd"
  /usr/bin/ln -s /proc/self/fd/0 "$rootfs/dev/stdin"
  /usr/bin/ln -s /proc/self/fd/1 "$rootfs/dev/stdout"
  /usr/bin/ln -s /proc/self/fd/2 "$rootfs/dev/stderr"

  /usr/bin/mount -t proc -o nosuid,nodev,noexec proc "$rootfs/proc"
  mounted_paths+=("$rootfs/proc")

  /usr/bin/install -d -o root -g root -m 0755 "$rootfs$project_root"
  /usr/bin/mount --bind "$project_root" "$rootfs$project_root"
  mounted_paths+=("$rootfs$project_root")
  /usr/bin/mount -o remount,bind,ro "$rootfs$project_root"

  real_python="$(/usr/bin/readlink -f /usr/bin/python3)"
  /usr/bin/install -d -o root -g root -m 0711 "$rootfs/volume4/.shunda-test-bin"
  /usr/bin/install -o root -g root -m 0755 "$real_python" "$rootfs/volume4/.shunda-test-bin/python3"
  /usr/bin/install -o root -g root -m 0755 /usr/bin/mktemp "$rootfs/volume4/.shunda-test-bin/mktemp"
  /usr/bin/install -o root -g root -m 0755 /usr/bin/rm "$rootfs/volume4/.shunda-test-bin/rm"
  /usr/bin/rm -f \
    "$rootfs/usr/bin/python3" \
    "$rootfs/usr/local/bin/python3" \
    "$rootfs/usr/bin/mktemp" \
    "$rootfs/usr/bin/sleep" \
    "$rootfs/usr/bin/rm" \
    "$rootfs/usr/bin/docker" \
    "$rootfs/usr/local/bin/docker" \
    "$rootfs/usr/local/lib/docker/cli-plugins/docker-compose" \
    "$rootfs/usr/local/libexec/docker/cli-plugins/docker-compose" \
    "$rootfs/usr/lib/docker/cli-plugins/docker-compose" \
    "$rootfs/usr/libexec/docker/cli-plugins/docker-compose"
  /usr/bin/install -o root -g root -m 0755 "$fake_dir/python3" "$rootfs/usr/bin/python3"
  /usr/bin/install -o root -g root -m 0755 "$fake_dir/python3" "$rootfs/usr/local/bin/python3"
  if [ "$test_batch" = "LOCK" ]; then
    /usr/bin/install -o root -g root -m 0755 "$fake_dir/mktemp" "$rootfs/usr/bin/mktemp"
  else
    /usr/bin/install -o root -g root -m 0755 /usr/bin/mktemp "$rootfs/usr/bin/mktemp"
  fi
  /usr/bin/install -o root -g root -m 0755 "$fake_dir/sleep" "$rootfs/usr/bin/sleep"
  /usr/bin/install -o root -g root -m 0755 "$fake_dir/rm" "$rootfs/usr/bin/rm"
  /usr/bin/install -o root -g root -m 0755 "$fake_dir/docker" "$rootfs/usr/bin/docker"
  /usr/bin/install -o root -g root -m 0755 "$fake_dir/docker" "$rootfs/usr/local/bin/docker"
  /usr/bin/install -d -o root -g root -m 0755 "$rootfs/usr/lib/docker/cli-plugins"
  /usr/bin/install \
    -o root \
    -g root \
    -m 0755 \
    "$fake_dir/docker" \
    "$rootfs/usr/lib/docker/cli-plugins/docker-compose"
  docker_smoke_paths=(/usr/bin/docker /usr/local/bin/docker)
  docker_candidates=(/usr/bin/docker /usr/local/bin/docker)
  if [ "$test_batch" = "TCB" ]; then
    /usr/bin/mv "$rootfs/usr/local/bin/python3" "$rootfs/usr/local/bin/python3-real"
    /usr/bin/ln -s python3-real "$rootfs/usr/local/bin/python3"
    /usr/bin/install \
      -o "$target_uid" \
      -g "$target_gid" \
      -m 0755 \
      "$fake_dir/untrusted-python3" \
      "$rootfs/usr/bin/python3"
  elif [ "$test_batch" = "DSM_TCB" ]; then
    /usr/bin/mv "$rootfs/usr/bin/python3" "$rootfs/usr/bin/python3.8"
    /usr/bin/ln -s python3.8 "$rootfs/usr/bin/python3"
    /usr/bin/rm -f "$rootfs/usr/bin/docker" "$rootfs/usr/local/bin/docker"
    /usr/bin/install -d -o root -g root -m 0755 \
      "$rootfs/var/packages" \
      "$rootfs/var/packages/ContainerManager" \
      "$rootfs/volume4/@appstore" \
      "$rootfs/volume4/@appstore/ContainerManager" \
      "$rootfs/volume4/@appstore/ContainerManager/usr" \
      "$rootfs/volume4/@appstore/ContainerManager/usr/bin"
    /usr/bin/install -o root -g root -m 0755 \
      "$fake_dir/docker" \
      "$rootfs/volume4/@appstore/ContainerManager/usr/bin/docker"
    /usr/bin/ln -s \
      /volume4/@appstore/ContainerManager \
      "$rootfs/var/packages/ContainerManager/target"
    /usr/bin/ln -s \
      /var/packages/ContainerManager/target/usr/bin/docker \
      "$rootfs/usr/local/bin/docker"
    docker_smoke_paths=(
      /volume4/@appstore/ContainerManager/usr/bin/docker
      /volume4/@appstore/ContainerManager/usr/bin/docker
    )
    docker_candidates=(/usr/local/bin/docker)
  fi
  /usr/bin/cmp -s "$fake_dir/docker" "$rootfs${docker_smoke_paths[0]}"
  /usr/bin/cmp -s "$fake_dir/docker" "$rootfs${docker_smoke_paths[1]}"

  /usr/bin/install -d -o root -g root -m 0700 "$rootfs/volume4/.shunda-updater-fake"
  : >"$rootfs/run/docker.sock"
  /usr/bin/chown root:root "$rootfs/run/docker.sock"
  /usr/bin/chmod 000 "$rootfs/run/docker.sock"

  read -r \
    smoke_marker \
    usr_docker_identity \
    local_docker_identity \
    scenario_identity \
    log_identity \
    state_identity <<<"$(
    /usr/sbin/chroot "$rootfs" /volume4/.shunda-test-bin/python3 -I -S - \
      /volume4/.shunda-updater-fake \
      /var/run/docker.sock \
      "${docker_smoke_paths[@]}" <<'PY'
import json
import os
import secrets
import stat
import sys
from pathlib import Path

control_path = Path(sys.argv[1])
socket_path = Path(sys.argv[2])
docker_paths = [Path(value) for value in sys.argv[3:]]
marker = secrets.token_hex(32)
identities = []
for path in docker_paths:
    linked = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o755
        or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
    ):
        raise SystemExit(1)
    identities.append(f"{opened.st_dev}:{opened.st_ino}")

socket_stat = socket_path.lstat()
if (
    not stat.S_ISREG(socket_stat.st_mode)
    or socket_stat.st_uid != 0
    or socket_stat.st_gid != 0
    or stat.S_IMODE(socket_stat.st_mode) != 0
):
    raise SystemExit(1)

payloads = {
    "scenario": b"success",
    "docker.log": b"",
    "docker-state.json": json.dumps({"up": 0, "marker": marker}, sort_keys=True).encode("ascii"),
}
for name, payload in payloads.items():
    descriptor = os.open(
        control_path / name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
        value = os.fstat(descriptor)
        identities.append(f"{value.st_dev}:{value.st_ino}")
    finally:
        os.close(descriptor)
directory_descriptor = os.open(control_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
print(marker, *identities)
PY
  )"

  for docker_candidate in "${docker_candidates[@]}"; do
    /usr/sbin/chroot "$rootfs" /usr/bin/env -i \
      PATH=/usr/bin:/usr/local/bin:/bin \
      SHUNDA_FAKE_DOCKER_PREFLIGHT=1 \
      "$docker_candidate" \
      --host unix:///var/run/docker.sock \
      pull ghcr.io/s450586793/shunda-finance-updater:v1.2.4 \
      >/dev/null 2>&1
  done

  /usr/sbin/chroot "$rootfs" /volume4/.shunda-test-bin/python3 -I -S - \
    /volume4/.shunda-updater-fake \
    "$smoke_marker" \
    "$usr_docker_identity" \
    "$local_docker_identity" \
    "$scenario_identity" \
    "$log_identity" \
    "$state_identity" \
    "${docker_smoke_paths[@]}" \
    /var/run/docker.sock \
    "${#docker_candidates[@]}" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

control_path = Path(sys.argv[1])
marker = sys.argv[2]
expected_docker_identities = sys.argv[3:5]
names = ("scenario", "docker.log", "docker-state.json")
expected_control_identities = dict(zip(names, sys.argv[5:8], strict=True))
docker_paths = [Path(value) for value in sys.argv[8:10]]
socket_path = Path(sys.argv[10])
docker_call_count = int(sys.argv[11])
expected_payloads = {
    "scenario": b"success",
    "docker.log": b'{"action": "pull"}\n' * docker_call_count,
    "docker-state.json": json.dumps({"up": 0, "marker": marker}, sort_keys=True).encode("ascii"),
}

for path, expected_identity in zip(docker_paths, expected_docker_identities, strict=True):
    expected_device, expected_inode = (int(value) for value in expected_identity.split(":"))
    linked = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o755
        or (opened.st_dev, opened.st_ino) != (expected_device, expected_inode)
        or (linked.st_dev, linked.st_ino) != (expected_device, expected_inode)
    ):
        raise SystemExit(1)

socket_stat = socket_path.lstat()
if (
    not stat.S_ISREG(socket_stat.st_mode)
    or socket_stat.st_uid != 0
    or socket_stat.st_gid != 0
    or stat.S_IMODE(socket_stat.st_mode) != 0
):
    raise SystemExit(1)

directory_descriptor = os.open(control_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    for name in names:
        expected_device, expected_inode = (
            int(value) for value in expected_control_identities[name].split(":")
        )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            payload = os.read(descriptor, len(expected_payloads[name]) + 1)
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (expected_device, expected_inode)
            or (linked.st_dev, linked.st_ino) != (expected_device, expected_inode)
            or payload != expected_payloads[name]
        ):
            raise SystemExit(1)
        os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY

  /usr/sbin/chroot "$rootfs" /volume4/.shunda-test-bin/python3 -I -S - \
    /volume4/.shunda-updater-fake \
    "$target_uid" \
    "$target_gid" <<'PY'
import os
import sys
from pathlib import Path

control_path = Path(sys.argv[1])
uid = int(sys.argv[2])
gid = int(sys.argv[3])
payloads = {
    "scenario": b"",
    "docker.log": b"",
    "docker-state.json": b'{"up":0}',
}
for name, payload in payloads.items():
    descriptor = os.open(
        control_path / name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
directory_descriptor = os.open(control_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
  /usr/bin/chown "$target_uid:$target_gid" "$rootfs/volume4/.shunda-updater-fake"
  /usr/bin/chmod 0700 "$rootfs/volume4/.shunda-updater-fake"

  set +e
  (
    cd /
    /usr/sbin/chroot "$rootfs" \
      /usr/bin/setpriv \
      --reuid="$target_uid" \
      --regid="$target_gid" \
      --init-groups \
      /usr/bin/env -i \
        HOME=/tmp \
        PATH=/usr/bin:/usr/local/bin:/bin \
        SHUNDA_TEST_NAMESPACE=1 \
        SHUNDA_TEST_PRIVATE_ROOTFS=1 \
        SHUNDA_TEST_BATCH="$test_batch" \
        /usr/bin/bash "$project_root/scripts/system-update-updater.test.sh"
  )
  test_status=$?
  set -e
  exit "$test_status"
fi

[ "$#" -eq 6 ] || exit 64
suite_dir="$1"
fake_dir="$2"
project_root="$3"
target_uid="$4"
target_gid="$5"
test_batch="$6"
helper_path="$(/usr/bin/readlink -f "$0")"

/usr/bin/sudo -n /usr/bin/env -i PATH=/usr/bin:/bin \
  /usr/bin/python3 -I -S - \
  "$helper_path" \
  "$suite_dir" \
  "$fake_dir" \
  "$project_root" \
  "$target_uid" \
  "$target_gid" \
  "$test_batch" <<'PY'
import fcntl
import os
import secrets
import stat
import sys
import time
from pathlib import Path

helper_path, suite_dir, fake_dir, project_root, uid, gid, test_batch = sys.argv[1:]
lock_path = Path("/run/shunda-system-update-updater-test.lock")
marker_name = ".shunda-updater-rootfs-owner"
marker_value = secrets.token_hex(32).encode("ascii")
lock_descriptor = -1
marker_inode = None
rootfs_inode = None
rootfs_path = None
exit_status = 1
host_volume_seen = False


def safe_root_directory(path: Path) -> os.stat_result:
    value = path.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != 0
        or stat.S_IMODE(value.st_mode) & 0o022
    ):
        raise RuntimeError("unsafe root directory")
    return value


try:
    safe_root_directory(Path("/run"))
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    lock_lstat = lock_path.lstat()
    lock_fstat = os.fstat(lock_descriptor)
    if (
        not stat.S_ISREG(lock_fstat.st_mode)
        or lock_fstat.st_uid != 0
        or lock_fstat.st_gid != 0
        or stat.S_IMODE(lock_fstat.st_mode) != 0o600
        or lock_lstat.st_dev != lock_fstat.st_dev
        or lock_lstat.st_ino != lock_fstat.st_ino
    ):
        raise RuntimeError("unsafe lock")
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock_descriptor, True)

    try:
        Path("/volume4").lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("host /volume4 already exists")

    while rootfs_path is None:
        candidate = Path("/run") / f"shunda-system-update-updater-rootfs.{secrets.token_hex(16)}"
        try:
            os.mkdir(candidate, 0o700)
        except FileExistsError:
            continue
        rootfs_path = candidate

    rootfs_stat = rootfs_path.lstat()
    rootfs_inode = (rootfs_stat.st_dev, rootfs_stat.st_ino)
    marker_path = rootfs_path / marker_name
    marker_descriptor = os.open(
        marker_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(marker_descriptor, marker_value)
        os.fsync(marker_descriptor)
        marker_stat = os.fstat(marker_descriptor)
        marker_inode = (marker_stat.st_dev, marker_stat.st_ino)
    finally:
        os.close(marker_descriptor)
    run_descriptor = os.open("/run", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(run_descriptor)
    finally:
        os.close(run_descriptor)

    child = os.fork()
    if child == 0:
        os.execve(
            "/usr/bin/unshare",
            [
                "/usr/bin/unshare",
                "--mount",
                "--propagation",
                "private",
                "/usr/bin/bash",
                helper_path,
                "--inside",
                str(rootfs_path),
                fake_dir,
                project_root,
                uid,
                gid,
                test_batch,
            ],
            {"PATH": "/usr/bin:/bin", "HOME": "/root"},
        )

    while True:
        try:
            Path("/volume4").lstat()
        except FileNotFoundError:
            pass
        else:
            host_volume_seen = True
        completed, child_status = os.waitpid(child, os.WNOHANG)
        if completed == child:
            value = os.waitstatus_to_exitcode(child_status)
            exit_status = value if value >= 0 else 128 - value
            break
        time.sleep(0.001)
finally:
    cleanup_safe = False
    if rootfs_path is not None and rootfs_inode is not None and marker_inode is not None:
        try:
            current_rootfs = rootfs_path.lstat()
            marker_path = rootfs_path / marker_name
            linked_marker = marker_path.lstat()
            marker_descriptor = os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                opened_marker = os.fstat(marker_descriptor)
                marker_payload = os.read(marker_descriptor, len(marker_value) + 1)
            finally:
                os.close(marker_descriptor)
            cleanup_safe = (
                stat.S_ISDIR(current_rootfs.st_mode)
                and current_rootfs.st_uid == 0
                and current_rootfs.st_gid == 0
                and stat.S_IMODE(current_rootfs.st_mode) == 0o700
                and (current_rootfs.st_dev, current_rootfs.st_ino) == rootfs_inode
                and stat.S_ISREG(opened_marker.st_mode)
                and opened_marker.st_uid == 0
                and opened_marker.st_gid == 0
                and stat.S_IMODE(opened_marker.st_mode) == 0o600
                and (opened_marker.st_dev, opened_marker.st_ino) == marker_inode
                and (linked_marker.st_dev, linked_marker.st_ino) == marker_inode
                and marker_payload == marker_value
                and {entry.name for entry in os.scandir(rootfs_path)} == {marker_name}
            )
        except OSError:
            cleanup_safe = False
        if cleanup_safe:
            os.unlink(marker_path)
            os.rmdir(rootfs_path)
            run_descriptor = os.open("/run", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(run_descriptor)
            finally:
                os.close(run_descriptor)
    if lock_descriptor >= 0:
        os.close(lock_descriptor)

if not cleanup_safe:
    raise SystemExit(90)
if host_volume_seen:
    raise SystemExit("namespace launcher created host /volume4")
raise SystemExit(exit_status)
PY
