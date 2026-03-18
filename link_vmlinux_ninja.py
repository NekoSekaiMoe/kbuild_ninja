#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Link vmlinux for ninja build
# This script handles the final vmlinux link step
#
# Usage: python3 link_vmlinux_ninja.py --vmlinux-o vmlinux.o --output vmlinux

import argparse
import os
import subprocess
import sys


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Link vmlinux for ninja builds'
    )
    parser.add_argument(
        '--vmlinux-o', required=True,
        help='Path to vmlinux.o (input)'
    )
    parser.add_argument(
        '--output', '-o', required=True,
        help='Output vmlinux path'
    )
    parser.add_argument(
        '--objtree', default='.',
        help='Object tree root (default: current directory)'
    )
    parser.add_argument(
        '--srctree', default='.',
        help='Source tree root (default: current directory)'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Verbose output'
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    objtree = os.path.realpath(args.objtree)
    srctree = os.path.realpath(args.srctree)
    vmlinux_o = os.path.realpath(args.vmlinux_o)
    output = os.path.realpath(args.output)

    if args.verbose:
        print(f"Linking vmlinux:")
        print(f"  objtree: {objtree}")
        print(f"  srctree: {srctree}")
        print(f"  vmlinux.o: {vmlinux_o}")
        print(f"  output: {output}")

    # The kallsyms iteration process is complex and tightly integrated
    # with the kernel build system. We use make for the final link step
    # which handles:
    # - kallsyms iteration (up to 3 passes)
    # - BTF generation
    # - System.map generation
    # - Table sorting
    
    # Build make command
    if objtree == srctree:
        # In-tree build
        cmd = ['make', '-C', objtree, 'vmlinux']
    else:
        # Out-of-tree build
        cmd = ['make', '-C', objtree, f'SRCTREE={srctree}', 'vmlinux']

    if args.verbose:
        print(f"  Running: {' '.join(cmd)}")

    # Run make vmlinux
    result = subprocess.run(
        cmd,
        cwd=objtree,
        env={**os.environ, 'KBUILD_VERBOSE': '1' if args.verbose else '0'}
    )

    if result.returncode != 0:
        print(f"Error: vmlinux link failed", file=sys.stderr)
        sys.exit(result.returncode)

    # Verify output
    expected_vmlinux = os.path.join(objtree, 'vmlinux')
    if not os.path.exists(expected_vmlinux):
        print(f"Error: vmlinux not found at {expected_vmlinux}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"vmlinux linked successfully")

    return 0


if __name__ == '__main__':
    sys.exit(main())
