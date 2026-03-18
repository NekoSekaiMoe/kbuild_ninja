#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Copyright (C) 2024
#
# Author: iFlow CLI
#
"""A tool for generating build.ninja from Linux kernel build artifacts.

This script generates a Ninja build file from the kernel's .cmd files,
enabling fast incremental builds with the Ninja build system.

Features:
- Compiles all kernel source files (.c, .S)
- Builds built-in.a archives for each directory
- Links vmlinux.a from all built-in.a files
- Links vmlinux.o and final vmlinux
- Builds kernel modules (.ko)
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

_DEFAULT_OUTPUT = 'build.ninja'
_DEFAULT_LOG_LEVEL = 'WARNING'

# Pattern to match .cmd files
_FILENAME_PATTERN = r'^\..*\.cmd$'

# Pattern to parse saved command from .cmd file
# Format: savedcmd_<target> := <command>
_CMD_PATTERN = r'^savedcmd_([^ ]+) := (.+)$'

# Pattern to parse compile command (for source file extraction)
_COMPILE_PATTERN = r'^(.* )(?P<file_path>[^ ]*\.[cS])$'

# Pattern to detect objcopy command for .pi.o files
_OBJCOPY_PATTERN = r'objcopy\s+.*\s+(\S+\.o)\s+(\S+\.pi\.o)'

_EXCLUDE_DIRS = ['.git', 'Documentation', 'include', 'tools', 'out', 'o']


def parse_arguments():
    """Sets up and parses command-line arguments."""
    usage = 'Creates a build.ninja file from kernel .cmd files'
    parser = argparse.ArgumentParser(description=usage)

    parser.add_argument('-d', '--directory', type=str, default='.',
                        help='specify the output directory used for the kernel build')

    parser.add_argument('-o', '--output', type=str, default=_DEFAULT_OUTPUT,
                        help='path to the output ninja file')

    parser.add_argument('--log_level', type=str, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default=_DEFAULT_LOG_LEVEL)

    parser.add_argument('-a', '--ar', type=str, default='ar',
                        help='command used for parsing .a archives')

    parser.add_argument('--ld', type=str, default='ld',
                        help='linker command')

    parser.add_argument('--cc', type=str, default='gcc',
                        help='compiler command')

    parser.add_argument('paths', type=str, nargs='*',
                        help='directories to search or files to parse')

    args = parser.parse_args()

    return (args.log_level,
            os.path.realpath(args.directory),
            args.output,
            args.ar,
            args.ld,
            args.cc,
            args.paths if len(args.paths) > 0 else [args.directory])


def escape_ninja_path(path):
    """Escape special characters in ninja paths."""
    if path is None:
        return ''
    return path.replace('$', '$$').replace(' ', '$ ').replace(':', '$:')


def escape_ninja_cmd(cmd):
    """Escape special characters in ninja commands."""
    if cmd is None:
        return ''
    # Escape $ but not $(...) which is valid in shell
    result = []
    i = 0
    while i < len(cmd):
        if cmd[i] == '$':
            if i + 1 < len(cmd) and cmd[i + 1] == '(':
                # Keep $( as is - it's shell command substitution
                result.append('$(')
                i += 2
            else:
                # Escape $ as $$
                result.append('$$')
                i += 1
        else:
            result.append(cmd[i])
            i += 1
    return ''.join(result)


def cmdfiles_in_dir(directory):
    """Generate the iterator of .cmd files found under the directory."""
    filename_matcher = re.compile(_FILENAME_PATTERN)
    exclude_dirs = [os.path.join(directory, d) for d in _EXCLUDE_DIRS]

    for dirpath, dirnames, filenames in os.walk(directory, topdown=True):
        # Skip excluded directories
        if any(dirpath.startswith(exd) for exd in exclude_dirs):
            dirnames[:] = []
            continue

        for filename in filenames:
            if filename_matcher.match(filename):
                yield os.path.join(dirpath, filename)


def normalize_path(path):
    """Normalize path by removing double slashes and fixing relative paths."""
    # Remove double slashes
    while '//' in path:
        path = path.replace('//', '/')
    return path


def fix_depfile_path(command, target):
    """Fix depfile path for ninja compatibility.
    
    Kernel uses .$o.d format (e.g., mm/.slub.o.d)
    Ninja expects $out.d format (e.g., mm/slub.o.d)
    
    This function transforms -Wp,-MMD,<dir>/.<name>.d to -Wp,-MMD,<dir>/<name>.d
    """
    # Find the pattern: -Wp,-MMD,<path>/.<name>.d
    # Replace it with: -Wp,-MMD,<path>/<name>.d
    pattern = r'-Wp,-MMD,([^,]+)/\.([^,]+)\.d'
    
    def replace_depfile(m):
        dir_path = m.group(1)
        name = m.group(2)
        return f'-Wp,-MMD,{dir_path}/{name}.d'
    
    return re.sub(pattern, replace_depfile, command)


def parse_cmdfile(cmdfile_path):
    """Parse a .cmd file and extract the saved command.

    Returns:
        tuple: (target_name, command) or (None, None) if parsing fails
    """
    try:
        with open(cmdfile_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                match = re.match(_CMD_PATTERN, line)
                if match:
                    target = normalize_path(match.group(1))
                    command = match.group(2)
                    # Replace $(pound) with #
                    command = command.replace('$(pound)', '#')
                    # Fix depfile path for ninja compatibility
                    command = fix_depfile_path(command, target)
                    return target, command
    except (IOError, OSError) as e:
        pass
    return None, None


def get_target_from_cmdfile(cmdfile_path):
    """Get the target name from a .cmd file path."""
    dir_name = os.path.dirname(cmdfile_path)
    base = os.path.basename(cmdfile_path)
    if base.startswith('.') and base.endswith('.cmd'):
        return os.path.join(dir_name, base[1:-4])
    return None


def parse_archive_for_objs(archive_path, ar_cmd):
    """Parse an archive file and return list of object files it contains."""
    try:
        output = subprocess.check_output(
            [ar_cmd, '-t', archive_path],
            stderr=subprocess.DEVNULL
        )
        objs = output.decode().strip().split()
        # Convert to full paths relative to archive directory
        archive_dir = os.path.dirname(archive_path)
        return [os.path.join(archive_dir, obj) for obj in objs if obj]
    except (subprocess.CalledProcessError, OSError):
        return []


def parse_vmlinux_objs(directory):
    """Parse .vmlinux.objs file to get list of objects for vmlinux."""
    vmlinux_objs_file = os.path.join(directory, '.vmlinux.objs')
    if not os.path.exists(vmlinux_objs_file):
        return []
    try:
        with open(vmlinux_objs_file, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except IOError:
        return []


def parse_modules_order(modules_order_path):
    """Parse modules.order file to get list of modules."""
    if not os.path.exists(modules_order_path):
        return []
    try:
        with open(modules_order_path, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except IOError:
        return []


def parse_mod_file(mod_file_path):
    """Parse .mod file to get list of objects for a module."""
    if not os.path.exists(mod_file_path):
        return []
    try:
        with open(mod_file_path, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except IOError:
        return []


def get_source_from_compile_cmd(command):
    """Extract source file from compile command."""
    match = re.match(_COMPILE_PATTERN, command.strip())
    if match:
        return match.group('file_path')
    return None


class NinjaFileGenerator:
    """Generator for Ninja build files from kernel build artifacts."""

    def __init__(self, directory, output, ar, ld, cc):
        self.directory = directory
        self.output = output
        self.ar = ar
        self.ld = ld
        self.cc = cc

        # Build rules storage
        self.compile_rules = {}  # obj -> (source, command)
        self.objcopy_rules = {}  # target.o -> (input.o, command) for objcopy transformations
        self.archive_rules = {}  # archive -> [objs]
        self.vmlinux_a_deps = []  # archives for vmlinux.a
        self.vmlinux_libs = []    # lib.a files
        self.modules = []         # list of .ko files

        # Additional artifacts
        self.special_objs = {}    # special object files like .vmlinux.export.o

    def collect_cmdfiles(self, paths):
        """Collect and parse all .cmd files from given paths."""
        for path in paths:
            if os.path.isdir(path):
                cmdfiles = cmdfiles_in_dir(path)
            elif path.endswith('.a'):
                # Parse archive and get .cmd files for its objects
                objs = parse_archive_for_objs(path, self.ar)
                cmdfiles = (to_cmdfile(obj) for obj in objs)
            elif path.endswith('modules.order'):
                # Get modules and their .cmd files
                modules = parse_modules_order(path)
                for mod in modules:
                    mod_file = mod.replace('.o', '.mod')
                    if os.path.exists(mod_file):
                        for obj in parse_mod_file(mod_file):
                            cmdfile = to_cmdfile(obj)
                            if os.path.exists(cmdfile):
                                target, cmd = parse_cmdfile(cmdfile)
                                if target and cmd:
                                    self.compile_rules[target] = (get_source_from_compile_cmd(cmd) or obj, cmd)
                continue
            else:
                continue

            for cmdfile in cmdfiles:
                if not os.path.exists(cmdfile):
                    continue
                target, command = parse_cmdfile(cmdfile)
                if target and command:
                    # Determine rule type based on target and command
                    if target.endswith('.o'):
                        # Check if this is an objcopy transformation (e.g., .o -> .pi.o)
                        objcopy_match = re.search(_OBJCOPY_PATTERN, command)
                        if objcopy_match:
                            input_o = normalize_path(objcopy_match.group(1))
                            self.objcopy_rules[target] = (input_o, command)
                        else:
                            source = get_source_from_compile_cmd(command)
                            self.compile_rules[target] = (source, command)
                    elif target.endswith('.a'):
                        # Archive rule - parse the command for dependencies
                        # Command format: rm -f $@ && ar rcST $@ obj1.o obj2.o ...
                        objs = self._parse_ar_command(command, target)
                        # Add to archive_rules even if empty (needed for subdirs with conditional compilation)
                        self.archive_rules[target] = objs

    def _parse_ar_command(self, command, archive):
        """Parse ar command to extract object dependencies."""
        # Find objects after the archive name
        parts = command.split()
        objs = []
        found_archive = False
        archive_basename = os.path.basename(archive)
        archive_dir = os.path.dirname(archive)
        
        for part in parts:
            # Normalize the part
            part = normalize_path(part)
            
            if part == archive or part == archive_basename:
                found_archive = True
                continue
            if found_archive and (part.endswith('.o') or part.endswith('.a')):
                # Convert to full path relative to archive directory
                # Handle paths that might be relative or have double slashes
                if os.path.isabs(part):
                    obj_path = part
                elif archive_dir and archive_dir != '.':
                    # Prepend archive directory to relative paths
                    obj_path = os.path.join(archive_dir, part)
                else:
                    obj_path = part
                objs.append(normalize_path(obj_path))
        return objs

    def collect_vmlinux_deps(self):
        """Collect vmlinux dependencies from built-in.a.cmd and lib files."""
        # Parse root built-in.a.cmd to get vmlinux.a dependencies
        root_builtin_cmd = os.path.join(self.directory, '.built-in.a.cmd')
        if os.path.exists(root_builtin_cmd):
            target, command = parse_cmdfile(root_builtin_cmd)
            if target and command:
                # Parse the ar command to get the list of built-in.a files
                deps = self._parse_ar_command(command, target)
                # Filter out built-in.a files for vmlinux.a deps
                self.vmlinux_a_deps = [d for d in deps if d.endswith('built-in.a')]
                # lib.a files go to vmlinux_libs
                self.vmlinux_libs = [d for d in deps if d.endswith('lib.a')]
        
        # Also check for existing vmlinux.a
        vmlinux_a = os.path.join(self.directory, 'vmlinux.a')
        if os.path.exists(vmlinux_a) and not self.vmlinux_a_deps:
            self.vmlinux_a_deps = parse_archive_for_objs(vmlinux_a, self.ar)

        # Check for special objects
        export_o = os.path.join(self.directory, '.vmlinux.export.o')
        if os.path.exists(export_o):
            self.special_objs['export'] = export_o

        builtin_dtbs_o = os.path.join(self.directory, '.builtin-dtbs.o')
        if os.path.exists(builtin_dtbs_o):
            self.special_objs['builtin_dtbs'] = builtin_dtbs_o

    def collect_modules(self):
        """Collect module information from modules.order."""
        modules_order = os.path.join(self.directory, 'modules.order')
        self.modules = parse_modules_order(modules_order)

    def generate(self):
        """Generate the ninja build file."""
        with open(self.output, 'w', encoding='utf-8') as f:
            self._synthesize_missing_objcopy_rules()
            self._synthesize_missing_dtb_rules()
            self._write_header(f)
            self._write_rules(f)
            self._write_compile_rules(f)
            self._write_objcopy_rules(f)
            self._write_archive_rules(f)
            self._write_vmlinux_rules(f)
            self._write_modules_rules(f)
            self._write_default_target(f)

    def _synthesize_missing_objcopy_rules(self):
        """Synthesize objcopy rules for .pi.o files missing .cmd files."""
        # Determine if building out-of-tree
        # Check if directory is different from current working directory
        try:
            dir_abs = os.path.realpath(self.directory)
            cwd_abs = os.path.realpath(os.getcwd())
            same_dir = dir_abs == cwd_abs
        except (OSError, ValueError):
            same_dir = self.directory == '.'

        # Check if any existing compile rule has ../ prefix (indicates out-of-tree)
        has_dotdot_prefix = False
        for obj, (source, _) in self.compile_rules.items():
            if source and source.startswith('../'):
                has_dotdot_prefix = True
                break

        # out_of_tree if we're in a different directory OR if .cmd files indicate it
        out_of_tree = not same_dir or has_dotdot_prefix
        prefix = '../' if out_of_tree else ''

        # Find all .pi.o files needed by archives but without objcopy rules
        for archive, objs in self.archive_rules.items():
            for obj in objs:
                if obj.endswith('.pi.o') and obj not in self.objcopy_rules:
                    # .pi.o -> .o (remove .pi.o (5 chars), add .o)
                    base_o = obj[:-5] + '.o'
                    
                    # Check if base_o doesn't have a compile rule - synthesize one for lib-*.o
                    if base_o not in self.compile_rules and os.path.basename(base_o).startswith('lib-'):
                        # lib-fdt.o comes from lib/fdt.c
                        lib_name = os.path.basename(base_o)[4:-2]  # remove 'lib-' and '.o'
                        src_path = f'{prefix}lib/{lib_name}.c' if prefix else f'lib/{lib_name}.c'
                        # Find an existing pi compile rule to use as template
                        pi_compile_rules = [(t, (s, c)) for t, (s, c) in self.compile_rules.items() 
                                           if 'kernel/pi/' in t and t.endswith('.o') and not t.endswith('.pi.o')]
                        if pi_compile_rules:
                            sample_target, (sample_src, sample_cmd) = pi_compile_rules[0]
                            # Replace target and source in command
                            synthesized_compile_cmd = sample_cmd.replace(sample_target, base_o)
                            # Replace source path pattern - handle both with and without ../ prefix
                            synthesized_compile_cmd = re.sub(r'(\.\./)?arch/arm64/kernel/pi/\S+\.c', src_path, synthesized_compile_cmd)
                            self.compile_rules[base_o] = (src_path, synthesized_compile_cmd)
                    
                    if base_o in self.compile_rules:
                        # Synthesize an objcopy rule
                        # Use a default objcopy command pattern
                        synthesized_cmd = f'objcopy --prefix-symbols=__pi_ --remove-section=.note.gnu.property {base_o} {obj}'
                        if self.objcopy_rules:
                            # Use existing objcopy rule as template for relacheck
                            sample_target = next(iter(self.objcopy_rules.keys()))
                            _, sample_cmd = self.objcopy_rules[sample_target]
                            # Replace paths in the sample command
                            sample_base = sample_target[:-5] + '.o'
                            synthesized_cmd = sample_cmd.replace(sample_base, base_o).replace(sample_target, obj)
                        self.objcopy_rules[obj] = (base_o, synthesized_cmd)

    def _synthesize_missing_dtb_rules(self):
        """Synthesize rules for generated .o files missing .cmd files."""
        # Handle .dtb.o, generated .c -> .o, and other special cases
        for archive, objs in list(self.archive_rules.items()):
            for obj in list(objs):  # Use list() to allow modification
                if obj.endswith('.o') and obj not in self.compile_rules and obj not in self.objcopy_rules:
                    # This .o file has no compile or objcopy rule
                    # Check if it's a known generated file type
                    if obj.endswith('.dtb.o'):
                        # Device tree blob - remove from deps
                        # (would need dtc to build, which is complex)
                        self.archive_rules[archive].remove(obj)
                    elif 'deftbl' in obj or 'defkeymap' in obj:
                        # Generated source files - remove from deps
                        self.archive_rules[archive].remove(obj)
                    else:
                        # Unknown - try to find source file
                        obj_dir = os.path.dirname(obj)
                        obj_name = os.path.basename(obj).replace('.o', '')
                        # Try .c, .S in same directory
                        # Use '../' prefix only when building out-of-tree (directory != '.')
                        prefix = '../' if self.directory != '.' else ''
                        for ext in ['.c', '.S']:
                            src_path = os.path.join(prefix, obj_dir, obj_name + ext) if prefix else os.path.join(obj_dir, obj_name + ext)
                            if os.path.exists(src_path):
                                # Found source, synthesize a simple compile rule
                                # Use existing compile rule as template if available
                                if self.compile_rules:
                                    sample_target = next(iter(self.compile_rules.keys()))
                                    _, sample_cmd = self.compile_rules[sample_target]
                                    # Replace the sample paths with our paths
                                    synthesized_cmd = sample_cmd.replace(sample_target, obj)
                                    synthesized_cmd = re.sub(r'\S+\.c$', src_path, synthesized_cmd)
                                    self.compile_rules[obj] = (src_path, synthesized_cmd)
                                break

    def _write_header(self, f):
        """Write ninja file header."""
        f.write('# Generated by generate_ninja.py\n')
        f.write('# Do not edit manually\n\n')
        f.write(f'builddir = {escape_ninja_path(self.directory)}\n')
        f.write(f'AR = {self.ar}\n')
        f.write(f'LD = {self.ld}\n')
        f.write(f'CC = {self.cc}\n\n')

    def _write_rules(self, f):
        """Write ninja build rules."""
        # Compile rule for C files
        f.write('rule cc\n')
        f.write('  command = $cmd\n')
        f.write('  description = CC $out\n')
        f.write('  deps = gcc\n')
        f.write('  depfile = $out.d\n\n')

        # Compile rule for assembly files
        f.write('rule as\n')
        f.write('  command = $cmd\n')
        f.write('  description = AS $out\n\n')

        # Archive rule
        f.write('rule ar\n')
        f.write('  command = rm -f $out && $AR rcST $out $in\n')
        f.write('  description = AR $out\n\n')

        # Link rule for object files
        f.write('rule ld\n')
        f.write('  command = $cmd\n')
        f.write('  description = LD $out\n\n')

        # Link rule for vmlinux.o
        f.write('rule ld_vmlinux_o\n')
        f.write('  command = $LD -r -o $out --whole-archive $in --no-whole-archive --start-group $libs --end-group\n')
        f.write('  description = LD $out\n\n')

        # Link rule for vmlinux
        f.write('rule ld_vmlinux\n')
        f.write('  command = $cmd\n')
        f.write('  description = LD $out\n\n')

        # Kallsyms rule
        f.write('rule kallsyms\n')
        f.write('  command = scripts/kallsyms $in > $out\n')
        f.write('  description = KALLSYMS $out\n\n')

        # Modpost rule
        f.write('rule modpost\n')
        f.write('  command = scripts/mod/modpost $args\n')
        f.write('  description = MODPOST\n\n')

        # Module link rule
        f.write('rule ld_ko\n')
        f.write('  command = $LD -r -o $out $in\n')
        f.write('  description = LD [M] $out\n\n')

        # Objcopy rule (for transformations like .o -> .pi.o)
        f.write('rule objcopy\n')
        f.write('  command = $cmd\n')
        f.write('  description = OBJCOPY $out\n\n')

    def _write_compile_rules(self, f):
        """Write compile rules for object files."""
        f.write('# Object file compilation rules\n')

        # Use '../' prefix only when building out-of-tree
        # Detect out-of-tree by checking if .cmd files have ../ prefixed source paths
        # or if the directory is different from current working directory
        try:
            dir_abs = os.path.realpath(self.directory)
            cwd_abs = os.path.realpath(os.getcwd())
            same_dir = dir_abs == cwd_abs
        except (OSError, ValueError):
            same_dir = self.directory == '.'

        # Check if any existing compile rule has ../ prefix (indicates out-of-tree)
        has_dotdot_prefix = False
        for obj, (source, _) in self.compile_rules.items():
            if source and source.startswith('../'):
                has_dotdot_prefix = True
                break

        # out_of_tree if we're in a different directory OR if .cmd files indicate it
        out_of_tree = not same_dir or has_dotdot_prefix
        prefix = '../' if out_of_tree else ''

        for obj in sorted(self.compile_rules.keys()):
            source, command = self.compile_rules[obj]
            if not source:
                continue

            # Add prefix for source path when building out-of-tree
            # But only if source doesn't already have the prefix
            if prefix and not source.startswith('../'):
                source = prefix + source

            escaped_obj = escape_ninja_path(obj)
            escaped_src = escape_ninja_path(source)
            escaped_cmd = escape_ninja_cmd(command)

            # Determine if assembly or C
            if source.endswith('.S'):
                rule = 'as'
            else:
                rule = 'cc'

            f.write(f'build {escaped_obj}: {rule} {escaped_src}\n')
            f.write(f'  cmd = {escaped_cmd}\n\n')

    def _write_objcopy_rules(self, f):
        """Write objcopy transformation rules (e.g., .o -> .pi.o)."""
        if not self.objcopy_rules:
            return

        f.write('# Objcopy transformation rules\n')

        for target in sorted(self.objcopy_rules.keys()):
            input_o, command = self.objcopy_rules[target]
            escaped_target = escape_ninja_path(target)
            escaped_input = escape_ninja_path(input_o)
            escaped_cmd = escape_ninja_cmd(command)

            f.write(f'build {escaped_target}: objcopy {escaped_input}\n')
            f.write(f'  cmd = {escaped_cmd}\n\n')

    def _write_archive_rules(self, f):
        """Write archive rules for built-in.a files."""
        f.write('# Archive rules\n')

        for archive in sorted(self.archive_rules.keys()):
            objs = self.archive_rules[archive]

            escaped_archive = escape_ninja_path(archive)
            escaped_objs = ' '.join(escape_ninja_path(obj) for obj in sorted(objs)) if objs else ''

            # Write rule even for empty archives - they're still needed as dependencies
            f.write(f'build {escaped_archive}: ar {escaped_objs}\n\n')

    def _write_vmlinux_rules(self, f):
        """Write rules for vmlinux build chain."""
        if not self.vmlinux_a_deps:
            return

        f.write('# vmlinux build rules\n')

        # vmlinux.a from all built-in.a
        vmlinux_a = os.path.join(self.directory, 'vmlinux.a')
        escaped_vmlinux_a = escape_ninja_path(vmlinux_a)
        escaped_deps = ' '.join(escape_ninja_path(dep) for dep in sorted(self.vmlinux_a_deps))

        f.write(f'build {escaped_vmlinux_a}: ar {escaped_deps}\n\n')

        # vmlinux.o from vmlinux.a and lib.a files
        vmlinux_o = os.path.join(self.directory, 'vmlinux.o')
        escaped_vmlinux_o = escape_ninja_path(vmlinux_o)
        escaped_libs = ' '.join(escape_ninja_path(lib) for lib in sorted(self.vmlinux_libs))

        f.write(f'build {escaped_vmlinux_o}: ld_vmlinux_o {escaped_vmlinux_a}\n')
        f.write(f'  libs = {escaped_libs}\n\n')

        # vmlinux final link
        # This requires the linker script and potentially kallsyms
        vmlinux = os.path.join(self.directory, 'vmlinux')

        # Check for linker script
        lds_file = self._find_linker_script()
        if lds_file:
            f.write(f'# Linker script: {lds_file}\n')

        # Write vmlinux rule using link-vmlinux.sh approach
        # For simplicity, we call make vmlinux which handles all the complexity
        f.write(f'build {escape_ninja_path(vmlinux)}: ld_vmlinux {escaped_vmlinux_o}\n')
        f.write(f'  cmd = cd {self.directory} && make vmlinux\n\n')

        # Add init/version-timestamp.o dependency
        version_o = os.path.join(self.directory, 'init/version-timestamp.o')
        if os.path.exists(os.path.join(self.directory, 'init')):
            f.write(f'# Note: init/version-timestamp.o is built by link-vmlinux.sh\n\n')

    def _find_linker_script(self):
        """Find the linker script for the architecture."""
        # Try common locations
        arch_dirs = [d for d in os.listdir(self.directory)
                     if d.startswith('arch/') or os.path.isdir(os.path.join(self.directory, 'arch', d))]

        for arch in ['arm64', 'x86', 'riscv', 'arm', 'mips', 'powerpc']:
            lds = os.path.join(self.directory, f'arch/{arch}/kernel/vmlinux.lds')
            if os.path.exists(lds):
                return lds
        return None

    def _write_modules_rules(self, f):
        """Write rules for module builds."""
        if not self.modules:
            return

        f.write('# Module build rules\n')

        all_kos = []

        for module_o in self.modules:
            # module_o is like drivers/net/ethernet/intel/e1000e/e1000e.o
            module_dir = os.path.dirname(module_o)
            module_base = os.path.basename(module_o)

            # .ko file
            ko_file = module_o.replace('.o', '.ko')
            all_kos.append(ko_file)

            # .mod file
            mod_file = module_o.replace('.o', '.mod')

            # Get objects for this module
            mod_objs = parse_mod_file(mod_file)
            if mod_objs:
                escaped_ko = escape_ninja_path(ko_file)
                escaped_objs = ' '.join(escape_ninja_path(obj) for obj in sorted(mod_objs))

                # Build .mod.o first
                mod_c = module_o.replace('.o', '.mod.c')
                mod_o = module_o.replace('.o', '.mod.o')

                f.write(f'build {escape_ninja_path(mod_o)}: cc {escape_ninja_path(mod_c)}\n')
                f.write(f'  cmd = $CC -c -o $out $in\n\n')

                # Link .ko
                f.write(f'build {escaped_ko}: ld_ko {escaped_objs} {escape_ninja_path(mod_o)}\n\n')

        # all-modules target
        if all_kos:
            escaped_kos = ' '.join(escape_ninja_path(ko) for ko in sorted(all_kos))
            f.write(f'build modules: phony {escaped_kos}\n\n')

    def _write_default_target(self, f):
        """Write the default target."""
        f.write('# Default targets\n')

        vmlinux = os.path.join(self.directory, 'vmlinux')
        has_vmlinux = os.path.exists(os.path.join(self.directory, 'vmlinux.a')) or self.vmlinux_a_deps
        has_modules = bool(self.modules)

        targets = []
        if has_vmlinux:
            targets.append(escape_ninja_path(vmlinux))
        if has_modules:
            targets.append('modules')

        if targets:
            f.write(f'build all: phony {" ".join(targets)}\n')
        else:
            # Fallback: build all built-in.a files
            all_archives = [escape_ninja_path(a) for a in sorted(self.archive_rules.keys())]
            if all_archives:
                f.write(f'build all: phony {" ".join(all_archives)}\n')
            else:
                f.write('build all: phony\n')

        f.write('\ndefault all\n')


def to_cmdfile(path):
    """Return the path of .cmd file for the given build artifact."""
    dir_name = os.path.dirname(path)
    base = os.path.basename(path)
    return os.path.join(dir_name, '.' + base + '.cmd')


def generate_ninja(directory, output, ar, ld, cc, paths):
    """Main function to generate build.ninja."""
    generator = NinjaFileGenerator(directory, output, ar, ld, cc)

    print(f'Collecting build rules from: {", ".join(paths)}')
    generator.collect_cmdfiles(paths)

    print(f'Collecting vmlinux dependencies...')
    generator.collect_vmlinux_deps()

    print(f'Collecting module information...')
    generator.collect_modules()

    print(f'Generating {output}...')
    generator.generate()

    print(f'Done. Generated {output}')
    print(f'  - {len(generator.compile_rules)} compile rules')
    print(f'  - {len(generator.objcopy_rules)} objcopy rules')
    print(f'  - {len(generator.archive_rules)} archive rules')
    print(f'  - {len(generator.modules)} modules')


def main():
    """Main entry point."""
    log_level, directory, output, ar, ld, cc, paths = parse_arguments()

    level = getattr(__import__('logging'), log_level.upper(), None)
    if level:
        import logging
        logging.basicConfig(format='%(levelname)s: %(message)s', level=level)

    generate_ninja(directory, output, ar, ld, cc, paths)


if __name__ == '__main__':
    main()