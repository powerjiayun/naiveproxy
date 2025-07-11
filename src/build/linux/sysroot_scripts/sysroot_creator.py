#!/usr/bin/env python3
# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""
This script is used to build Debian sysroot images for building Chromium.
"""

import argparse
import collections
import hashlib
import lzma
import os
import re
import shutil
import subprocess
import tempfile
import time

import requests
import reversion_glibc

DISTRO = "debian"
RELEASES = {
    "amd64": "bullseye",
    "i386": "bullseye",
    "armhf": "bullseye",
    "arm64": "bullseye",
    "mipsel": "bullseye",
    "mips64el": "bullseye",
    "ppc64el": "bullseye",
    "riscv64": "trixie",
    "loong64": "sid",
}

GCC_VERSIONS = {
    "bullseye": 10,
    "trixie": 12,
    "sid": 13,
}


# This number is appended to the sysroot key to cause full rebuilds.  It
# should be incremented when removing packages or patching existing packages.
# It should not be incremented when adding packages.
SYSROOT_RELEASE = 1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CHROME_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

# gpg keyring file generated using generate_keyring.sh
KEYRING_FILE = os.path.join(SCRIPT_DIR, "keyring.gpg")

ARCHIVE_TIMESTAMP = "20250129T203412Z"

ARCHIVE_URL = f"https://snapshot.debian.org/archive/debian/{ARCHIVE_TIMESTAMP}/"
APT_SOURCES_LIST = [
    # This mimics a sources.list from bullseye.
    ("bullseye", ["main", "contrib", "non-free"]),
    ("bullseye-updates", ["main", "contrib", "non-free"]),
    ("bullseye-backports", ["main", "contrib", "non-free"]),
]
APT_SOURCES_LIST_RISCV = [("trixie", ["main", "contrib"])]
APT_SOURCES_LISTS = {
    "amd64": APT_SOURCES_LIST,
    "i386": APT_SOURCES_LIST,
    "armhf": APT_SOURCES_LIST,
    "arm64": APT_SOURCES_LIST,
    "mipsel": APT_SOURCES_LIST,
    "mips64el": APT_SOURCES_LIST,
    "ppc64el": APT_SOURCES_LIST,
    "riscv64": APT_SOURCES_LIST_RISCV,
    "loong64": [("sid", ["main"])],
}

TRIPLES = {
    "amd64": "x86_64-linux-gnu",
    "i386": "i386-linux-gnu",
    "armhf": "arm-linux-gnueabihf",
    "arm64": "aarch64-linux-gnu",
    "mipsel": "mipsel-linux-gnu",
    "mips64el": "mips64el-linux-gnuabi64",
    "ppc64el": "powerpc64le-linux-gnu",
    "riscv64": "riscv64-linux-gnu",
    "loong64": "loongarch64-linux-gnu",
}

LIB_DIRS = {
    "bullseye": "lib",
    "trixie": "usr/lib",
}

REQUIRED_TOOLS = [
    "dpkg-deb",
    "file",
    "gpgv",
    "readelf",
    "tar",
    "xz",
]

# Package configuration
PACKAGES_EXT = "xz"
RELEASE_FILE = "Release"
RELEASE_FILE_GPG = "Release.gpg"

# List of development packages. Dependencies are automatically included.
DEBIAN_PACKAGES = [
    "libc6-dev",
]


def banner(message: str) -> None:
    print("#" * 70)
    print(message)
    print("#" * 70)


def sub_banner(message: str) -> None:
    print("-" * 70)
    print(message)
    print("-" * 70)


def hash_file(hasher, file_name: str) -> str:
    with open(file_name, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_copyfile(source: str, destination: str) -> None:
    dest_dir = os.path.dirname(destination)
    with tempfile.NamedTemporaryFile(mode="wb", delete=False,
                                     dir=dest_dir) as temp_file:
        temp_filename = temp_file.name
    shutil.copyfile(source, temp_filename)
    os.rename(temp_filename, destination)


def download_or_copy_non_unique_filename(url: str, dest: str) -> None:
    """
    Downloads a file from a given URL to a destination with a unique filename,
    based on the SHA-256 hash of the URL.
    """
    hash_digest = hashlib.sha256(url.encode()).hexdigest()
    unique_dest = f"{dest}.{hash_digest}"
    download_or_copy(url, unique_dest)
    atomic_copyfile(unique_dest, dest)


def download_or_copy(source: str, destination: str) -> None:
    """
    Downloads a file from the given URL or copies it from a local path to the
    specified destination.
    """
    if os.path.exists(destination):
        print(f"{destination} already in place")
        return

    if source.startswith(("http://", "https://")):
        download_file(source, destination)
    else:
        atomic_copyfile(source, destination)


def download_file(url: str, dest: str, retries=5) -> None:
    """
    Downloads a file from a URL to a specified destination with retry logic,
    directory creation, and atomic write.
    """
    print(f"Downloading from {url} -> {dest}")
    # Create directories if they don't exist
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    for attempt in range(retries):
        try:
            with requests.get(url, stream=True) as response:
                response.raise_for_status()

                # Use a temporary file to write data
                with tempfile.NamedTemporaryFile(
                        mode="wb", delete=False,
                        dir=os.path.dirname(dest)) as temp_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)

                # Rename temporary file to destination file
                os.rename(temp_file.name, dest)
                print(f"Downloaded {dest}")
                break

        except requests.RequestException as e:
            print(f"Attempt {attempt} failed: {e}")
            # Exponential back-off
            time.sleep(2**attempt)
    else:
        raise Exception(f"Failed to download file after {retries} attempts")


def sanity_check(build_dir: str) -> None:
    """
    Performs sanity checks to ensure the environment is correctly set up.
    """
    banner("Sanity Checks")

    # Determine the Chrome build directory
    os.makedirs(build_dir, exist_ok=True)
    print(f"Using build directory: {build_dir}")

    # Check for required tools
    missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]
    if missing:
        raise Exception(f"Required tools not found: {', '.join(missing)}")


def clear_install_dir(install_root: str) -> None:
    if os.path.exists(install_root):
        shutil.rmtree(install_root)
    os.makedirs(install_root)


def create_tarball(install_root: str, arch: str, build_dir: str) -> None:
    tarball_path = os.path.join(
        build_dir, f"{DISTRO}_{RELEASES[arch]}_{arch}_sysroot.tar.xz")
    banner("Creating tarball " + tarball_path)
    command = [
        "tar",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--sort=name",
        "--no-xattrs",
        "-I",
        "xz -z9 -T0 --lzma2='dict=256MiB'",
        "-cf",
        tarball_path,
        "-C",
        install_root,
        ".",
    ]
    subprocess.run(command, check=True)


def generate_package_list_dist_repo(arch: str, dist: str, repo_name: str,
                                    build_dir: str) -> list[dict[str, str]]:
    repo_basedir = f"{ARCHIVE_URL}/dists/{dist}"
    package_list = f"{build_dir}/Packages.{dist}_{repo_name}_{arch}"
    package_list = f"{package_list}.{PACKAGES_EXT}"
    package_file_arch = f"{repo_name}/binary-{arch}/Packages.{PACKAGES_EXT}"
    package_list_arch = f"{repo_basedir}/{package_file_arch}"

    download_or_copy_non_unique_filename(package_list_arch, package_list)
    verify_package_listing(package_file_arch, package_list, dist, build_dir)

    # `not line.endswith(":")` is added here to handle the case of
    # "X-Cargo-Built-Using:\n rust-adler (= 1.0.2-2), ..."
    with lzma.open(package_list, "rt") as src:
        return [
            dict(
                line.split(": ", 1) for line in package_meta.splitlines()
                if not line.startswith(" ") and not line.endswith(":"))
            for package_meta in src.read().split("\n\n") if package_meta
        ]


def generate_package_list(arch: str, build_dir: str) -> dict[str, str]:
    # Workaround for some misconfigured package dependencies.
    BROKEN_DEPS = {
        "libgcc1",
        "qt6-base-abi",
        "libc-dev",  # pulls in a newer libc6-dev
    }

    package_meta = {}
    sources = APT_SOURCES_LISTS[arch]
    for dist, repos in sources:
        for repo_name in repos:
            for meta in generate_package_list_dist_repo(
                    arch, dist, repo_name, build_dir):
                package_meta[meta["Package"]] = meta
                if "Provides" not in meta:
                    continue
                for provides in meta["Provides"].split(", "):
                    # Strip version requirements
                    provides = provides.split()[0]
                    if provides in package_meta:
                        continue
                    package_meta[provides] = meta

    def add_package_dependencies(package: str) -> None:
        if package in BROKEN_DEPS:
            return
        meta = package_meta[package]
        url = ARCHIVE_URL + meta["Filename"]
        if url in package_dict:
            return
        package_dict[url] = meta["SHA256"]
        if "Depends" in meta:
            for dep in meta["Depends"].split(", "):
                add_package_dependencies(dep.split()[0].split(":")[0])

    # Read the input file and create a dictionary mapping package names to URLs
    # and checksums.
    missing = set(DEBIAN_PACKAGES)
    # Add corresponding libstdc++-dev package (needed for trixie)
    missing.add(f"libstdc++-{GCC_VERSIONS[RELEASES[arch]]}-dev")
    package_dict: dict[str, str] = {}
    for package in package_meta:
        if package in missing:
            missing.remove(package)
            add_package_dependencies(package)
    if missing:
        raise Exception(f"Missing packages: {', '.join(missing)}")

    # Write the URLs and checksums of the requested packages to the output file
    output_file = os.path.join(SCRIPT_DIR, "generated_package_lists",
                               f"{RELEASES[arch]}.{arch}")
    with open(output_file, "w") as f:
        f.write("\n".join(sorted(package_dict)) + "\n")
    return package_dict


def hacks_and_patches(install_root: str, script_dir: str, arch: str) -> None:
    banner("Misc Hacks & Patches")

    debian_dir = os.path.join(install_root, "debian")
    control_file = os.path.join(debian_dir, "control")
    # Create an empty control file
    open(control_file, "a").close()

    # Remove an unnecessary dependency on qtchooser.
    qtchooser_conf = os.path.join(install_root, "usr", "lib", TRIPLES[arch],
                                  "qt-default/qtchooser/default.conf")
    if os.path.exists(qtchooser_conf):
        os.remove(qtchooser_conf)

    # __GLIBC_MINOR__ is used as a feature test macro. Replace it with the
    # earliest supported version of glibc (2.26).
    features_h = os.path.join(install_root, "usr", "include", "features.h")
    replace_in_file(features_h, r"(#define\s+__GLIBC_MINOR__)", r"\1 26 //")

    # C23 STRTOL requires glibc >= 2.38
    replace_in_file(features_h, r"(#\s?define\s+__GLIBC_USE_C23_STRTOL)",
                    r"\1 0 //")

    # fcntl64() was introduced in glibc 2.28. Make sure to use fcntl() instead.
    fcntl_h = os.path.join(install_root, "usr", "include", "fcntl.h")
    replace_in_file(
        fcntl_h,
        r"#ifndef __USE_FILE_OFFSET64(\nextern int fcntl)",
        r"#if 1\1",
    )

    # Do not use pthread_cond_clockwait as it was introduced in glibc 2.30.
    cppconfig_h = os.path.join(
        install_root,
        "usr",
        "include",
        TRIPLES[arch],
        "c++",
        str(GCC_VERSIONS[RELEASES[arch]]),
        "bits",
        "c++config.h",
    )
    replace_in_file(cppconfig_h,
                    r"(#define\s+_GLIBCXX_USE_PTHREAD_COND_CLOCKWAIT)",
                    r"// \1")

    # Include limits.h in stdlib.h to fix an ODR issue.
    stdlib_h = os.path.join(install_root, "usr", "include", "stdlib.h")
    replace_in_file(stdlib_h, r"(#include <stddef.h>)",
                    r"\1\n#include <limits.h>")

    # Move pkgconfig scripts.
    pkgconfig_dir = os.path.join(install_root, "usr", "lib", "pkgconfig")
    os.makedirs(pkgconfig_dir, exist_ok=True)
    triple_pkgconfig_dir = os.path.join(install_root, "usr", "lib",
                                        TRIPLES[arch], "pkgconfig")
    if os.path.exists(triple_pkgconfig_dir):
        for file in os.listdir(triple_pkgconfig_dir):
            shutil.move(os.path.join(triple_pkgconfig_dir, file),
                        pkgconfig_dir)

    if not os.path.exists(os.path.join(install_root, "lib")):
        os.symlink(os.path.join("usr", "lib"), os.path.join(install_root, "lib"))

    if (os.path.exists(os.path.join(install_root, "usr", "lib64")) and
        not os.path.exists(os.path.join(install_root, "lib64"))):
        os.symlink(os.path.join("usr", "lib64"), os.path.join(install_root, "lib64"))

    # Avoid requiring unsupported glibc versions.
    for lib in ["libc.so.6", "libm.so.6", "libcrypt.so.1"]:
        lib_path = os.path.join(install_root, "lib", TRIPLES[arch], lib)
        reversion_glibc.reversion_glibc(lib_path, arch)

def create_extra_symlinks(install_root: str, arch: str):
    if RELEASES[arch] != "bullseye":
        # Recent debian releases no longer symlink lib{dl,pthread,rt}.so
        for lib in ["libdl.so.2", "librt.so.1", "libpthread.so.0"]:
            os.symlink(
                lib,
                os.path.join(install_root, "lib", TRIPLES[arch],
                             lib.rpartition(".")[0]))


def replace_in_file(file_path: str, search_pattern: str,
                    replace_pattern: str) -> None:
    with open(file_path, "r") as file:
        content = file.read()
    with open(file_path, "w") as file:
        file.write(re.sub(search_pattern, replace_pattern, content))


def install_into_sysroot(build_dir: str, install_root: str,
                         packages: dict[str, str]) -> None:
    """
    Installs libraries and headers into the sysroot environment.
    """
    banner("Install Libs And Headers Into Jail")

    debian_packages_dir = os.path.join(build_dir, "debian-packages")
    os.makedirs(debian_packages_dir, exist_ok=True)

    debian_dir = os.path.join(install_root, "debian")
    os.makedirs(debian_dir, exist_ok=True)
    for package, sha256sum in packages.items():
        package_name = os.path.basename(package)
        package_path = os.path.join(debian_packages_dir, package_name)

        banner(f"Installing {package_name}")
        download_or_copy(package, package_path)
        if hash_file(hashlib.sha256(), package_path) != sha256sum:
            raise ValueError(f"SHA256 mismatch for {package_path}")

        sub_banner(f"Extracting to {install_root}")
        subprocess.run(["dpkg-deb", "-x", package_path, install_root],
                       check=True)

        base_package = get_base_package_name(package_path)
        debian_package_dir = os.path.join(debian_dir, base_package, "DEBIAN")

        # Extract the control file
        os.makedirs(debian_package_dir, exist_ok=True)
        with subprocess.Popen(
            ["dpkg-deb", "-e", package_path, debian_package_dir],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
        ) as proc:
            _, err = proc.communicate()
            if proc.returncode != 0:
                message = "Failed to extract control from"
                raise Exception(
                    f"{message} {package_path}: {err.decode('utf-8')}")

    # Prune /usr/share, leaving only allowlisted directories.
    USR_SHARE_ALLOWLIST = {
        "fontconfig",
        "pkgconfig",
        "wayland",
        "wayland-protocols",
    }
    usr_share = os.path.join(install_root, "usr", "share")
    for item in os.listdir(usr_share):
        full_path = os.path.join(usr_share, item)
        if os.path.isdir(full_path) and item not in USR_SHARE_ALLOWLIST:
            shutil.rmtree(full_path)


def get_base_package_name(package_path: str) -> str:
    """
    Retrieves the base package name from a Debian package.
    """
    result = subprocess.run(["dpkg-deb", "--field", package_path, "Package"],
                            capture_output=True,
                            text=True)
    if result.returncode != 0:
        raise Exception(
            f"Failed to get package name from {package_path}: {result.stderr}")
    return result.stdout.strip()


def cleanup_jail_symlinks(install_root: str) -> None:
    """
    Cleans up jail symbolic links by converting absolute symlinks
    into relative ones.
    """
    for root, dirs, files in os.walk(install_root):
        for name in files + dirs:
            full_path = os.path.join(root, name)
            if os.path.islink(full_path):
                target_path = os.readlink(full_path)
                if target_path == "/dev/null":
                    # Some systemd services get masked by symlinking them to
                    # /dev/null. It's safe to remove these.
                    os.remove(full_path)
                    continue

                # If the link's target does not exist, remove this broken link.
                if os.path.isabs(target_path):
                    absolute_target = os.path.join(install_root,
                                                   target_path.strip("/"))
                else:
                    absolute_target = os.path.join(os.path.dirname(full_path),
                                                   target_path)
                if not os.path.exists(absolute_target):
                    os.remove(full_path)
                    continue

                if os.path.isabs(target_path):
                    # Compute the relative path from the symlink to the target.
                    relative_path = os.path.relpath(
                        os.path.join(install_root, target_path.strip("/")),
                        os.path.dirname(full_path),
                    )
                    # Verify that the target exists inside the install_root.
                    joined_path = os.path.join(os.path.dirname(full_path),
                                               relative_path)
                    if not os.path.exists(joined_path):
                        raise Exception(
                            f"Link target doesn't exist: {joined_path}")
                    os.remove(full_path)
                    os.symlink(relative_path, full_path)


def removing_unnecessary_files(install_root, arch):
    """
    Minimizes the sysroot by removing unnecessary files.
    """
    # Preserve these files.
    gcc_triple = "i686-linux-gnu" if arch == "i386" else TRIPLES[arch]
    gcc_version = GCC_VERSIONS[RELEASES[arch]]
    ALLOWLIST = {
        f"usr/lib/gcc/{gcc_triple}/{gcc_version}/libgcc.a",
        f"usr/lib/{TRIPLES[arch]}/libc_nonshared.a",

        # https://developers.redhat.com/articles/2021/12/17/why-glibc-234-removed-libpthread
        f"usr/lib/{TRIPLES[arch]}/libdl.a",
        f"usr/lib/{TRIPLES[arch]}/libpthread.a",
        f"usr/lib/{TRIPLES[arch]}/librt.a",
    }

    for file in ALLOWLIST:
        assert os.path.exists(os.path.join(install_root,
                                           file)), f"{file} does not exist"

    # Remove all executables and static libraries, and any symlinks that
    # were pointing to them.
    reverse_links = collections.defaultdict(list)
    remove = []
    for root, _, files in os.walk(install_root):
        for filename in files:
            filepath = os.path.join(root, filename)
            if os.path.relpath(filepath, install_root) in ALLOWLIST:
                continue
            if os.path.islink(filepath):
                target_path = os.readlink(filepath)
                if not os.path.isabs(target_path):
                    target_path = os.path.join(root, target_path)
                reverse_links[os.path.realpath(target_path)].append(filepath)
            elif "so" in filepath.split(".")[-3:]:
                continue
            elif os.access(filepath, os.X_OK) or filepath.endswith(".a"):
                remove.append(filepath)
    for filepath in remove:
        os.remove(filepath)
        for link in reverse_links[filepath]:
            os.remove(link)


def strip_sections(install_root: str, arch: str):
    """
    Strips all sections from ELF files except for dynamic linking and
    essential sections. Skips static libraries (.a), object files (.o), and a
    few files used by other Chromium-related projects.
    """
    PRESERVED_FILES = (
        # Old debian(bullseye) has ld-2.31.so,
        # while in trixie, it is ld-linux-$ARCH.so.2
        r'(libc\.so\.\d)|(libc-\d.\d\d\.so)',
        r'(libm\.so\.\d)|(libm-\d.\d\d\.so)',
        r'(ld-linux.*\.so\.\d)|(ld-\d.\d\d\.so)',
    )

    PRESERVED_SECTIONS = {
        ".dynamic",
        ".dynstr",
        ".dynsym",
        ".gnu.version",
        ".gnu.version_d",
        ".gnu.version_r",
        ".hash",
        ".note.ABI-tag",
        ".note.gnu.build-id",
    }

    preserved_files_count = 0
    lib_dir = LIB_DIRS[RELEASES[arch]]
    lib_arch_path = os.path.join(install_root, lib_dir, TRIPLES[arch])
    for root, _, files in os.walk(install_root):
        for file in files:
            file_path = os.path.join(root, file)
            if file_path.startswith(lib_arch_path):
                for preserved in PRESERVED_FILES:
                    if re.match(preserved,
                                file) and not os.path.islink(file_path):
                        preserved_files_count += 1
                        continue

            if (os.access(file, os.X_OK) or file.endswith((".a", ".o"))
                    or os.path.islink(file_path)):
                continue

            # Verify this is an ELF file
            with open(file_path, "rb") as f:
                magic = f.read(4)
                if magic != b"\x7fELF":
                    continue

            # Get section headers
            objdump_cmd = ["objdump", "-h", file_path]
            result = subprocess.run(objdump_cmd,
                                    check=True,
                                    text=True,
                                    capture_output=True)
            section_lines = result.stdout.splitlines()

            # Parse section names
            sections = set()
            for line in section_lines:
                parts = line.split()
                if len(parts) > 1 and parts[0].isdigit():
                    sections.add(parts[1])

            sections_to_remove = sections - PRESERVED_SECTIONS
            if sections_to_remove:
                objcopy_arch = "amd64" if arch == "i386" else arch
                objcopy_bin = TRIPLES[objcopy_arch] + "-objcopy"
                objcopy_cmd = ([objcopy_bin] + [
                    f"--remove-section={section}"
                    for section in sections_to_remove
                ] + [file_path])
                subprocess.run(objcopy_cmd, check=True, stderr=subprocess.PIPE)
    if preserved_files_count != len(PRESERVED_FILES):
        raise Exception(
            f"Expected file(s) to preserve missing, preserved " +
            f"{preserved_files_count}, expected {len(PRESERVED_FILES)}")


def record_metadata(install_root: str) -> dict[str, tuple[float, float]]:
    """
    Recursively walk the install_root directory and record the metadata of all
    files. Symlinks are not followed. Returns a dictionary mapping each path
    (relative to install_root) to its original metadata.
    """
    metadata = {}
    for root, dirs, files in os.walk(install_root):
        for name in dirs + files:
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, install_root)
            st = os.lstat(full_path)
            metadata[rel_path] = (st.st_atime, st.st_mtime)
    return metadata


def restore_metadata(install_root: str,
                     old_meta: dict[str, tuple[float, float]]) -> None:
    """
    1. Restore the metadata of any file that exists in old_meta.
    2. For all other files, set their timestamp to ARCHIVE_TIMESTAMP.
    3. For all directories (including install_root), set the timestamp
       to ARCHIVE_TIMESTAMP.
    """
    # Convert the timestamp to a UNIX epoch time.
    archive_time = time.mktime(
        time.strptime(ARCHIVE_TIMESTAMP, "%Y%m%dT%H%M%SZ"))

    # Walk through the install_root, applying old_meta where available;
    # otherwise set times to archive_time.
    for root, dirs, files in os.walk(install_root):
        # Directories get archive_time.
        os.utime(root, (archive_time, archive_time))

        # Files: old_meta if available, else archive_time.
        for file_name in files:
            file_path = os.path.join(root, file_name)
            if os.path.lexists(file_path):
                rel_path = os.path.relpath(file_path, install_root)
                if rel_path in old_meta:
                    restore_time = old_meta[rel_path]
                else:
                    restore_time = (archive_time, archive_time)
                os.utime(file_path, restore_time, follow_symlinks=False)


def build_sysroot(arch: str, build_dir: str) -> None:
    install_root = os.path.join(build_dir, f"{RELEASES[arch]}_{arch}_staging")
    clear_install_dir(install_root)
    packages = generate_package_list(arch, build_dir)
    install_into_sysroot(build_dir, install_root, packages)
    old_metadata = record_metadata(install_root)
    hacks_and_patches(install_root, SCRIPT_DIR, arch)
    create_extra_symlinks(install_root, arch)
    cleanup_jail_symlinks(install_root)
    removing_unnecessary_files(install_root, arch)
    # Skips stripping so the sysroot can be used for testing
    # strip_sections(install_root, arch)
    restore_metadata(install_root, old_metadata)


def upload_sysroot(arch: str, build_dir: str) -> str:
    tarball_path = os.path.join(
        build_dir, f"{DISTRO}_{RELEASES[arch]}_{arch}_sysroot.tar.xz")
    command = [
        "upload_to_google_storage_first_class.py",
        "--bucket",
        "chrome-linux-sysroot",
        tarball_path,
    ]
    return subprocess.check_output(command).decode("utf-8")


def verify_package_listing(file_path: str, output_file: str, dist: str,
                           build_dir: str) -> None:
    """
    Verifies the downloaded Packages.xz file against its checksum and GPG keys.
    """
    # Paths for Release and Release.gpg files
    repo_basedir = f"{ARCHIVE_URL}/dists/{dist}"
    release_list = f"{repo_basedir}/{RELEASE_FILE}"
    release_list_gpg = f"{repo_basedir}/{RELEASE_FILE_GPG}"

    release_file = os.path.join(build_dir, f"{dist}-{RELEASE_FILE}")
    release_file_gpg = os.path.join(build_dir, f"{dist}-{RELEASE_FILE_GPG}")

    if not os.path.exists(KEYRING_FILE):
        raise Exception(f"KEYRING_FILE not found: {KEYRING_FILE}")

    # Download Release and Release.gpg files
    download_or_copy_non_unique_filename(release_list, release_file)
    download_or_copy_non_unique_filename(release_list_gpg, release_file_gpg)

    # Verify Release file with GPG
    subprocess.run(
        ["gpgv", "--keyring", KEYRING_FILE, release_file_gpg, release_file],
        check=True)

    # Find the SHA256 checksum for the specific file in the Release file
    sha256sum_pattern = re.compile(r"([a-f0-9]{64})\s+\d+\s+" +
                                   re.escape(file_path) + r"$")
    sha256sum_match = None
    with open(release_file, "r") as f:
        for line in f:
            if match := sha256sum_pattern.search(line):
                sha256sum_match = match.group(1)
                break

    if not sha256sum_match:
        raise Exception(
            f"Checksum for {file_path} not found in {release_file}")

    if hash_file(hashlib.sha256(), output_file) != sha256sum_match:
        raise Exception(f"Checksum mismatch for {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Build and upload Debian sysroot images for Chromium.")
    parser.add_argument("command", choices=["build", "upload"])
    parser.add_argument("architecture", choices=list(TRIPLES))
    args = parser.parse_args()
    build_dir = os.path.join(CHROME_DIR, "out", "sysroot-build",
                             RELEASES[args.architecture])

    sanity_check(build_dir)

    global ARCHIVE_URL
    if args.architecture == "loong64":
        ARCHIVE_URL = "https://snapshot.debian.org/archive/debian-ports/20250625T074124Z/"

    if args.command == "build":
        build_sysroot(args.architecture, build_dir)
    elif args.command == "upload":
        upload_sysroot(args.architecture, build_dir)


if __name__ == "__main__":
    main()
