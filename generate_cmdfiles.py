#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Generate .cmd files for all kernel source files without compiling
# This is used by 'make ninja' to prepare build rules
#
# Usage: python3 generate_cmdfiles.py [--srcarch SRCARCH] [--srctree SRCTREE] [--objtree OBJTREE]

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate .cmd files for kernel source files without compiling'
    )
    parser.add_argument(
        '--srcarch',
        default=os.environ.get('SRCARCH', 'arm64'),
        help='Source architecture (default: from $SRCARCH or arm64)'
    )
    parser.add_argument(
        '--srctree',
        default=os.environ.get('srctree', os.getcwd()),
        help='Source tree root directory (default: current directory)'
    )
    parser.add_argument(
        '--objtree',
        default=os.environ.get('objtree', os.getcwd()),
        help='Object tree root directory (default: current directory)'
    )
    parser.add_argument(
        '-j', '--jobs',
        type=int,
        default=4,
        help='Number of parallel jobs (default: 4)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output'
    )

    return parser.parse_args()


def get_directories(srcarch):
    """
    Get list of top-level directories to process.
    These correspond to the directories included in the kernel build.
    """
    dirs = [
        'init',
        'drivers',
        'kernel',
        'lib',
        'mm',
        'net',
        'fs',
        'ipc',
        'security',
        'crypto',
        'block',
        'io_uring',
        'virt',
        f'arch/{srcarch}',
    ]
    return dirs


def generate_for_dir(dir_path, srctree, objtree, verbose=False):
    """
    Generate .cmd files for a single directory.

    Args:
        dir_path: Directory path relative to objtree
        srctree: Source tree root
        objtree: Object tree root
        verbose: Enable verbose output

    Returns:
        tuple: (directory, success, message)
    """
    full_dir = os.path.join(objtree, dir_path)
    kbuild_path = os.path.join(srctree, dir_path, 'Kbuild')
    makefile_path = os.path.join(srctree, dir_path, 'Makefile')

    # Check if directory has Kbuild or Makefile in source tree
    if not os.path.exists(kbuild_path) and not os.path.exists(makefile_path):
        return (dir_path, True, "skipped (no Kbuild/Makefile)")

    # Build the make command
    # Need to pass: srcroot, srctree, objtree, obj
    # srcroot is the source root directory
    # srctree is the source tree (same as srcroot for in-tree builds)
    # objtree is the object tree (same as cwd for in-tree builds)
    # obj is the subdirectory to process
    makefile_build = os.path.join(srctree, 'scripts', 'Makefile.build')

    cmd = [
        'make',
        '-f', makefile_build,
        f'srcroot={srctree}',
        f'srctree={srctree}',
        f'objtree={objtree}',
        f'obj={dir_path}',
        'cmdfiles-mode=1',
        'cmdfiles'
    ]

    if verbose:
        print(f"  Processing: {dir_path}")

    try:
        result = subprocess.run(
            cmd,
            cwd=objtree,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout per directory
        )

        if result.returncode != 0 and verbose:
            return (dir_path, False, f"failed: {result.stderr[:200]}")

        return (dir_path, True, "done")

    except subprocess.TimeoutExpired:
        return (dir_path, False, "timeout")
    except Exception as e:
        return (dir_path, False, str(e))


def main():
    args = parse_arguments()

    srctree = os.path.realpath(args.srctree)
    objtree = os.path.realpath(args.objtree)
    srcarch = args.srcarch

    print(f"Generating .cmd files...")
    print(f"  Source tree: {srctree}")
    print(f"  Object tree: {objtree}")
    print(f"  Architecture: {srcarch}")
    print(f"  Parallel jobs: {args.jobs}")
    print()

    # Get directories to process
    dirs = get_directories(srcarch)

    # Filter to only existing directories
    existing_dirs = []
    for d in dirs:
        full_path = os.path.join(objtree, d)
        if os.path.isdir(full_path):
            existing_dirs.append(d)
        elif args.verbose:
            print(f"  Skipping {d}: directory not found")

    print(f"Processing {len(existing_dirs)} directories...")
    print()

    # Process directories in parallel
    success_count = 0
    fail_count = 0
    skip_count = 0

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(generate_for_dir, d, srctree, objtree, args.verbose): d
            for d in existing_dirs
        }

        for future in as_completed(futures):
            dir_path, success, message = future.result()

            if "skipped" in message:
                skip_count += 1
            elif success:
                success_count += 1
            else:
                fail_count += 1
                print(f"  FAILED: {dir_path} - {message}")

    print()
    print(f"Done generating .cmd files")
    print(f"  Success: {success_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Failed: {fail_count}")

    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
